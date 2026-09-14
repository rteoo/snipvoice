"""Optional ASR backends. The production binding is transcribe.cpp.

The package is optional until a self-contained wheel exists. Tests inject a
fake backend. Missing native code leaves voice unavailable instead of crashing.
"""

import threading

from voice_catalog import (
    LANGUAGE_AUTO,
    LANGUAGE_EN_US,
    LANGUAGE_PT_BR,
    PROFILE_STREAMING,
    catalog_entry,
)


_MODEL_LANGUAGE_CODES = {
    LANGUAGE_PT_BR: "pt",
    LANGUAGE_EN_US: "en",
}


class VoiceRuntimeError(Exception):
    """User-visible inference or load failure."""


class AsrBackend:
    """Minimal contract one resident model must satisfy."""

    def available(self):
        return False

    def load(self, model_path, profile, language):
        raise VoiceRuntimeError("Backend de voz indisponível.")

    def unload(self):
        return None

    def is_loaded(self):
        return False

    def transcribe(self, pcm, cancel_event=None):
        raise VoiceRuntimeError("Backend de voz indisponível.")

    def cancel(self):
        """Interrupt an in-flight ``transcribe`` from another thread."""
        return None

    def start_stream(self):
        raise VoiceRuntimeError("Este perfil não faz transcrição contínua.")

    def feed(self, pcm_chunk):
        return ""

    def finalize_stream(self):
        return ""

    def supports_stream(self):
        return False


class FakeAsrBackend(AsrBackend):
    """Deterministic backend for tests. Never loads a real model."""

    def __init__(self, transcript="hello", partials=None):
        self.transcript = transcript
        self.partials = list(partials or ())
        self.loaded_path = None
        self.profile = None
        self.language = None
        self.transcribe_calls = []
        self.cancel_calls = 0
        self._cancel = threading.Event()
        self._stream = []

    def available(self):
        return True

    def load(self, model_path, profile, language):
        self.loaded_path = model_path
        self.profile = profile
        self.language = language
        self._cancel.clear()

    def unload(self):
        self.loaded_path = None
        self.profile = None
        self.language = None

    def is_loaded(self):
        return self.loaded_path is not None

    def transcribe(self, pcm, cancel_event=None):
        if self._cancelled(cancel_event):
            raise VoiceRuntimeError("Transcrição cancelada.")
        self.transcribe_calls.append(list(pcm) if pcm is not None else None)
        return self.transcript

    def cancel(self):
        self.cancel_calls += 1
        self._cancel.set()

    def _cancelled(self, cancel_event):
        return self._cancel.is_set() or (
            cancel_event is not None and cancel_event.is_set()
        )

    def supports_stream(self):
        return True

    def start_stream(self):
        self._stream = []

    def feed(self, pcm_chunk):
        self._stream.append(pcm_chunk)
        if self.partials:
            return self.partials[min(len(self._stream), len(self.partials)) - 1]
        return ""

    def finalize_stream(self):
        return self.transcript


class TranscribeCppBackend(AsrBackend):
    """Production backend. Import is lazy so the app starts without the wheel."""

    def __init__(self):
        self._model = None
        self._session = None
        self._stream = None
        self._module = None
        self._profile = None
        self._language = None
        self._state = threading.Condition(threading.Lock())
        self._active_operations = 0
        self._stream_operations = []
        self._stream_starting = False
        self._starting_streams = []
        self._pending_stream_closes = []
        self._stream_epoch = 0
        self._cancel_in_progress = 0
        self._unloading = False

    def available(self):
        return self._import() is not None

    def _import(self):
        if self._module is not None:
            return self._module
        try:
            import transcribe_cpp
        except Exception:
            return None
        self._module = transcribe_cpp
        return self._module

    def load(self, model_path, profile, language):
        module = self._import()
        if module is None:
            raise VoiceRuntimeError(
                "O runtime transcribe.cpp não está instalado neste aplicativo."
            )
        self.unload()
        with self._state:
            while self._unloading or self._cancel_in_progress:
                self._state.wait()
            self._begin_operation_locked()
        model = None
        session = None
        try:
            model = module.Model(model_path)
            session = model.session()
        except Exception as exc:
            self._close_resource(session)
            self._close_resource(model)
            raise VoiceRuntimeError(f"Falha ao carregar o modelo de voz: {exc}") from exc
        else:
            entry = catalog_entry(profile)
            model_language = (
                LANGUAGE_AUTO
                if entry and entry.get("language_hint") == "unsupported"
                else language
            )
            with self._state:
                self._model = model
                self._session = session
                self._profile = profile
                self._language = model_language
        finally:
            self._end_operation()

    def unload(self):
        with self._state:
            while self._unloading or self._active_operations or self._cancel_in_progress:
                self._state.wait()
            self._unloading = True
            self._stream_epoch += 1
            if self._stream is not None:
                self._append_pending_stream_locked(self._stream)
                self._stream = None
            streams = self._take_ready_streams_locked()
            session = self._session
            self._session = None
            model = self._model
            self._model = None
            self._profile = None
            self._language = None
        try:
            self._close_streams(streams)
            if session is not None:
                self._close_resource(session)
            if model is not None:
                self._close_resource(model)
        finally:
            with self._state:
                self._unloading = False
                self._state.notify_all()

    def is_loaded(self):
        with self._state:
            return self._session is not None

    def _language_kw(self):
        with self._state:
            language = self._language
        if language in (None, LANGUAGE_AUTO):
            return {}
        return {"language": _MODEL_LANGUAGE_CODES.get(language, language)}

    def cancel(self):
        with self._state:
            self._cancel_in_progress += 1
            self._stream_epoch += 1
            session = self._session
            self._append_pending_stream_locked(self._stream)
            self._stream = None
            for stream in self._starting_streams:
                self._append_pending_stream_locked(stream)
            streams = self._take_ready_streams_locked()
        self._close_streams(streams)
        try:
            cancel = getattr(session, "cancel", None) if session is not None else None
            if cancel is not None:
                cancel()
        except Exception:
            pass
        finally:
            with self._state:
                self._cancel_in_progress -= 1
                self._state.notify_all()
                streams = self._take_ready_streams_locked()
            self._close_streams(streams)

    def transcribe(self, pcm, cancel_event=None):
        with self._state:
            session = self._session
            if session is None or self._unloading:
                raise VoiceRuntimeError("Nenhum modelo de voz está carregado.")
            self._begin_operation_locked()
        if cancel_event is not None and cancel_event.is_set():
            try:
                self.cancel()
                raise VoiceRuntimeError("Transcrição cancelada.")
            finally:
                self._end_operation()
        done = threading.Event()

        def watch():
            while not done.wait(0.05):
                if cancel_event is not None and cancel_event.is_set():
                    self.cancel()
                    return

        watcher = None
        if cancel_event is not None:
            watcher = threading.Thread(
                target=watch, name="voice-native-cancel", daemon=True
            )
            watcher.start()
        try:
            try:
                result = session.run(pcm, **self._language_kw())
            except TypeError:
                result = session.run(pcm)
        except Exception as exc:
            raise VoiceRuntimeError(f"Falha na transcrição: {exc}") from exc
        finally:
            done.set()
            if watcher is not None:
                watcher.join(0.2)
            streams = self._end_operation()
            self._close_streams(streams)
        if cancel_event is not None and cancel_event.is_set():
            raise VoiceRuntimeError("Transcrição cancelada.")
        return _result_text(result)

    @staticmethod
    def _close_resource(resource):
        close = getattr(resource, "close", None)
        if close is None:
            return
        try:
            close()
        except Exception:
            pass

    @classmethod
    def _close_stream(cls, stream):
        close = getattr(stream, "close", None) or getattr(stream, "__exit__", None)
        if close is None:
            return
        try:
            if close == getattr(stream, "__exit__", None):
                close(None, None, None)
            else:
                close()
        except Exception:
            pass

    def _close_streams(self, streams):
        if not streams:
            return
        try:
            for stream in streams:
                self._close_stream(stream)
        finally:
            with self._state:
                self._active_operations -= len(streams)
                self._state.notify_all()

    def _append_pending_stream_locked(self, stream):
        if stream is not None and not any(
            pending is stream for pending in self._pending_stream_closes
        ):
            self._pending_stream_closes.append(stream)

    def _replace_stream_identity_locked(self, previous, current):
        if previous is None or previous is current:
            return
        self._starting_streams = [
            current if stream is previous else stream for stream in self._starting_streams
        ]
        self._pending_stream_closes = [
            current if stream is previous else stream
            for stream in self._pending_stream_closes
        ]
        self._stream_operations = [
            (current if stream is previous else stream, count)
            for stream, count in self._stream_operations
        ]

    def _take_ready_streams_locked(self):
        active = [stream for stream, count in self._stream_operations if count]
        ready = [
            stream
            for stream in self._pending_stream_closes
            if not any(active_stream is stream for active_stream in active)
        ]
        self._pending_stream_closes = [
            stream
            for stream in self._pending_stream_closes
            if any(active_stream is stream for active_stream in active)
        ]
        self._active_operations += len(ready)
        return ready

    def _begin_operation_locked(self, stream=None):
        self._active_operations += 1
        if stream is not None:
            for index, (active_stream, count) in enumerate(self._stream_operations):
                if active_stream is stream:
                    self._stream_operations[index] = (stream, count + 1)
                    break
            else:
                self._stream_operations.append((stream, 1))

    def _end_operation(self, stream=None):
        with self._state:
            self._active_operations -= 1
            if stream is not None:
                for index, (active_stream, count) in enumerate(self._stream_operations):
                    if active_stream is stream:
                        if count > 1:
                            self._stream_operations[index] = (stream, count - 1)
                        else:
                            self._stream_operations.pop(index)
                        break
            streams = self._take_ready_streams_locked()
            self._state.notify_all()
            return streams

    def supports_stream(self):
        with self._state:
            session = self._session
            profile = self._profile
        return profile == PROFILE_STREAMING and session is not None and hasattr(
            session, "stream"
        )

    def start_stream(self):
        with self._state:
            while self._cancel_in_progress:
                self._state.wait()
            session = self._session
            if (
                self._unloading
                or self._profile != PROFILE_STREAMING
                or session is None
                or not hasattr(session, "stream")
            ):
                raise VoiceRuntimeError("Este perfil não faz transcrição contínua.")
            if (
                self._active_operations
                or self._stream is not None
                or self._stream_starting
                or self._starting_streams
            ):
                raise VoiceRuntimeError("A transcrição contínua já está ativa.")
            self._stream_starting = True
            epoch = self._stream_epoch
            self._begin_operation_locked()
        stream = None
        entered_stream = None
        try:
            stream = session.stream()
            with self._state:
                if stream is not None:
                    self._starting_streams.append(stream)
                    self._begin_operation_locked(stream)
            enter = getattr(stream, "__enter__", None)
            entered_stream = enter() if enter is not None else stream
            if entered_stream is None:
                entered_stream = stream
            if entered_stream is not stream:
                with self._state:
                    self._replace_stream_identity_locked(stream, entered_stream)
            with self._state:
                stale = (
                    epoch != self._stream_epoch
                    or self._cancel_in_progress
                    or self._unloading
                    or session is not self._session
                )
                if stale:
                    self._append_pending_stream_locked(entered_stream)
                else:
                    self._stream = entered_stream
        except Exception:
            with self._state:
                self._append_pending_stream_locked(entered_stream or stream)
            raise
        finally:
            with self._state:
                self._stream_starting = False
                if stream is not None:
                    self._starting_streams = [
                        current
                        for current in self._starting_streams
                        if current is not stream and current is not entered_stream
                    ]
            operation_stream = entered_stream if entered_stream is not None else stream
            if operation_stream is not None:
                streams = self._end_operation(operation_stream)
                self._close_streams(streams)
            streams = self._end_operation()
            self._close_streams(streams)

    def feed(self, pcm_chunk):
        with self._state:
            stream = self._stream
            if stream is None or self._unloading:
                return ""
            self._begin_operation_locked(stream)
        text = ""
        try:
            stream.feed(pcm_chunk)
            text = stream.text()
        finally:
            streams = self._end_operation(stream)
            self._close_streams(streams)
        committed = getattr(text, "committed", None)
        tentative = getattr(text, "tentative", None)
        if committed is None and tentative is None:
            return str(text or "")
        return f"{committed or ''}{tentative or ''}"

    def finalize_stream(self):
        with self._state:
            stream = self._stream
            self._stream = None
            if stream is None:
                return ""
            self._append_pending_stream_locked(stream)
            self._begin_operation_locked(stream)
        try:
            finalize = getattr(stream, "finalize", None)
            if finalize is not None:
                finalize()
            text = stream.text()
            return _result_text(text)
        finally:
            streams = self._end_operation(stream)
            self._close_streams(streams)


def _result_text(result):
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    text = getattr(result, "text", None)
    if isinstance(text, str):
        return text
    committed = getattr(result, "committed", None)
    tentative = getattr(result, "tentative", None)
    if committed is not None or tentative is not None:
        return f"{committed or ''}{tentative or ''}"
    return str(result)


def create_backend():
    """Prefer transcribe.cpp; otherwise a backend that reports unavailable."""
    backend = TranscribeCppBackend()
    if backend.available():
        return backend
    return AsrBackend()
