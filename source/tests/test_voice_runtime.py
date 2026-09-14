import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from voice_catalog import PROFILE_STREAMING
from voice_runtime import (
    AsrBackend,
    FakeAsrBackend,
    TranscribeCppBackend,
    VoiceRuntimeError,
    create_backend,
)


class RuntimeTests(unittest.TestCase):
    def test_base_backend_is_unavailable(self):
        backend = AsrBackend()
        self.assertFalse(backend.available())
        with self.assertRaises(VoiceRuntimeError):
            backend.load("x.gguf", "balanced", "auto")

    def test_create_backend_without_wheel_is_unavailable(self):
        real_import = __import__

        def import_without_backend(name, *args, **kwargs):
            if name == "transcribe_cpp":
                raise ImportError("simulated missing optional backend")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=import_without_backend):
            backend = create_backend()
            self.assertFalse(backend.available())

    def test_fake_backend_roundtrip(self):
        backend = FakeAsrBackend(transcript="ok")
        backend.load("model.gguf", "balanced", "pt-BR")
        self.assertTrue(backend.is_loaded())
        self.assertEqual(backend.transcribe([0.0, 0.1]), "ok")
        backend.unload()
        self.assertFalse(backend.is_loaded())

    def test_fake_cancel(self):
        backend = FakeAsrBackend()
        cancel = type("E", (), {"is_set": lambda self: True})()
        with self.assertRaises(VoiceRuntimeError):
            backend.transcribe([0.0], cancel_event=cancel)

    def test_transcribe_cpp_cancel_interrupts_an_in_flight_run(self):
        started = threading.Event()
        session = mock.Mock()

        def run(pcm, **kwargs):
            started.set()
            time.sleep(0.25)
            return "late"

        session.run.side_effect = run
        backend = TranscribeCppBackend()
        backend._session = session
        cancel = threading.Event()
        errors = []

        def worker():
            try:
                backend.transcribe([0.0], cancel_event=cancel)
            except VoiceRuntimeError as exc:
                errors.append(str(exc))

        thread = threading.Thread(target=worker)
        thread.start()
        self.assertTrue(started.wait(1.0))
        cancel.set()
        thread.join(1.0)
        self.assertFalse(thread.is_alive())
        session.cancel.assert_called()
        self.assertTrue(errors)

    def test_transcribe_cpp_maps_ui_locales_to_model_language_codes(self):
        backend = TranscribeCppBackend()
        backend._profile = "balanced"
        backend._language = "pt-BR"
        self.assertEqual(backend._language_kw(), {"language": "pt"})
        backend._language = "en-US"
        self.assertEqual(backend._language_kw(), {"language": "en"})

    def test_transcribe_cpp_auto_omits_the_language_hint(self):
        backend = TranscribeCppBackend()
        backend._profile = "balanced"
        backend._language = "auto"
        self.assertEqual(backend._language_kw(), {})

    def test_cancel_closes_stream_after_an_inflight_feed_returns(self):
        feed_started = threading.Event()
        release_feed = threading.Event()

        class BlockingStream:
            def __init__(self):
                self.close_calls = 0

            def feed(self, chunk):
                del chunk
                feed_started.set()
                self.assert_true(release_feed.wait(1.0))

            @staticmethod
            def assert_true(value):
                if not value:
                    raise AssertionError("feed release gate timed out")

            @staticmethod
            def text():
                return ""

            def close(self):
                self.close_calls += 1

        stream = BlockingStream()
        session = mock.Mock()
        session.stream.return_value = stream
        backend = TranscribeCppBackend()
        backend._session = session
        backend._profile = PROFILE_STREAMING
        backend._model = mock.Mock()
        backend.start_stream()

        feed_thread = threading.Thread(target=lambda: backend.feed([0.1]))
        feed_thread.start()
        self.assertTrue(feed_started.wait(1.0))
        backend.cancel()
        self.assertIsNone(backend._stream)
        self.assertEqual(stream.close_calls, 0)

        unload_thread = threading.Thread(target=backend.unload)
        unload_thread.start()
        time.sleep(0.05)
        self.assertTrue(unload_thread.is_alive())
        session.close.assert_not_called()

        release_feed.set()
        feed_thread.join(1.0)
        unload_thread.join(1.0)
        self.assertFalse(feed_thread.is_alive())
        self.assertFalse(unload_thread.is_alive())
        self.assertEqual(stream.close_calls, 1)
        session.close.assert_called_once_with()

    def test_cancel_fences_a_stream_created_after_cancellation(self):
        stream_started = threading.Event()
        release_stream = threading.Event()

        class LateStream:
            def __init__(self):
                self.close_calls = 0

            def close(self):
                self.close_calls += 1

        stream = LateStream()
        session = mock.Mock()

        def create_stream():
            stream_started.set()
            self.assertTrue(release_stream.wait(1.0))
            return stream

        session.stream.side_effect = create_stream
        backend = TranscribeCppBackend()
        backend._session = session
        backend._profile = PROFILE_STREAMING

        start_thread = threading.Thread(target=backend.start_stream)
        start_thread.start()
        self.assertTrue(stream_started.wait(1.0))
        backend.cancel()
        release_stream.set()
        start_thread.join(1.0)

        self.assertFalse(start_thread.is_alive())
        self.assertIsNone(backend._stream)
        self.assertEqual(stream.close_calls, 1)

        next_stream = LateStream()
        session.stream.side_effect = None
        session.stream.return_value = next_stream
        backend.start_stream()
        self.assertIs(backend._stream, next_stream)
        backend.cancel()
        self.assertEqual(next_stream.close_calls, 1)

    def test_unload_waits_for_stream_close_before_closing_session(self):
        close_started = threading.Event()
        release_close = threading.Event()

        class BlockingCloseStream:
            def finalize(self):
                return None

            @staticmethod
            def text():
                return ""

            def close(self):
                close_started.set()
                if not release_close.wait(1.0):
                    raise AssertionError("close release gate timed out")

        stream = BlockingCloseStream()
        session = mock.Mock()
        backend = TranscribeCppBackend()
        backend._session = session
        backend._profile = PROFILE_STREAMING
        backend._stream = stream

        finalize_thread = threading.Thread(target=backend.finalize_stream)
        finalize_thread.start()
        self.assertTrue(close_started.wait(1.0))

        unload_thread = threading.Thread(target=backend.unload)
        unload_thread.start()
        time.sleep(0.05)
        self.assertTrue(unload_thread.is_alive())
        session.close.assert_not_called()

        release_close.set()
        finalize_thread.join(1.0)
        unload_thread.join(1.0)
        self.assertFalse(finalize_thread.is_alive())
        self.assertFalse(unload_thread.is_alive())
        session.close.assert_called_once_with()

    def test_unload_closes_an_active_stream_and_releases_close_tracking(self):
        class Stream:
            close_calls = 0

            def close(self):
                self.close_calls += 1

        stream = Stream()
        session = mock.Mock()
        model = mock.Mock()
        backend = TranscribeCppBackend()
        backend._session = session
        backend._model = model
        backend._profile = PROFILE_STREAMING
        backend._stream = stream

        backend.unload()
        backend.unload()

        self.assertEqual(stream.close_calls, 1)
        self.assertEqual(backend._active_operations, 0)
        session.close.assert_called_once_with()
        model.close.assert_called_once_with()

    def test_start_stream_reserves_admission_before_native_factory_returns(self):
        stream_started = threading.Event()
        release_stream = threading.Event()

        class Stream:
            close_calls = 0

            def close(self):
                self.close_calls += 1

        stream = Stream()
        session = mock.Mock()

        def create_stream():
            stream_started.set()
            if not release_stream.wait(1.0):
                raise AssertionError("stream release gate timed out")
            return stream

        session.stream.side_effect = create_stream
        backend = TranscribeCppBackend()
        backend._session = session
        backend._profile = PROFILE_STREAMING

        first_thread = threading.Thread(target=backend.start_stream)
        first_thread.start()
        self.assertTrue(stream_started.wait(1.0))

        second_errors = []

        def start_again():
            try:
                backend.start_stream()
            except VoiceRuntimeError as exc:
                second_errors.append(str(exc))

        second_thread = threading.Thread(target=start_again)
        second_thread.start()
        second_thread.join(1.0)
        self.assertFalse(second_thread.is_alive())
        self.assertEqual(len(second_errors), 1)
        self.assertEqual(session.stream.call_count, 1)

        release_stream.set()
        first_thread.join(1.0)
        self.assertFalse(first_thread.is_alive())
        self.assertIs(backend._stream, stream)
        backend.cancel()
        self.assertEqual(stream.close_calls, 1)

    def test_start_stream_rejects_while_previous_stream_close_is_blocked(self):
        close_started = threading.Event()
        release_close = threading.Event()

        class BlockingCloseStream:
            def finalize(self):
                return None

            @staticmethod
            def text():
                return ""

            def close(self):
                close_started.set()
                if not release_close.wait(1.0):
                    raise AssertionError("close release gate timed out")

        class NextStream:
            close_calls = 0

            def close(self):
                self.close_calls += 1

        first_stream = BlockingCloseStream()
        next_stream = NextStream()
        session = mock.Mock()
        session.stream.return_value = next_stream
        backend = TranscribeCppBackend()
        backend._session = session
        backend._profile = PROFILE_STREAMING
        backend._stream = first_stream

        finalize_thread = threading.Thread(target=backend.finalize_stream)
        finalize_thread.start()
        self.assertTrue(close_started.wait(1.0))

        start_errors = []

        def start_again():
            try:
                backend.start_stream()
            except VoiceRuntimeError as exc:
                start_errors.append(str(exc))

        start_thread = threading.Thread(target=start_again)
        start_thread.start()
        start_thread.join(1.0)
        self.assertFalse(start_thread.is_alive())
        self.assertEqual(len(start_errors), 1)
        session.stream.assert_not_called()

        release_close.set()
        finalize_thread.join(1.0)
        self.assertFalse(finalize_thread.is_alive())

        backend.start_stream()
        session.stream.assert_called_once_with()
        self.assertIs(backend._stream, next_stream)
        backend.cancel()
        self.assertEqual(next_stream.close_calls, 1)


if __name__ == "__main__":
    unittest.main()
