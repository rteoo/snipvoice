"""Worker-owned meeting lifecycle; keyboard and Tk callbacks only enqueue work."""

import array
import itertools
import queue
import sys
import threading
import time

from meeting_audio import NativeCapture
from meeting_settings import resolve_meeting_settings
from meeting_store import MeetingStore


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
                    "levels": dict(self._levels)}

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
            self._generation += 1
            self._state, self._error = "starting", ""
            self._source_errors.clear()
            self._elapsed = 0.0
            self._started = time.monotonic()
            self._stop.clear()
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
        error, clean_stop = "", True
        try:
            token = self.voice.reserve_for_meeting()
            session = self.store.begin(settings.payload(), title)
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
                    with self._lock:
                        self._levels[event["track"]] = peak
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
                    status = "failed" if error else "partial" if self._source_errors else "completed"
                    self.store.finish(session, status, error or self._error or None)
                    if not error and not self._closed:
                        self.store.begin_revision(session, settings.profile, settings.language, status="pending")
                except Exception:
                    error = error or "Não foi possível finalizar os metadados; a recuperação ocorrerá ao reabrir."
            if token is not None and clean_stop:
                self.voice.release_meeting(token)
            with self._lock:
                self._error = error or self._error
                self._elapsed = max(0.0, time.monotonic() - self._started)
                self._last_status = "failed" if error else "partial" if self._source_errors else "completed"
                self._levels = {"microphone": 0.0, "system": 0.0}
                # Retain the reservation and block a new capture if teardown is unproven.
                self._state = "idle" if clean_stop else "unavailable"
            if error:
                self.notify(error)

    def list_sessions(self, offset=0, limit=50, query="", status=""):
        return self.store.list_sessions(offset, limit, query, status=status)

    def get_session(self, session_id):
        return self.store.get(session_id)

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

    def import_wav(self, path, settings):
        from meeting_files import import_wav
        return self._file_work(lambda: import_wav(self.store, path, settings, cancel_event=self._cancel))

    def export(self, session_id, path, format="markdown"):
        from meeting_files import export_meeting
        return self._file_work(lambda: export_meeting(self.store, session_id, path, format,
                                                    cancel_event=self._cancel))

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
