"""Worker-owned meeting lifecycle; keyboard and Tk callbacks only enqueue work."""

import array
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
    candidate = destination / (stem + ".wav")
    # ceiling: a session will not probe an unbounded hostile destination.
    for suffix in range(2, 1002):
        if not candidate.exists():
            return candidate
        candidate = destination / f"{stem} ({suffix}).wav"
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
        # ceiling: the UI keeps only the latest 360 measured envelope points
        # per source, independent of recording duration.
        self._waveforms = {track: deque(maxlen=WAVEFORM_POINTS)
                           for track in ("microphone", "system")}
        self._output_path = ""
        self._postprocess = ""
        self._processing = False
        self._last_status = ""
        self._source_errors = set()
        self._annotation_generations = {}

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
                    "last_status": self._last_status, "partial": bool(self._source_errors),
                    "elapsed": max(0.0, time.monotonic() - self._started)
                    if self._state in {"starting", "recording", "paused", "stopping"} else self._elapsed,
                    "levels": dict(self._levels),
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

    def start(self, settings, title=""):
        if isinstance(settings, dict):
            settings = resolve_meeting_settings(settings)
        with self._lock:
            if self._closed or self._processing or self._state != "idle":
                return False
            if self._play_thread and self._play_thread.is_alive():
                return False
            title = initial_recording_title(title)
            self._generation += 1
            self._state, self._error = "starting", ""
            self._source_errors.clear()
            self._elapsed = 0.0
            self._output_path = ""
            self._postprocess = ""
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
                    values = array.array("f")
                    values.frombytes(payload)
                    if sys.byteorder != "little":
                        values.byteswap()
                    peak = min(1.0, max((abs(value) for value in values), default=0.0))
                    envelope = _amplitude_envelope(values, event["channels"])
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
                # Retain the reservation and block a new capture if teardown is unproven.
                self._state = "idle" if clean_stop else "unavailable"
            if error:
                self.notify(error)

    def _postprocess_recording(self, session_id, title, settings):
        """Create the final WAV, then run requested local processing in order."""
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

        transcription_ready = False
        if settings.auto_transcribe and not self._cancel.is_set():
            try:
                with self._lock:
                    self._postprocess = "Transcrevendo localmente"
                from meeting_transcription import transcribe_meeting
                transcribe_meeting(
                    self.store, session_id, settings.profile, settings.language,
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

        if settings.auto_summary and transcription_ready and not self._cancel.is_set():
            try:
                with self._lock:
                    self._postprocess = "Gerando o resumo local"
                from meeting_summary import summarize_meeting
                summarize_meeting(self.store, session_id, settings.summary_model,
                                  cancel_event=self._cancel)
                self._queue_projection(session_id)
            except Exception as exc:
                errors.append("o resumo automático falhou: " + str(exc))
        with self._lock:
            self._postprocess = "Pós-processamento concluído" if not errors else "Pós-processamento parcial"
        return errors, resource_live

    def _queue_projection(self, session_id):
        """Queue disposable indexing without making capture/finalization depend on it."""
        try:
            return self.library.queue_index_session(session_id)
        except Exception:
            # Canonical metadata/transcript/audio already committed.  A missing,
            # locked, or unavailable index is repaired by a later reconcile.
            return None

    def list_sessions(self, offset=0, limit=50, query="", status=""):
        return self.library.list_sessions(offset, limit, query, status=status)

    def get_session(self, session_id):
        metadata = self.library.get_session(session_id)
        generation = metadata.get("annotation_generation", 0)
        if isinstance(generation, int) and not isinstance(generation, bool) and generation >= 0:
            with self._lock:
                self._annotation_generations[session_id] = generation
        metadata.setdefault("annotation_generation", 0)
        return metadata

    def delete_session(self, session_id):
        return self._file_work(lambda: self.library.delete(session_id))

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
        ))

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

    def _launch_processing(self, function):
        with self._lock:
            if self._closed or self._processing or self._state != "idle":
                return False
            self._processing = True
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

            self._processing_thread = threading.Thread(target=worker, daemon=True)
            self._processing_thread.start()
            return True

    def transcribe(self, session_id, profile, language):
        def work():
            from meeting_transcription import transcribe_meeting
            token = self.voice.reserve_for_meeting()
            release = True
            try:
                transcribe_meeting(self.store, session_id, profile, language,
                                   self.voice.cache_dir, cancel_event=self._cancel)
                if not self._cancel.is_set():
                    self._refine_automatic_title(session_id)
                    self._queue_projection(session_id)
            except Exception as exc:
                if getattr(exc, "resource_live", False):
                    release = False
                    with self._lock:
                        self._state = "unavailable"
                raise
            finally:
                if release:
                    self.voice.release_meeting(token)
        return self._launch_processing(work)

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
            self._queue_projection(session_id)
            return session_id
        return self._file_work(work)

    def import_wav(self, path, settings):
        return self.import_audio(path, settings)

    def export(self, session_id, path, format="markdown"):
        return self._file_work(lambda: self.library.export(
            session_id, path, format, cancel_event=self._cancel,
        ))

    def export_mixdown(self, session_id, path, enhance_microphone=False):
        return self._file_work(lambda: export_mixdown(
            self.store, session_id, path, enhance_microphone=enhance_microphone,
            cancel_event=self._cancel,
        ))

    def _file_work(self, operation):
        # Caller is an IO worker; reserve admission without another nested thread.
        with self._lock:
            if self._closed or self._processing or self._state != "idle":
                raise RuntimeError("Aguarde a gravação ou o processamento terminar.")
            self._processing = True
            self._cancel.clear()
            self._file_done.clear()
        try:
            return operation()
        finally:
            with self._lock:
                self._processing = False
                self._file_done.set()

    def summarize(self, session_id, model):
        def work():
            from meeting_summary import summarize_meeting
            token = self.voice.reserve_for_meeting()
            try:
                summarize_meeting(self.store, session_id, model, cancel_event=self._cancel)
                self._queue_projection(session_id)
            finally:
                self.voice.release_meeting(token)
        return self._launch_processing(work)

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

    def list_reports(self, session_id, include_legacy=True, limit=REPORT_HISTORY_LIMIT):
        limit = _bounded_report_history_limit(limit)
        reader = getattr(self.library, "list_report_metadata", None)
        if callable(reader):
            rows = reader(
                session_id,
                include_legacy=include_legacy,
                limit=limit,
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
                         revision_id=None, language=None):
        """Run a memory-only answer; nothing is saved until save_answer()."""
        def work():
            token = self.voice.reserve_for_meeting()
            try:
                result = self._intelligence().ask_this_meeting(
                    session_id, question, model, revision=revision,
                    revision_id=revision_id, language=language,
                    cancel_event=self._cancel, include_provenance=True,
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
        return self._file_work(work)

    def save_answer(self, session_id, answer, model, *, question="", revision=None,
                    revision_id=None):
        normalized, selected_question, selected_revision, provenance = _normalize_saved_answer(
            answer, question=question, revision=revision, revision_id=revision_id,
        )
        def work():
            return self._intelligence().save_answer(
                session_id, normalized, model, question=selected_question,
                revision=selected_revision, provenance=provenance,
            )
        return self._file_work(work)

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
        ))

    def play(self, session_id, track, start=0.0):
        from meeting_files import play_audio
        with self._lock:
            if self._closed or self._state != "idle" or self._processing:
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
