"""Worker-owned meeting lifecycle; keyboard and Tk callbacks only enqueue work."""

import array
import copy
from collections import deque
import itertools
import math
import os
from pathlib import Path
import queue
import re
import sys
import threading
import time

from meeting_audio import NativeCapture
from meeting_mixdown import export_mixdown
from meeting_library import MeetingLibrary, REPORT_HISTORY_LIMIT as LIBRARY_REPORT_HISTORY_LIMIT
from meeting_settings import resolve_meeting_settings
from meeting_store import MeetingStore
from meeting_titles import initial_recording_title, refine_recording_title


_UNSET = object()


WAVEFORM_POINTS = 360
WAVEFORM_BLOCK_POINTS = 24
TRANSCRIPT_PAGE_SIZE = 100
TRANSCRIPT_LIMIT = 500
REPORT_HISTORY_LIMIT = LIBRARY_REPORT_HISTORY_LIMIT
PLAYBACK_PROGRESS_INTERVAL = 0.25
_INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def _report_history_projection(report):
    """Detach only bounded report metadata for controller/UI history calls."""
    if not isinstance(report, dict):
        return None
    identifier = report.get("id", report.get("report_id"))
    if not isinstance(identifier, str) or not identifier:
        return None
    result = {"id": identifier}
    for key in (
        "schema_version", "kind", "profile_id", "profile_version",
        "session_id", "transcript_revision", "status", "created_at",
        "completed_at", "virtual",
    ):
        value = report.get(key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            if key in report:
                result[key] = value
    model = report.get("model")
    if isinstance(model, dict):
        result["model"] = {
            key: model[key]
            for key in ("id", "sha256", "runtime", "context_limit")
            if key in model and isinstance(model[key], (str, int, float, bool))
        }
    reviewed = report.get("reviewed_artifact")
    result["reviewed"] = reviewed is not None or bool(report.get("reviewed"))
    if isinstance(reviewed, dict) and isinstance(reviewed.get("generation"), int):
        result["review_generation"] = reviewed["generation"]
    return result


def _normalize_saved_answer(answer, question="", revision=None, revision_id=None):
    """Remove controller/UI metadata before crossing the intelligence seam."""
    if not isinstance(answer, dict):
        return answer, question, revision if revision is not None else revision_id, None
    clean = dict(answer)
    provenance = clean.pop("_provenance", None)
    embedded_question = clean.pop("question", None)
    embedded_revision = clean.pop("revision", None)
    embedded_revision_id = clean.pop("revision_id", None)
    if (embedded_revision is not None and embedded_revision_id is not None
            and embedded_revision != embedded_revision_id):
        raise ValueError("A revisão de transcrição foi informada duas vezes.")
    embedded_revision = (embedded_revision if embedded_revision is not None
                         else embedded_revision_id)
    provenance_revision = provenance.get("revision") if isinstance(provenance, dict) else None
    selected_revision = revision if revision is not None else revision_id
    if selected_revision is not None and embedded_revision is not None and selected_revision != embedded_revision:
        raise ValueError("A revisão de transcrição foi informada duas vezes.")
    if selected_revision is None:
        selected_revision = embedded_revision if embedded_revision is not None else provenance_revision
    selected_question = question
    if not selected_question and isinstance(embedded_question, str):
        selected_question = embedded_question
    return clean, selected_question, selected_revision, provenance


def _bounded_report_history_limit(value):
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = REPORT_HISTORY_LIMIT
    if isinstance(value, bool):
        result = REPORT_HISTORY_LIMIT
    return max(1, min(REPORT_HISTORY_LIMIT, result))


def _amplitude_envelope(values, channels, points=WAVEFORM_BLOCK_POINTS):
    """Return bounded real peaks from one interleaved native audio block."""
    frames = len(values) // channels if channels else 0
    if frames <= 0:
        return []
    bucket_frames = max(1, math.ceil(frames / max(1, points)))
    peaks = []
    for first in range(0, frames, bucket_frames):
        last = min(frames, first + bucket_frames)
        peak = 0.0
        for frame in range(first, last):
            offset = frame * channels
            for channel in range(channels):
                value = values[offset + channel]
                if math.isfinite(value):
                    peak = max(peak, abs(value))
        peaks.append(min(1.0, peak))
    return peaks


def _measure_audio(payload, channels):
    """Return a finite peak and bounded waveform from one native audio block."""
    values = array.array("f")
    values.frombytes(payload)
    if sys.byteorder != "little":
        values.byteswap()
    peak = min(1.0, max((abs(value) for value in values if math.isfinite(value)), default=0.0))
    return peak, _amplitude_envelope(values, channels)


def _final_audio_path(root, settings, session_id, title=""):
    """Resolve one non-overwriting visible recording path."""
    if settings.destination:
        destination = Path(settings.destination).expanduser()
        if not destination.is_absolute() or not destination.is_dir():
            raise ValueError("A pasta padrão de gravações não existe ou não é absoluta.")
    else:
        destination = Path(root).resolve().parent / "recordings"
        destination.mkdir(parents=True, exist_ok=True)
    clean_title = _INVALID_FILENAME.sub("-", str(title)).strip(" .-")[:120]
    stem = session_id + (" - " + clean_title if clean_title else "")
    candidate = destination / (stem + ".mp3")
    # ceiling: a session will not probe an unbounded hostile destination.
    for suffix in range(2, 1002):
        if not candidate.exists():
            return candidate
        candidate = destination / f"{stem} ({suffix}).mp3"
    raise RuntimeError("A pasta de gravações contém muitas cópias com o mesmo nome.")


class MeetingController:
    def __init__(self, root, voice, notify=None, capture_factory=NativeCapture, store=None,
                 library=None, clock=None):
        self.root = root
        self.voice = voice
        self.notify = notify or (lambda message: None)
        self.capture_factory = capture_factory
        self._clock = time.monotonic if clock is None else clock
        # Store initialization/recovery is lazy and runs on an IO worker.
        self._store = store
        self._library = library
        if self._library is not None and self._store is not None:
            existing = getattr(self._library, "_store", None)
            if existing is None:
                self._library._store = self._store
            elif existing is not self._store:
                raise ValueError("MeetingController recebeu dois MeetingStore diferentes.")
        self._store_lock = threading.RLock()
        self._lock = threading.RLock()
        self._thread = None
        self._processing_thread = None
        self._play_thread = None
        self._play_stop = threading.Event()
        self._playback_generation = 0
        self._playback_active = False
        self._playback_session = None
        self._playback_track = None
        self._playback_start = 0.0
        self._playback_position = 0.0
        self._playback_started = 0.0
        self._playback_error = ""
        self._cancel = threading.Event()
        self._file_done = threading.Event()
        self._file_done.set()
        self._commands = queue.Queue(maxsize=16)
        self._stop = threading.Event()
        self._closed = False
        self._generation = 0
        self._state = "idle"
        self._session_id = None
        self._error = ""
        self._started = 0.0
        self._elapsed = 0.0
        self._levels = {"microphone": 0.0, "system": 0.0}
        self._previewing = False
        # ceiling: the UI keeps only the latest 360 measured envelope points
        # per source, independent of recording duration.
        self._waveforms = {track: deque(maxlen=WAVEFORM_POINTS)
                           for track in ("microphone", "system")}
        self._output_path = ""
        self._postprocess = ""
        self._processing = False
        self._processing_session = None
        self._retention_active = False
        self._retention_service = None
        self._privacy_defaults = None
        self._recording_consent_granted = False
        self._last_status = ""
        self._source_errors = set()
        self._annotation_generations = {}
        self._index_rebuild_lock = threading.Lock()
        self._index_rebuild_cancel = threading.Event()
        self._index_rebuild_progress = {"done": 0, "total": None, "state": "idle"}

    # -- Privacy and retention admission --------------------------------

    def refresh_privacy_defaults(self):
        """Load validated privacy settings on an IO/UI worker and cache them.

        Hotkey callbacks must not read workspace files.  The application can
        call this seam during startup or when the settings view is opened;
        ``start`` only consults the resulting in-memory snapshot.
        """
        reader = getattr(self.library, "read_privacy_defaults", None)
        if callable(reader):
            value = reader()
        else:
            workspace = self.library.read_workspace()
            value = workspace.get("privacy_defaults", {})
        if not isinstance(value, dict):
            raise ValueError("As configurações de privacidade são inválidas.")
        with self._lock:
            self._privacy_defaults = value
        return copy.deepcopy(value)

    def read_workspace(self):
        """Read the detached workspace projection on a background worker.

        The GUI uses this small controller seam instead of reaching through to
        ``MeetingLibrary``.  Keeping the read here also makes it possible for
        the application to swap the controller's library without teaching Tk
        about the canonical storage layout.
        """
        return self.library.read_workspace()

    def update_workspace(self, patch, *, expected_generation=_UNSET):
        """Atomically merge workspace settings and refresh controller caches."""
        kwargs = {}
        if expected_generation is not _UNSET:
            kwargs["expected_generation"] = expected_generation
        result = self.library.update_workspace(patch, **kwargs)
        if not isinstance(result, dict):
            raise ValueError("A atualização do workspace retornou um estado inválido.")
        privacy = result.get("privacy_defaults", {})
        if not isinstance(privacy, dict):
            raise ValueError("As configurações de privacidade retornadas são inválidas.")
        with self._lock:
            self._privacy_defaults = copy.deepcopy(privacy)
        # The retention service captures its trash deadline at construction;
        # discard it after a policy edit so the next operation reads the new
        # validated defaults.  No active operation can overlap this call.
        with self._store_lock:
            self._retention_service = None
        return copy.deepcopy(result)

    save_workspace = update_workspace

    def update_privacy_defaults(self, patch, *, expected_generation=_UNSET):
        """Merge only the privacy section while preserving unknown keys."""
        return self.update_workspace({"privacy_defaults": patch}, expected_generation=expected_generation)

    def update_retention_defaults(self, patch, *, expected_generation=_UNSET):
        """Merge only retention settings while preserving future policy keys."""
        return self.update_workspace({"retention_defaults": patch}, expected_generation=expected_generation)

    def privacy_defaults(self):
        with self._lock:
            cached = self._privacy_defaults
        if cached is not None:
            return copy.deepcopy(cached)
        return self.refresh_privacy_defaults()

    @staticmethod
    def recording_notice_text(language="pt-BR"):
        if language == "en-US":
            language = "en"
        if language == "en":
            return (
                "This meeting is being recorded locally by SnipVoice. "
                "Audio and transcripts stay on this device and are not uploaded by the app. "
                "Please confirm that everyone has been informed and consents before recording."
            )
        if language != "pt-BR":
            raise ValueError("O idioma do aviso de gravação é inválido.")
        return (
            "Esta reunião está sendo gravada localmente pelo SnipVoice. "
            "O áudio e as transcrições ficam neste dispositivo e não são enviados pelo app. "
            "Confirme que todas as pessoas foram informadas e consentiram antes de gravar."
        )

    def recording_notice(self, language=None):
        defaults = self.privacy_defaults()
        notice = defaults.get("recording_notice")
        if not isinstance(notice, dict):
            notice = {}
        if language is None:
            language = notice.get("language", defaults.get("recording_notice_language", "pt-BR"))
        return self.recording_notice_text(language)

    def recording_notice_required(self):
        with self._lock:
            defaults = self._privacy_defaults
            granted = self._recording_consent_granted
        if not isinstance(defaults, dict):
            return False
        notice = defaults.get("recording_notice")
        enabled = notice.get("enabled", False) if isinstance(notice, dict) else defaults.get(
            "recording_notice_enabled", False,
        )
        return bool(enabled) and not granted

    def grant_recording_consent(self):
        with self._lock:
            self._recording_consent_granted = True
        return True

    def revoke_recording_consent(self):
        with self._lock:
            self._recording_consent_granted = False
        return True

    def _retention_lease_checker(self, _session_id=None):
        """Report capture/process/playback leases, excluding retention itself."""
        with self._lock:
            # ``_retention_active`` is deliberately absent from this result:
            # MeetingRetention calls back into this checker while it owns the
            # controller admission slot and must not self-block.
            if self._state in {"starting", "recording", "paused", "stopping", "postprocessing"}:
                return True
            if self._processing:
                return True
            if self._playback_active or (self._play_thread and self._play_thread.is_alive()):
                return True
        return False

    def _retention(self):
        with self._store_lock:
            if self._retention_service is None:
                from meeting_retention import MeetingRetention

                workspace_root = getattr(self.library, "home_root", None)
                reader = getattr(self.library, "read_retention_defaults", None)
                if callable(reader):
                    defaults = reader()
                else:
                    workspace = self.library.read_workspace()
                    defaults = workspace.get("retention_defaults", {})
                trash_days = defaults.get("trash_days", 30.0)
                self._retention_service = MeetingRetention(
                    self.store,
                    library=self.library,
                    workspace_root=workspace_root,
                    lease_checker=self._retention_lease_checker,
                    trash_retention_days=trash_days,
                )
            return self._retention_service

    def _begin_retention(self):
        with self._lock:
            if self._closed:
                raise RuntimeError("O controlador de reuniões está encerrado.")
            if self._retention_active:
                raise RuntimeError("Uma operação de retenção já está em andamento.")
            if self._state != "idle":
                raise RuntimeError("Aguarde a gravação terminar antes da retenção.")
            if self._processing:
                raise RuntimeError("Aguarde o processamento terminar antes da retenção.")
            if self._playback_active or (self._play_thread and self._play_thread.is_alive()):
                raise RuntimeError("Aguarde a reprodução terminar antes da retenção.")
            self._retention_active = True

    def _run_retention(self, operation):
        self._begin_retention()
        try:
            return operation(self._retention())
        finally:
            with self._lock:
                self._retention_active = False

    def retention_active(self):
        with self._lock:
            return self._retention_active

    def is_busy(self):
        """True while capture, processing, playback, or retention work is running."""
        return self._retention_lease_checker() or self.retention_active()

    @property
    def store(self):
        with self._store_lock:
            if self._store is None:
                if self._library is not None:
                    self._store = self._library.store
                else:
                    self._store = MeetingStore(self.root)
            return self._store

    @property
    def library(self):
        with self._store_lock:
            if self._library is None:
                workspace_root = os.path.dirname(os.path.abspath(self.root)) \
                    if os.path.basename(os.path.abspath(self.root)).casefold() == "meetings" else self.root
                self._library = MeetingLibrary(self.store, workspace_root=workspace_root)
            return self._library

    def snapshot(self):
        with self._lock:
            playback_position = self._playback_position
            if self._playback_active:
                elapsed = max(0.0, self._clock() - self._playback_started)
                # Playback is intentionally reported at controller-poll granularity.  The
                # native writer remains the owner of actual audio timing; this bounded value
                # is only used to select the visible transcript chunk.
                playback_position = max(self._playback_start, self._playback_start + elapsed)
                if elapsed >= PLAYBACK_PROGRESS_INTERVAL:
                    self._playback_position = playback_position
            return {"state": self._state, "session_id": self._session_id,
                    "error": self._error, "processing": self._processing,
                    "retention_active": self._retention_active,
                    "last_status": self._last_status, "partial": bool(self._source_errors),
                    "elapsed": max(0.0, time.monotonic() - self._started)
                    if self._state in {"starting", "recording", "paused", "stopping"} else self._elapsed,
                    "levels": dict(self._levels),
                    "previewing": self._previewing,
                    "waveforms": {track: list(values) for track, values in self._waveforms.items()},
                    "final_audio": self._output_path,
                    "postprocess": self._postprocess,
                    "playback": {
                        "active": self._playback_active,
                        "session_id": self._playback_session,
                        "track": self._playback_track,
                        "start": self._playback_start,
                        "position": playback_position,
                        "generation": self._playback_generation,
                        "error": self._playback_error,
                    }}

    def devices(self):
        return self.capture_factory().list_devices()

    def preview_sources(self, settings, seconds=3.0):
        """Measure selected sources without creating a meeting or writing audio."""
        if isinstance(settings, dict):
            settings = resolve_meeting_settings(settings)
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) \
                or not math.isfinite(seconds) or not 0 < seconds <= 10:
            raise ValueError("A duração do teste de áudio é inválida.")
        enabled = ("microphone", "system") if settings.sources == "both" else (settings.sources,)

        def work():
            token, capture, failure = None, None, None
            peaks = {track: 0.0 for track in enabled}
            errors = set()
            with self._lock:
                self._generation += 1
                generation = self._generation
                self._previewing = True
                self._levels = {"microphone": 0.0, "system": 0.0}
                for values in self._waveforms.values():
                    values.clear()
            try:
                token = self.voice.reserve_for_meeting()
                capture = self.capture_factory()
                capture.start(settings, generation)
                deadline = time.monotonic() + seconds
                stop_deadline = None
                while True:
                    if stop_deadline is None and (time.monotonic() >= deadline or self._cancel.is_set()):
                        capture.command("stop")
                        stop_deadline = time.monotonic() + 5
                    if stop_deadline is not None and time.monotonic() >= stop_deadline:
                        raise RuntimeError("O capturador não confirmou o fim do teste de áudio.")
                    item = capture.read_event(timeout=0.1)
                    if item is None:
                        continue
                    event, payload = item
                    if event["type"] == "stopped":
                        break
                    if event["type"] == "source_error" and event.get("track") in enabled:
                        errors.add(event["track"])
                    if (event["type"] == "gap" and event.get("track") in enabled
                            and any(word in str(event.get("reason", "")) for word in
                                    ("overflow", "invalid", "discontinuity", "timestamp_error"))):
                        errors.add(event["track"])
                    if event["type"] != "audio" or event.get("track") not in enabled:
                        continue
                    peak, envelope = _measure_audio(payload, event["channels"])
                    track = event["track"]
                    peaks[track] = max(peaks[track], peak)
                    with self._lock:
                        self._levels[track] = peak
                        self._waveforms[track].extend(envelope)
            except Exception as exc:
                failure = exc
            finally:
                live = False
                if capture is not None:
                    try:
                        capture.stop(force=failure is not None)
                    except Exception as exc:
                        live = getattr(exc, "resource_live", True)
                        failure = failure or exc
                with self._lock:
                    self._previewing = False
                    self._levels.update(peaks)
                    if live:
                        self._state = "unavailable"
                if token is not None and not live:
                    self.voice.release_meeting(token)
            if failure is not None:
                raise failure
            return {"enabled": enabled, "peaks": peaks, "errors": tuple(sorted(errors))}

        return self._file_work(work)

    def start(self, settings, title=""):
        if isinstance(settings, dict):
            settings = resolve_meeting_settings(settings)
        with self._lock:
            if self._closed or self._retention_active or self._processing or self._state != "idle":
                return False
            if self._play_thread and self._play_thread.is_alive():
                return False
            defaults = self._privacy_defaults
            notice = defaults.get("recording_notice") if isinstance(defaults, dict) else None
            notice_enabled = notice.get("enabled", False) if isinstance(notice, dict) else (
                defaults.get("recording_notice_enabled", False) if isinstance(defaults, dict) else False
            )
            if notice_enabled and not self._recording_consent_granted:
                self._error = "Confirme o aviso de gravação antes de iniciar."
                return False
            # Consent is one explicit acknowledgement for one recording start.
            self._recording_consent_granted = False
            title = initial_recording_title(title)
            self._generation += 1
            self._state, self._error = "starting", ""
            self._source_errors.clear()
            self._elapsed = 0.0
            self._output_path = ""
            self._postprocess = ""
            self._levels = {"microphone": 0.0, "system": 0.0}
            for values in self._waveforms.values():
                values.clear()
            self._started = time.monotonic()
            self._stop.clear()
            self._cancel.clear()
            self._commands = queue.Queue(maxsize=16)
            self._thread = threading.Thread(target=self._capture,
                                            args=(settings, title, self._generation), daemon=True)
            self._thread.start()
            return True

    def stop(self):
        with self._lock:
            if self._state not in {"starting", "recording", "paused", "stopping"}:
                return False
            self._stop.set()
            self._state = "stopping"
            return True

    def _enqueue(self, command):
        with self._lock:
            if self._state not in {"recording", "paused"}:
                return False
            try:
                self._commands.put_nowait(command)
            except queue.Full:
                return False
            return True

    def pause(self):
        return self._enqueue("pause")

    def resume(self):
        return self._enqueue("resume")

    def toggle(self, settings):
        # Hotkey path: only state checks and thread launch; no capture/disk/joins.
        with self._lock:
            active = self._state != "idle"
        return self.stop() if active else self.start(settings)

    def _capture(self, settings, title, generation):
        token, session, capture = None, None, None
        error, clean_stop, final_status = "", True, "failed"
        try:
            if settings.destination and (not os.path.isabs(settings.destination)
                                         or not os.path.isdir(settings.destination)):
                raise ValueError("A pasta padrão de gravações não existe ou não é absoluta.")
            token = self.voice.reserve_for_meeting()
            session_settings = settings.payload()
            # The output directory can contain a user name or client folder.
            # It is an app preference, not recording provenance.
            session_settings.pop("meeting_destination", None)
            session = self.store.begin(session_settings, title)
            with self._lock:
                self._session_id = session
            capture = self.capture_factory()
            with self._lock:
                self._started = time.monotonic()
            capture.start(settings, generation)
            with self._lock:
                if not self._stop.is_set():
                    self._state = "recording"
            sent_stop, deadline = False, None
            while True:
                if self._stop.is_set() and not sent_stop:
                    capture.command("stop")
                    sent_stop, deadline = True, time.monotonic() + 8
                if deadline is not None and time.monotonic() > deadline:
                    raise RuntimeError("O capturador não confirmou a parada; o áudio parcial foi preservado.")
                try:
                    command = self._commands.get_nowait()
                except queue.Empty:
                    command = None
                if command and not sent_stop:
                    capture.command(command)
                    self.store.add_event(session, {"type": command, "timestamp": time.monotonic() - self._started})
                    with self._lock:
                        self._state = "paused" if command == "pause" else "recording"
                item = capture.read_event(timeout=0.1)
                if item is None:
                    continue
                event, payload = item
                if event["type"] == "stopped":
                    self.store.add_event(session, {"type": "recording_stopped",
                        "timestamp": time.monotonic() - self._started,
                        "clock": "controller_monotonic"})
                    break
                if event["type"] == "audio":
                    self.store.append_audio(session, event, payload)
                    peak, envelope = _measure_audio(payload, event["channels"])
                    with self._lock:
                        self._levels[event["track"]] = peak
                        self._waveforms[event["track"]].extend(envelope)
                else:
                    self.store.add_event(session, event)
                    loss = event["type"] == "gap" and any(word in str(event.get("reason", ""))
                        for word in ("overflow", "invalid", "discontinuity", "timestamp_error"))
                    if event["type"] == "source_error" or loss:
                        with self._lock:
                            first_loss = event.get("track", "unknown") not in self._source_errors
                            self._source_errors.add(event.get("track", "unknown"))
                            detail = str(event.get("message") or event.get("reason") or "Uma fonte foi interrompida.")[:1024]
                            self._error = "Captura parcial: " + detail
                        if first_loss:
                            self.notify(self._error)
        except Exception as exc:
            # Never log native buffers, transcript contents, or helper diagnostics.
            error = str(exc)
        finally:
            if capture is not None:
                try:
                    capture.stop(force=bool(error))
                except Exception as exc:
                    clean_stop = not getattr(exc, "resource_live", True)
                    error = error or str(exc)
            if session is not None:
                try:
                    final_status = "failed" if error else "partial" if self._source_errors else "completed"
                    self.store.finish(session, final_status, error or self._error or None)
                    if not error and not self._closed:
                        self.store.begin_revision(session, settings.profile, settings.language, status="pending")
                    if not error:
                        self._queue_projection(session)
                except Exception:
                    error = error or "Não foi possível finalizar os metadados; a recuperação ocorrerá ao reabrir."
            if session is not None and not error and clean_stop and not self._closed:
                with self._lock:
                    self._state = "postprocessing"
                    self._processing = True
                    self._processing_session = session
                postprocess_errors, postprocess_resource_live = self._postprocess_recording(
                    session, title, settings)
                if postprocess_resource_live:
                    clean_stop = False
                if postprocess_errors:
                    error = "A gravação foi preservada, mas " + "; ".join(postprocess_errors)
            if token is not None and clean_stop:
                self.voice.release_meeting(token)
            with self._lock:
                self._error = error or self._error
                self._elapsed = max(0.0, time.monotonic() - self._started)
                self._last_status = final_status
                self._levels = {"microphone": 0.0, "system": 0.0}
                self._processing = False
                self._processing_session = None
                # Retain the reservation and block a new capture if teardown is unproven.
                self._state = "idle" if clean_stop else "unavailable"
            if error:
                self.notify(error)

    def _postprocess_recording(self, session_id, title, settings):
        """Create the final MP3, then run available local processing in order.

        New recordings automatically use installed models.  Legacy boolean
        switches remain compatibility fields, but do not disable processing
        when a model is available locally.
        """
        errors = []
        resource_live = False
        try:
            with self._lock:
                self._postprocess = "Gerando o áudio final"
            destination = _final_audio_path(self.root, settings, session_id, title)
            output = export_mixdown(
                self.store, session_id, destination,
                enhance_microphone=settings.voice_boost,
                cancel_event=self._cancel,
            )
            self.store.save_final_audio(session_id, output, settings.voice_boost)
            with self._lock:
                self._output_path = output
        except Exception as exc:
            errors.append("não foi possível gerar o áudio final: " + str(exc))

        transcription_profile = self._installed_voice_profile(settings.profile)
        transcription_ready = False
        if transcription_profile and not self._cancel.is_set():
            try:
                with self._lock:
                    self._postprocess = "Transcrevendo localmente"
                from meeting_transcription import transcribe_meeting
                from voice_catalog import default_language_for_profile
                transcribe_meeting(
                    self.store, session_id, transcription_profile,
                    default_language_for_profile(transcription_profile, settings.language),
                    self.voice.cache_dir, cancel_event=self._cancel,
                )
                if self._cancel.is_set():
                    errors.append("o processamento automático foi cancelado")
                else:
                    self._refine_automatic_title(session_id)
                    transcription_ready = True
                    self._queue_projection(session_id)
            except Exception as exc:
                errors.append("a transcrição automática falhou: " + str(exc))
                if getattr(exc, "resource_live", False):
                    resource_live = True

        summary_model = self._installed_summary_model(settings.summary_model)
        if summary_model and transcription_ready and not self._cancel.is_set():
            try:
                with self._lock:
                    self._postprocess = "Gerando o resumo local"
                from meeting_summary import summarize_meeting
                summarize_meeting(self.store, session_id, summary_model,
                                  cancel_event=self._cancel)
                self._queue_projection(session_id)
            except Exception as exc:
                errors.append("o resumo automático falhou: " + str(exc))
        with self._lock:
            self._postprocess = "Pós-processamento concluído" if not errors else "Pós-processamento parcial"
        return errors, resource_live

    def _installed_voice_profile(self, preferred):
        """Return the preferred installed ASR profile, then a stable fallback."""
        from voice_catalog import catalog_entry, selectable_catalog
        from voice_models import model_is_installed

        entries = []
        preferred_entry = catalog_entry(preferred)
        if preferred_entry is not None:
            entries.append(preferred_entry)
        entries.extend(entry for entry in selectable_catalog() if entry not in entries)
        for entry in entries:
            try:
                if model_is_installed(entry, self.voice.cache_dir):
                    return entry["profile"]
            except (OSError, TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _installed_summary_model(preferred):
        """Return the preferred installed summary model, then catalog order."""
        from summary_catalog import summary_catalog, summary_catalog_entry
        from summary_models import summary_model_is_installed

        entries = []
        preferred_entry = summary_catalog_entry(preferred)
        if preferred_entry is not None:
            entries.append(preferred_entry)
        entries.extend(entry for entry in summary_catalog() if entry not in entries)
        for entry in entries:
            try:
                if summary_model_is_installed(entry["id"]):
                    return entry["id"]
            except (OSError, TypeError, ValueError):
                continue
        return None

    def _queue_projection(self, session_id):
        """Queue disposable indexing without making capture/finalization depend on it."""
        try:
            return self.library.queue_index_session(session_id)
        except Exception:
            # Canonical metadata/transcript/audio already committed.  A missing,
            # locked, or unavailable index is repaired by a later reconcile.
            return None

    def list_sessions_page(self, *, limit=50, cursor=None, offset=0, query="", status="", **filters):
        """Read one bounded library page on the controller's IO worker."""
        return self.library.list_sessions_page(
            limit=limit, cursor=cursor, offset=offset, query=query, status=status, **filters,
        )

    list_sessions_cursor = list_sessions_page

    def list_sessions(self, offset=0, limit=50, query="", status="", **filters):
        # Compatibility wrapper retained for existing manager callers.  New
        # UI code consumes the keyset page seam above.
        return self.library.list_sessions(offset, limit, query, status=status, **filters)

    def search_library(self, query, *, limit=50, offset=0, **filters):
        return self.library.search(query, limit=limit, offset=offset, **filters)

    search = search_library

    def resolve_search_result(self, result):
        return self.library.resolve_search_result(result)

    def index_state(self):
        return self.library.index_state

    def list_collections(self, *, include_archived=True):
        return self.library.list_collections(include_archived=include_archived)

    def list_series(self, *, include_archived=True):
        return self.library.list_series(include_archived=include_archived)

    def save_collection(self, value, *, expected_generation):
        return self.library.save_collection(value, expected_generation=expected_generation)

    create_collection = save_collection
    update_collection = save_collection

    def save_series(self, value, *, expected_generation):
        return self.library.save_series(value, expected_generation=expected_generation)

    create_series = save_series
    update_series = save_series

    def assign_organization(self, session_id, *, collection_ids=None, tags=None, people=None,
                            series_id=_UNSET, expected_generation=_UNSET):
        expected = self._annotation_expected_generation(session_id, expected_generation)
        kwargs = {
            "collection_ids": collection_ids, "tags": tags, "people": people,
            "expected_generation": expected,
        }
        if series_id is not _UNSET:
            kwargs["series_id"] = series_id
        return self._annotation_result(session_id, self.library.assign_organization(session_id, **kwargs))

    def preview_organization_batch(self, session_ids, **changes):
        return self.library.preview_organization_batch(session_ids, **changes)

    def assign_organization_batch(self, session_ids, *, expected_generations, cancel_event=None, **changes):
        return self.library.assign_organization_batch(
            session_ids, expected_generations=expected_generations,
            cancel_event=cancel_event or self._cancel, **changes,
        )

    def rebuild_index(self, *, cancel_event=None, progress=None):
        """Rebuild the disposable catalog without blocking canonical reads."""
        if not self._index_rebuild_lock.acquire(blocking=False):
            raise RuntimeError("A reconstrução do índice já está em andamento.")
        event = cancel_event or self._index_rebuild_cancel
        if cancel_event is None:
            event.clear()
        with self._lock:
            self._index_rebuild_progress = {"done": 0, "total": None, "state": "rebuilding"}

        def report(done, total):
            with self._lock:
                self._index_rebuild_progress = {
                    "done": done, "total": total, "state": "rebuilding",
                }
            if progress is not None:
                progress(done, total)

        try:
            result = self.library.reconcile(cancel_event=event, progress=report)
            with self._lock:
                self._index_rebuild_progress = {
                    "done": result.get("sessions", 0) if isinstance(result, dict) else 0,
                    "total": result.get("sessions") if isinstance(result, dict) else None,
                    "state": result.get("state", "ready") if isinstance(result, dict) else "ready",
                }
            return result
        except Exception:
            with self._lock:
                self._index_rebuild_progress = {
                    "done": self._index_rebuild_progress.get("done", 0),
                    "total": self._index_rebuild_progress.get("total"),
                    "state": "cancelled" if event.is_set() else "failed",
                }
            raise
        finally:
            self._index_rebuild_lock.release()

    def cancel_rebuild_index(self):
        self._index_rebuild_cancel.set()
        return True

    def rebuild_progress(self):
        with self._lock:
            return dict(self._index_rebuild_progress)

    def get_session(self, session_id):
        metadata = self.library.get_session(session_id)
        generation = metadata.get("annotation_generation", 0)
        if isinstance(generation, int) and not isinstance(generation, bool) and generation >= 0:
            with self._lock:
                self._annotation_generations[session_id] = generation
        metadata.setdefault("annotation_generation", 0)
        return metadata

    def delete_session(self, session_id):
        """Move a completed meeting to recoverable app trash.

        The controller is the user-facing deletion seam. Direct canonical
        ``MeetingLibrary.delete`` fails closed; permanent purge has its own
        explicit method and confirmation token.
        """
        return self._run_retention(lambda retention: retention.trash_meeting(
            session_id, confirm=True,
        ))

    def retention_plan(self, session_id, policy=None, *, tracks=None, override=None):
        return self._run_retention(lambda retention: retention.plan(
            session_id, policy, tracks=tracks, override=override,
        ))

    plan_retention = retention_plan

    def apply_retention(self, plan, *, confirm=False, permanent=False):
        return self._run_retention(lambda retention: retention.apply(
            plan, confirm=confirm, permanent=permanent,
        ))

    def list_trash(self):
        return self._run_retention(lambda retention: retention.list_trash())

    def restore_session(self, session_id):
        return self._run_retention(lambda retention: retention.restore(session_id))

    restore_from_trash = restore_session

    def purge_session(self, session_id, *, confirm=False):
        return self._run_retention(lambda retention: retention.purge(
            session_id, confirm=confirm,
        ))

    permanently_delete_session = purge_session

    def empty_trash(self, *, confirm=False):
        return self._run_retention(lambda retention: retention.empty_trash(confirm=confirm))

    def recover_retention_operations(self):
        """Reconcile interrupted operations; never auto-purge expired trash."""
        return self._run_retention(lambda retention: retention.recover_operations())

    startup_recover_retention = recover_retention_operations

    def _raw_retention_policy(self, tracks=None, policy=None):
        from meeting_retention import RetentionPolicy

        if policy is None:
            defaults = self.library.read_retention_defaults()
            policy = defaults.get(
                "raw_audio",
                defaults.get("raw_audio_policy", defaults.get("raw_tracks")),
            )
            if tracks is None and defaults.get("raw_audio_tracks") is not None:
                tracks = defaults.get("raw_audio_tracks")
        if policy is None:
            policy = RetentionPolicy.raw_tracks(after_days=0, tracks=tracks or ())
        else:
            policy = RetentionPolicy.from_value(policy)
            if policy.mode == "keep":
                policy = RetentionPolicy.raw_tracks(
                    after_days=0 if policy.after_days is None else policy.after_days,
                    tracks=policy.tracks,
                    purge_after_days=policy.purge_after_days,
                )
            elif policy.mode != "raw_tracks":
                raise ValueError("A política selecionada não é de remoção de áudio raw.")
        if tracks is not None:
            policy = RetentionPolicy.raw_tracks(
                after_days=0 if policy.after_days is None else policy.after_days,
                tracks=tracks,
                purge_after_days=policy.purge_after_days,
            )
        return policy

    def plan_raw_tracks(self, session_id, tracks=None, *, policy=None):
        resolved = self._raw_retention_policy(tracks, policy)
        return self._run_retention(lambda retention: retention.plan(
            session_id, resolved, tracks=resolved.tracks,
        ))

    plan_raw_audio = plan_raw_tracks

    def apply_raw_tracks(self, plan, *, confirm=False):
        return self.apply_retention(plan, confirm=confirm)

    remove_raw_tracks = apply_raw_tracks

    def get_transcript(self, session_id, revision=None, offset=0, limit=TRANSCRIPT_LIMIT):
        """Return one bounded transcript window without retaining the full JSONL file."""
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("O deslocamento da transcrição é inválido.")
        if limit is None:
            limit = TRANSCRIPT_LIMIT
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("O limite da transcrição é inválido.")
        limit = min(limit, TRANSCRIPT_LIMIT)
        return list(itertools.islice(self.library.get_transcript(session_id, revision), offset, offset + limit))

    def get_transcript_preview(self, session_id, revision=None, style="full_text", max_chars=None):
        """Return a formatted transcript preview without changing canonical data."""
        from meeting_text import transcript_preview

        options = {"style": style}
        if max_chars is not None:
            options["max_chars"] = max_chars
        return transcript_preview(self.library.get_transcript(session_id, revision), **options)

    def get_transcript_page(self, session_id, revision=None, offset=0, limit=TRANSCRIPT_PAGE_SIZE):
        """Return a bounded page plus navigation metadata for the transcript workspace."""
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("O deslocamento da transcrição é inválido.")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("O limite da transcrição é inválido.")
        limit = min(limit, TRANSCRIPT_LIMIT)
        values = list(itertools.islice(
            self.library.get_transcript(session_id, revision), offset, offset + limit + 1,
        ))
        return {
            "segments": values[:limit],
            "offset": offset,
            "limit": limit,
            "has_previous": offset > 0,
            "has_more": len(values) > limit,
            "revision": revision,
        }

    def _annotation_expected_generation(self, session_id, expected_generation):
        if expected_generation is not _UNSET:
            return expected_generation
        with self._lock:
            cached = self._annotation_generations.get(session_id)
        if cached is not None:
            return cached
        current = self.library.read_annotations(session_id)
        generation = current.get("generation", 0)
        with self._lock:
            self._annotation_generations[session_id] = generation
        return generation

    def _annotation_result(self, session_id, result):
        generation = result.get("generation") if isinstance(result, dict) else None
        if isinstance(generation, int) and not isinstance(generation, bool) and generation >= 0:
            with self._lock:
                self._annotation_generations[session_id] = generation
        return result

    def get_annotations(self, session_id, revision=None, active_only=False):
        result = self.library.read_annotations(
            session_id, revision=revision, active_only=active_only,
        )
        return self._annotation_result(session_id, result)

    def create_speaker_label(self, session_id, value=None, *, expected_generation=_UNSET, **fields):
        expected = self._annotation_expected_generation(session_id, expected_generation)
        return self._annotation_result(session_id, self.library.create_speaker_label(
            session_id, value, expected_generation=expected, **fields,
        ))

    def add_speaker_label(self, session_id, revision, segment_id, label, *, expected_generation=_UNSET,
                          note="", label_id=None):
        expected = self._annotation_expected_generation(session_id, expected_generation)
        return self._annotation_result(session_id, self.library.add_speaker_label(
            session_id, revision, segment_id, label, expected_generation=expected,
            note=note, label_id=label_id,
        ))

    save_speaker_label = add_speaker_label

    def set_speaker_label(self, session_id, revision, segment_id, label, *, expected_generation=_UNSET,
                          note="", label_id=None):
        expected = self._annotation_expected_generation(session_id, expected_generation)
        return self._annotation_result(session_id, self.library.set_speaker_label(
            session_id, revision, segment_id, label, expected_generation=expected,
            note=note, label_id=label_id,
        ))

    def update_speaker_label(self, session_id, label_id, patch=None, *, expected_generation=_UNSET,
                             **fields):
        expected = self._annotation_expected_generation(session_id, expected_generation)
        return self._annotation_result(session_id, self.library.update_speaker_label(
            session_id, label_id, patch, expected_generation=expected, **fields,
        ))

    edit_speaker_label = update_speaker_label

    def delete_speaker_label(self, session_id, label_id, *, expected_generation=_UNSET):
        expected = self._annotation_expected_generation(session_id, expected_generation)
        return self._annotation_result(session_id, self.library.delete_speaker_label(
            session_id, label_id, expected_generation=expected,
        ))

    remove_speaker_label = delete_speaker_label

    def create_highlight(self, session_id, value=None, *, expected_generation=_UNSET, **fields):
        expected = self._annotation_expected_generation(session_id, expected_generation)
        return self._annotation_result(session_id, self.library.create_highlight(
            session_id, value, expected_generation=expected, **fields,
        ))

    def add_highlight(self, session_id, revision, start, end, track, segment_ids, *,
                      expected_generation=_UNSET, label="", note="", highlight_id=None):
        expected = self._annotation_expected_generation(session_id, expected_generation)
        return self._annotation_result(session_id, self.library.add_highlight(
            session_id, revision, start, end, track, segment_ids,
            expected_generation=expected, label=label, note=note, highlight_id=highlight_id,
        ))

    save_highlight = add_highlight

    def update_highlight(self, session_id, highlight_id, patch=None, *, expected_generation=_UNSET,
                         **fields):
        expected = self._annotation_expected_generation(session_id, expected_generation)
        return self._annotation_result(session_id, self.library.update_highlight(
            session_id, highlight_id, patch, expected_generation=expected, **fields,
        ))

    edit_highlight = update_highlight

    def delete_highlight(self, session_id, highlight_id, *, expected_generation=_UNSET):
        expected = self._annotation_expected_generation(session_id, expected_generation)
        return self._annotation_result(session_id, self.library.delete_highlight(
            session_id, highlight_id, expected_generation=expected,
        ))

    remove_highlight = delete_highlight

    def export_highlight_clip(self, session_id, highlight, path):
        from meeting_files import export_highlight_clip
        return self._file_work(lambda: export_highlight_clip(
            self.store, session_id, highlight, path, cancel_event=self._cancel,
        ), session_id=session_id)

    def rename_session(self, session_id, title, *, expected_generation=_UNSET):
        """Rename a recording without rewriting legacy notes or bookmarks."""
        expected = self._annotation_expected_generation(session_id, expected_generation)
        self._annotation_result(session_id, self.library.update_annotations(
            session_id, {"title": title}, expected_generation=expected,
        ))
        return True

    def update_notes(self, session_id, title, notes, bookmarks=None, expected_generation=_UNSET):
        fields = {"title": title, "notes": notes}
        if bookmarks is not None:
            fields["bookmarks"] = bookmarks
        expected = self._annotation_generations.get(session_id) if expected_generation is _UNSET else expected_generation
        kwargs = ({"expected_generation": expected}
                  if expected_generation is not _UNSET or expected is not None else {})
        result = self.library.update_annotations(session_id, fields, **kwargs)
        with self._lock:
            self._annotation_generations[session_id] = result["generation"]
        return True

    def update_summary(self, session_id, text, expected_generation=_UNSET):
        expected = self._annotation_generations.get(session_id) if expected_generation is _UNSET else expected_generation
        kwargs = ({"expected_generation": expected}
                  if expected_generation is not _UNSET or expected is not None else {})
        result = self.library.update_annotations(
            session_id, {"reviewed_summary": text}, **kwargs,
        )
        with self._lock:
            self._annotation_generations[session_id] = result["generation"]
        return True

    def _launch_processing(self, function, session_id=None):
        with self._lock:
            if self._closed or self._retention_active or self._processing or self._state != "idle":
                return False
            self._processing = True
            self._processing_session = session_id
            self._error = ""
            self._cancel.clear()

            def worker():
                try:
                    function()
                except Exception as exc:
                    with self._lock:
                        self._error = str(exc)
                    self.notify(str(exc))
                finally:
                    with self._lock:
                        self._processing = False
                        self._processing_session = None

            self._processing_thread = threading.Thread(target=worker, daemon=True)
            self._processing_thread.start()
            return True

    def transcribe(self, session_id, profile, language):
        def work():
            self._transcribe_and_summarize(session_id, profile, language)
        return self._launch_processing(work, session_id=session_id)

    def _transcribe_and_summarize(self, session_id, profile, language, preferred_summary=None):
        """Run installed-only transcription and summary on one processing lane."""
        from meeting_transcription import transcribe_meeting

        token = self.voice.reserve_for_meeting()
        release = True
        try:
            transcribe_meeting(
                self.store, session_id, profile, language,
                self.voice.cache_dir, cancel_event=self._cancel,
            )
            if self._cancel.is_set():
                return False
            self._refine_automatic_title(session_id)
            self._queue_projection(session_id)
            if preferred_summary is None:
                metadata = self.library.get_session(session_id)
                stored = metadata.get("settings") if isinstance(metadata, dict) else None
                preferred_summary = stored.get("meeting_summary_model") if isinstance(stored, dict) else None
            self._run_installed_summary(session_id, preferred_summary)
            return True
        except Exception as exc:
            if getattr(exc, "resource_live", False):
                release = False
                with self._lock:
                    self._state = "unavailable"
            raise
        finally:
            if release:
                self.voice.release_meeting(token)

    def _run_installed_summary(self, session_id, preferred=None):
        """Generate a summary only when a verified local model is installed."""
        model = self._installed_summary_model(preferred)
        if not model or self._cancel.is_set():
            return False
        from meeting_summary import summarize_meeting
        summarize_meeting(self.store, session_id, model, cancel_event=self._cancel)
        if not self._cancel.is_set():
            self._queue_projection(session_id)
        return True

    def _refine_automatic_title(self, session_id):
        metadata = self.library.get_session(session_id)
        if self.library.has_annotation_sidecar(session_id):
            # A sidecar may contain a human title. Automatic transcription must
            # never overwrite it, even when the generated title is better.
            return
        current = metadata.get("title", "")
        refined = refine_recording_title(
            current,
            session_id,
            self.library.get_transcript(session_id),
        )
        if refined != current:
            # Keep legacy bundles sidecar-free until a human mutation occurs.
            self.library.store.update(session_id, title=refined)

    def cancel_processing(self):
        self._cancel.set()

    def import_model(self, profile, path):
        from voice_models import import_local_model
        def work():
            token = self.voice.reserve_for_meeting()
            try:
                import_local_model(profile, path, self.voice.cache_dir, cancel_event=self._cancel)
            finally:
                self.voice.release_meeting(token)
        return self._launch_processing(work)

    def import_audio(self, path, settings):
        from meeting_files import import_audio
        def work():
            session_id = import_audio(self.store, path, settings, cancel_event=self._cancel)
            transcription_profile = self._installed_voice_profile(settings.profile)
            if transcription_profile and not self._cancel.is_set():
                from voice_catalog import default_language_for_profile
                self._transcribe_and_summarize(
                    session_id, transcription_profile,
                    default_language_for_profile(transcription_profile, settings.language),
                    settings.summary_model,
                )
            self._queue_projection(session_id)
            return session_id
        return self._file_work(work)

    def import_wav(self, path, settings):
        return self.import_audio(path, settings)

    def export(self, session_id, path, format="markdown"):
        return self._file_work(lambda: self.library.export(
            session_id, path, format, cancel_event=self._cancel,
        ), session_id=session_id)

    def export_transcript(self, session_id, path, *, style="full_text", revision=None):
        """Export one complete formatted transcript through the file worker."""
        def operation():
            from meeting_files import export_transcript
            return export_transcript(
                self.store, session_id, path, style=style, revision=revision,
                cancel_event=self._cancel,
            )
        return self._file_work(operation, session_id=session_id)

    def export_mixdown(self, session_id, path, enhance_microphone=False):
        return self._file_work(lambda: export_mixdown(
            self.store, session_id, path, enhance_microphone=enhance_microphone,
            cancel_event=self._cancel,
        ), session_id=session_id)

    def regenerate_final_audio(self, session_id):
        """Publish a new level-adjusted mix, preserving every previous audio file."""
        def work():
            metadata = self.store.get(session_id, include_events=False)
            microphone = metadata.get("tracks", {}).get("microphone")
            if (not isinstance(microphone, dict) or microphone.get("available") is False
                    or microphone.get("raw_removed")):
                raise ValueError("O áudio original do microfone não está disponível para ajustar o volume.")
            if metadata.get("status") == "recording":
                raise ValueError("Finalize a gravação antes de ajustar o volume.")
            with self._lock:
                self._postprocess = "Ajustando o volume do microfone"
            # Capture omits private destination settings from session metadata;
            # the previous final file still identifies the user's output folder.
            previous = metadata.get("final_audio") or {}
            previous_path = previous.get("path") if isinstance(previous, dict) else None
            stored_settings = metadata.get("settings") or {}
            folder = stored_settings.get("meeting_destination", "")
            if isinstance(previous_path, str) and os.path.isabs(previous_path):
                folder = str(Path(previous_path).parent)
            destination = _final_audio_path(
                self.root, resolve_meeting_settings({"meeting_destination": folder}), session_id,
                metadata.get("title", ""),
            )
            output = export_mixdown(
                self.store, session_id, destination,
                enhance_microphone=True, cancel_event=self._cancel,
            )
            self.store.save_final_audio(session_id, output, voice_boost=True)
            self._queue_projection(session_id)
            with self._lock:
                self._postprocess = "Áudio final ajustado; originais preservados"

        with self._lock:
            if self._playback_active or (self._play_thread and self._play_thread.is_alive()):
                return False
            return self._launch_processing(work, session_id=session_id)

    def _file_work(self, operation, session_id=None):
        # Caller is an IO worker; reserve admission without another nested thread.
        with self._lock:
            if (self._closed or self._retention_active or self._processing or self._state != "idle"
                    or self._playback_active or (self._play_thread and self._play_thread.is_alive())):
                raise RuntimeError("Aguarde a gravação, reprodução ou processamento terminar.")
            self._processing = True
            self._processing_session = session_id
            self._cancel.clear()
            self._file_done.clear()
        try:
            return operation()
        finally:
            with self._lock:
                self._processing = False
                self._processing_session = None
                self._file_done.set()

    def summarize(self, session_id, model):
        def work():
            token = self.voice.reserve_for_meeting()
            try:
                if not self._run_installed_summary(session_id, model):
                    raise ValueError("Nenhum modelo de resumo instalado; instale um modelo local antes de gerar o resumo.")
            finally:
                self.voice.release_meeting(token)
        return self._launch_processing(work, session_id=session_id)

    def _intelligence(self):
        """Build the installed-only intelligence seam on the IO worker."""
        from meeting_intelligence import MeetingIntelligence
        return MeetingIntelligence(self.store, library=self.library)

    def list_report_profiles(self, language=None):
        return self._intelligence().read_profiles(self.library, language=language)

    get_report_profiles = list_report_profiles

    def save_report_profile(self, profile, expected_generation=None):
        return self._intelligence().save_custom_profile(
            profile, self.library, expected_generation=expected_generation,
        )

    create_report_profile = save_report_profile
    update_report_profile = save_report_profile

    def set_report_profile_enabled(self, profile_id, enabled, expected_generation=None):
        return self._intelligence().set_custom_profile_enabled(
            profile_id, enabled, self.library, expected_generation=expected_generation,
        )

    def disable_report_profile(self, profile_id, expected_generation=None):
        return self.set_report_profile_enabled(
            profile_id, False, expected_generation=expected_generation,
        )

    def enable_report_profile(self, profile_id, expected_generation=None):
        return self.set_report_profile_enabled(
            profile_id, True, expected_generation=expected_generation,
        )

    def delete_report_profile(self, profile_id, expected_generation=None):
        return self._intelligence().delete_custom_profile(
            profile_id, self.library, expected_generation=expected_generation,
        )

    remove_report_profile = delete_report_profile

    def list_reports(self, session_id, include_legacy=True, limit=REPORT_HISTORY_LIMIT,
                     cancel_event=None):
        limit = _bounded_report_history_limit(limit)
        event = self._cancel if cancel_event is None else cancel_event
        reader = getattr(self.library, "list_report_metadata", None)
        if callable(reader):
            rows = reader(
                session_id,
                include_legacy=include_legacy,
                limit=limit,
                cancel_event=event,
            )
            result = []
            for row in rows or ():
                if len(result) >= limit:
                    break
                projection = _report_history_projection(row)
                if projection is not None:
                    result.append(projection)
            return result
        # Compatibility for older library adapters: project and cap before
        # returning anything to Tk rather than retaining full report bodies.
        reports = self.library.list_reports(session_id, include_legacy=include_legacy)
        result = []
        for report in reports or ():
            if event is not None and event.is_set():
                raise RuntimeError("A leitura do histórico de relatórios foi cancelada.")
            if len(result) >= limit:
                break
            projection = _report_history_projection(report)
            if projection is not None:
                result.append(projection)
        return result

    def get_report(self, session_id, report_id):
        return self.library.get_report(session_id, report_id)

    def review_report(self, session_id, report_id, sections, expected_generation=0):
        return self.library.review_report(
            session_id, report_id, sections, expected_generation=expected_generation,
        )

    def generate_report(self, session_id, model, *, profile=None, revision=None,
                        language=None):
        """Generate one immutable structured report off the GUI thread."""
        def work():
            token = self.voice.reserve_for_meeting()
            try:
                return self._intelligence().generate_report(
                    session_id, model, profile=profile, revision=revision,
                    language=language, cancel_event=self._cancel,
                )
            finally:
                self.voice.release_meeting(token)
        return self._file_work(work)

    def ask_this_meeting(self, session_id, question, model, *, revision=None,
                         revision_id=None, language=None, history=None):
        """Run a memory-only answer; nothing is saved until save_answer()."""
        def work():
            token = self.voice.reserve_for_meeting()
            try:
                result = self._intelligence().ask_this_meeting(
                    session_id, question, model, revision=revision,
                    revision_id=revision_id, language=language,
                    cancel_event=self._cancel, include_provenance=True, history=history,
                )
                if isinstance(result, dict):
                    result = dict(result)
                    provenance = result.get("_provenance")
                    selected_revision = revision if revision is not None else revision_id
                    if selected_revision is None and isinstance(provenance, dict):
                        selected_revision = provenance.get("revision")
                    if selected_revision is None:
                        metadata = self.library.get_session(session_id, include_events=False)
                        revisions = metadata.get("revisions", [])
                        selected_revision = (
                            revisions[-1].get("id") if revisions and isinstance(revisions[-1], dict)
                            else None
                        )
                    result["revision"] = selected_revision
                return result
            finally:
                self.voice.release_meeting(token)
        return self._file_work(work, session_id=session_id)

    def ask_across_meetings(self, question, model, *, filters=None, **kwargs):
        """Run bounded memory-only Q&A over the filtered meeting library."""
        def work():
            token = self.voice.reserve_for_meeting()
            try:
                return self._intelligence().ask_across_meetings(
                    question, model, filters=filters, cancel_event=self._cancel, **kwargs,
                )
            finally:
                self.voice.release_meeting(token)
        return self._file_work(work)

    ask_cross_meeting = ask_across_meetings
    ask_cross_meetings = ask_across_meetings

    def qa_mode(self):
        defaults = self.privacy_defaults()
        mode = defaults.get("qa_mode", "explicit_save")
        if mode not in {"memory_only", "explicit_save"}:
            raise ValueError("O modo de Q&A do workspace é inválido.")
        return mode

    def save_answer(self, session_id, answer, model, *, question="", revision=None,
                    revision_id=None):
        if self.qa_mode() == "memory_only":
            raise ValueError(
                "O modo de Q&A memory_only não permite salvar respostas; "
                "altere explicitamente a política do workspace para explicit_save."
            )
        normalized, selected_question, selected_revision, provenance = _normalize_saved_answer(
            answer, question=question, revision=revision, revision_id=revision_id,
        )
        def work():
            return self._intelligence().save_answer(
                session_id, normalized, model, question=selected_question,
                revision=selected_revision, provenance=provenance,
            )
        return self._file_work(work, session_id=session_id)

    def export_report(self, session_id, report_id, path, format="markdown", section=None):
        from meeting_files import export_report
        destination = Path(path).absolute()
        library_root = Path(self.library.meetings_root).absolute()
        try:
            inside_library = os.path.commonpath((str(library_root), str(destination))) == str(library_root)
        except ValueError:
            inside_library = False
        if inside_library:
            raise ValueError("Escolha uma pasta fora da biblioteca de reuniões para exportar o relatório.")
        return self._file_work(lambda: export_report(
            self.library.get_report(session_id, report_id), path, format=format, section=section,
            cancel_event=self._cancel,
        ), session_id=session_id)

    def play(self, session_id, track, start=0.0):
        from meeting_files import play_audio
        with self._lock:
            if self._closed or self._retention_active or self._state != "idle" or self._processing:
                return False
            if self._play_thread and self._play_thread.is_alive():
                return False
            self._play_stop.clear()
            self._playback_generation += 1
            generation = self._playback_generation
            self._playback_active = True
            self._playback_session = session_id
            self._playback_track = track
            self._playback_start = float(start)
            self._playback_position = float(start)
            self._playback_started = self._clock()
            self._playback_error = ""

            def work():
                done = threading.Event()
                error_holder = []

                def native_worker():
                    try:
                        play_audio(self.store, session_id, track, start, self._play_stop)
                    except Exception as exc:
                        error_holder.append(str(exc))
                    finally:
                        done.set()

                native = threading.Thread(target=native_worker, daemon=True,
                                          name="MeetingPlaybackAudio")
                native.start()
                # Keep progress publication bounded and worker-owned while the native
                # stream blocks in sounddevice.write().  Tk only observes snapshots.
                while not done.wait(PLAYBACK_PROGRESS_INTERVAL):
                    with self._lock:
                        if generation != self._playback_generation or self._closed:
                            continue
                        self._playback_position = max(
                            self._playback_start,
                            self._playback_start + max(0.0, self._clock() - self._playback_started),
                        )
                native.join()
                error = error_holder[0] if error_holder else ""
                with self._lock:
                    if generation != self._playback_generation or self._closed:
                        return
                    self._playback_active = False
                    self._playback_position = max(
                        self._playback_position,
                        self._playback_start + max(0.0, self._clock() - self._playback_started),
                    )
                    self._playback_error = error
                if error and not self._closed:
                    self.notify(error)
            self._play_thread = threading.Thread(target=work, daemon=True)
            self._play_thread.start()
            return True

    def stop_playback(self):
        self._play_stop.set()
        with self._lock:
            if self._playback_active:
                # Invalidate the worker's eventual completion update.  This keeps a
                # stopped or replaced playback from selecting a stale transcript row.
                self._playback_generation += 1
                self._playback_active = False
                self._playback_error = ""

    def seek_playback(self, session_id, track, start=0.0):
        """Replace playback from a new point after the old audio stream exits."""
        if (track not in {"microphone", "system", "final"} or isinstance(start, bool)
                or not isinstance(start, (int, float)) or not math.isfinite(start) or start < 0):
            raise ValueError("Escolha uma fonte e um instante de reprodução válido.")
        with self._lock:
            if self._closed or self._retention_active or self._state != "idle" or self._processing:
                return False
            worker = self._play_thread
        self.stop_playback()
        if worker and worker is not threading.current_thread():
            worker.join(12)
            if worker.is_alive():
                raise RuntimeError("A reprodução anterior ainda está encerrando.")
        return self.play(session_id, track, start=start)

    def shutdown(self):
        with self._lock:
            self._closed = True
        self.stop()
        self.cancel_processing()
        self.stop_playback()
        if not self._file_done.wait(12):
            raise RuntimeError("Uma operação de arquivo de reunião ainda está encerrando.")
        for worker in (self._thread, self._processing_thread, self._play_thread):
            if worker and worker is not threading.current_thread():
                worker.join(12)
                if worker.is_alive():
                    raise RuntimeError("Uma tarefa de reunião ainda está encerrando.")
        if self._library is not None:
            self._library.shutdown()
