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
from meeting_settings import resolve_meeting_settings
from meeting_store import MeetingStore
from meeting_titles import initial_recording_title, refine_recording_title


WAVEFORM_POINTS = 360
WAVEFORM_BLOCK_POINTS = 24
_INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


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
    def __init__(self, root, voice, notify=None, capture_factory=NativeCapture, store=None):
        self.root = root
        self.voice = voice
        self.notify = notify or (lambda message: None)
        self.capture_factory = capture_factory
        # Store initialization/recovery is lazy and runs on an IO worker.
        self._store = store
        self._store_lock = threading.Lock()
        self._lock = threading.RLock()
        self._thread = None
        self._processing_thread = None
        self._play_thread = None
        self._play_stop = threading.Event()
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

    @property
    def store(self):
        with self._store_lock:
            if self._store is None:
                self._store = MeetingStore(self.root)
            return self._store

    def snapshot(self):
        with self._lock:
            return {"state": self._state, "session_id": self._session_id,
                    "error": self._error, "processing": self._processing,
                    "last_status": self._last_status, "partial": bool(self._source_errors),
                    "elapsed": max(0.0, time.monotonic() - self._started)
                    if self._state in {"starting", "recording", "paused", "stopping"} else self._elapsed,
                    "levels": dict(self._levels),
                    "waveforms": {track: list(values) for track, values in self._waveforms.items()},
                    "final_audio": self._output_path,
                    "postprocess": self._postprocess}

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
            except Exception as exc:
                errors.append("o resumo automático falhou: " + str(exc))
        with self._lock:
            self._postprocess = "Pós-processamento concluído" if not errors else "Pós-processamento parcial"
        return errors, resource_live

    def list_sessions(self, offset=0, limit=50, query="", status=""):
        return self.store.list_sessions(offset, limit, query, status=status)

    def get_session(self, session_id):
        return self.store.get(session_id)

    def delete_session(self, session_id):
        return self._file_work(lambda: self.store.delete(session_id))

    def get_transcript(self, session_id):
        return list(itertools.islice(self.store.get_transcript(session_id), 500))

    def update_notes(self, session_id, title, notes, bookmarks=None):
        fields = {"title": title, "notes": notes}
        if bookmarks is not None:
            fields["bookmarks"] = bookmarks
        return self.store.update(session_id, **fields)

    def update_summary(self, session_id, text):
        return self.store.update(session_id, reviewed_summary=text)

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
        metadata = self.store.get(session_id)
        current = metadata.get("title", "")
        refined = refine_recording_title(
            current,
            session_id,
            self.store.get_transcript(session_id),
        )
        if refined != current:
            self.store.update(session_id, title=refined)

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
        return self._file_work(lambda: import_audio(self.store, path, settings, cancel_event=self._cancel))

    def import_wav(self, path, settings):
        return self.import_audio(path, settings)

    def export(self, session_id, path, format="markdown"):
        from meeting_files import export_meeting
        return self._file_work(lambda: export_meeting(self.store, session_id, path, format,
                                                    cancel_event=self._cancel))

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
            finally:
                self.voice.release_meeting(token)
        return self._launch_processing(work)

    def play(self, session_id, track, start=0.0):
        from meeting_files import play_audio
        with self._lock:
            if self._closed or self._state != "idle" or self._processing:
                return False
            if self._play_thread and self._play_thread.is_alive():
                return False
            self._play_stop.clear()

            def work():
                try:
                    play_audio(self.store, session_id, track, start, self._play_stop)
                except Exception as exc:
                    self.notify(str(exc))
            self._play_thread = threading.Thread(target=work, daemon=True)
            self._play_thread.start()
            return True

    def stop_playback(self):
        self._play_stop.set()

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
