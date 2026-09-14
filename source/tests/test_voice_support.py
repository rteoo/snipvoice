import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from trigger_index import compile_trigger_index
from voice_audio import AudioCapture, CaptureIssue, CaptureResult, VoiceAudioError
from voice_catalog import LANGUAGE_AUTO, PROFILE_STREAMING
from voice_dispatch import (
    MODE_COMMAND,
    MODE_DICTATION,
    OUTCOME_FAILED,
    OUTCOME_INSERTED,
    OUTCOME_SECURE_INPUT,
    OUTCOME_TARGET_LOST,
    VoiceTarget,
)
from voice_runtime import FakeAsrBackend, VoiceRuntimeError
from voice_support import (
    STATE_IDLE,
    STATE_LOADING,
    STATE_RECORDING,
    STATE_ROUTING,
    STATE_TRANSCRIBING,
    STATE_UNAVAILABLE,
    VoiceController,
)


class InlineRunner:
    def start(self, fn, *args, name=None):
        fn(*args)


class ThreadRunner:
    def __init__(self):
        self.threads = []

    def start(self, fn, *args, name=None):
        thread = threading.Thread(target=fn, args=args, daemon=True, name=name)
        self.threads.append(thread)
        thread.start()
        return thread


class FakeCapture:
    def __init__(self, samples=None, overflow=False, error=None):
        self.samples = list(samples or [0.1, 0.2])
        self.overflow = overflow
        self.error = error
        self.started = False
        self.stop_calls = 0
        self.issue = None
        self._queue = _Queue(self.samples)

    def start(self):
        if self.error:
            raise self.error
        self.started = True

    def stop(self):
        self.stop_calls += 1
        self.started = False
        issue = self.issue
        if issue is None and self.overflow:
            issue = CaptureIssue.DURATION_LIMIT
        return CaptureResult(
            self.samples,
            issue=issue,
            message=("capture failed" if issue is not None else None),
            duration_seconds=len(self.samples) / 16000,
        )

    def read_chunk(self, timeout=0.1):
        return self._queue.get(timeout=timeout)


class _Queue:
    def __init__(self, items):
        self.items = list(items)

    def get(self, timeout=0.1):
        if not self.items:
            raise Exception("empty")
        return self.items.pop(0)


class _ChunkCapture:
    def __init__(self, chunks):
        self._chunks = _Queue(chunks)

    def read_chunk(self, timeout=0):
        return self._chunks.get(timeout=timeout)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.inserted = []
        self.expanded = []
        self.notify = mock.Mock()
        self.logger = mock.Mock()
        self.backend = FakeAsrBackend(transcript="hello world")
        self.capture = FakeCapture()
        self.controller = VoiceController(
            {"voice_enabled": False},
            task_runner=InlineRunner(),
            insert_text=lambda text: self.inserted.append(text) or True,
            expand_trigger=lambda trigger: self.expanded.append(trigger) or True,
            notify=self.notify,
            logger=self.logger,
            capture_target=lambda: VoiceTarget("window", handle=1),
            restore_target=lambda target: True,
            secure_input_blocks=lambda: False,
            backend=self.backend,
            capture_factory=lambda: self.capture,
            cache_dir=self.tmp,
            download=lambda entry, cache_dir, progress=None, cancel_event=None: os.path.join(
                self.tmp, "model.gguf"
            ),
        )
        self.controller.bind_library(
            lambda: {"xadds": "hi"},
            lambda: compile_trigger_index({"xadds": "hi"}, set()),
        )

    def _ready(self):
        with mock.patch("voice_support.model_is_installed", return_value=True), \
                mock.patch("voice_support.installed_model_path", return_value="model.gguf"), \
                mock.patch.object(self.controller, "_start_monitor"):
            self.controller.enable()
        self.assertEqual(self.controller.state, STATE_IDLE)

    def test_defaults_off(self):
        self.assertFalse(self.controller.enabled)
        self.assertEqual(self.controller.state, STATE_UNAVAILABLE)

    def test_capture_runtime_unavailable_never_enters_ready(self):
        self.controller._capture_available = lambda: False
        self.controller._capture_factory = AudioCapture

        self.controller.enable()

        self.assertEqual(self.controller.state, STATE_UNAVAILABLE)
        self.assertFalse(self.controller._provider.is_ready())
        self.notify.assert_called_with(
            "A captura de áudio não está disponível neste aplicativo.",
            key="voice-load",
        )

    def test_hotkey_while_loading_is_ignored(self):
        self.controller._state = "loading"
        self.assertFalse(self.controller.handle_hotkey_press(MODE_DICTATION))

    def test_dictation_inserts_literally(self):
        self._ready()
        self.backend.transcript = "xadds"
        self.assertTrue(self.controller.handle_hotkey_press(MODE_DICTATION))
        self.controller.handle_hotkey_release(MODE_DICTATION)
        self.assertEqual(self.inserted, ["xadds"])
        self.assertEqual(self.expanded, [])
        self.assertEqual(self.controller.last_outcome, OUTCOME_INSERTED)
        entry = self.controller.history_entries()[0]
        self.assertEqual(entry["status"], "completed")
        self.assertEqual(entry["transcript"], "xadds")
        self.assertEqual(entry["provider"], "local")

    def test_failed_transcription_stays_retryable_in_history(self):
        self._ready()
        self.backend.transcribe = mock.Mock(side_effect=VoiceRuntimeError("falhou"))

        self.controller.handle_hotkey_press(MODE_DICTATION)
        self.controller.handle_hotkey_release(MODE_DICTATION)

        entry = self.controller.history_entries()[0]
        self.assertEqual(entry["status"], "failed")
        self.assertTrue(self.controller._history.is_retryable(entry["id"]))

    def test_history_retry_copies_recovered_text_without_pasting(self):
        self._ready()
        recording = self.controller._history.begin(
            mode=MODE_DICTATION,
            provider="local",
            profile="balanced",
            language="pt-BR",
            target_kind="window",
        )
        recording.finish_capture([0.1, 0.2])
        self.controller._history.fail(recording.record_id, "offline")
        self.controller.settings.voice_replacements = {"Queen": "Qwen"}
        self.backend.transcript = "texto Queen recuperado"

        with mock.patch("voice_support.Clipboard.set_content", return_value=True) as copied:
            self.assertTrue(self.controller.retry_history(recording.record_id))

        self.assertEqual(self.inserted, [])
        copied.assert_called_once_with("texto Qwen recuperado")
        entry = self.controller.history_entry(recording.record_id)
        self.assertEqual(entry["status"], "completed")
        self.assertEqual(entry["outcome"], "recovered")
        self.assertEqual(entry["raw_transcript"], "texto Queen recuperado")

    def test_cancelled_history_retry_is_not_mislabeled_as_failed(self):
        self._ready()
        recording = self.controller._history.begin(
            mode=MODE_DICTATION,
            provider="local",
            profile="balanced",
            language="pt-BR",
            target_kind="window",
        )
        recording.finish_capture([0.1])
        self.controller._history.fail(recording.record_id, "offline")
        with self.controller._lock:
            self.controller._state = STATE_TRANSCRIBING
            self.controller._retry_record_id = recording.record_id
            generation = self.controller._session_generation
        self.controller.cancel()

        self.controller._retry_history_worker(
            generation,
            recording.record_id,
        )

        entry = self.controller.history_entry(recording.record_id)
        self.assertEqual(entry["status"], "cancelled")

    def test_release_during_capture_start_cancels_startup(self):
        self._ready()
        entered = threading.Event()
        release = threading.Event()
        test_case = self

        class BlockingCapture(FakeCapture):
            def start(self):
                entered.set()
                test_case.assertTrue(release.wait(1.0))
                super().start()

        capture = BlockingCapture()
        self.controller._capture_factory = lambda: capture
        result = []
        press = threading.Thread(
            target=lambda: result.append(
                self.controller.handle_hotkey_press(MODE_DICTATION)
            ),
            daemon=True,
        )
        press.start()
        self.assertTrue(entered.wait(1.0))
        self.assertTrue(self.controller.handle_hotkey_release(MODE_DICTATION))
        self.assertEqual(self.controller.state, STATE_IDLE)
        self.assertFalse(self.controller.handle_hotkey_press(MODE_DICTATION))
        release.set()
        press.join(1.0)

        self.assertFalse(press.is_alive())
        self.assertEqual(result, [False])
        self.assertEqual(self.controller.state, STATE_IDLE)
        self.assertGreaterEqual(capture.stop_calls, 1)
        self.assertEqual(self.controller.history_entries()[0]["status"], "cancelled")

    def test_concurrent_presses_create_only_one_capture(self):
        self._ready()
        entered = threading.Event()
        release = threading.Event()
        captures = []
        test_case = self

        class BlockingCapture(FakeCapture):
            def start(self):
                if not entered.is_set():
                    entered.set()
                    test_case.assertTrue(release.wait(1.0))
                super().start()

        def factory():
            capture = BlockingCapture()
            captures.append(capture)
            return capture

        self.controller._capture_factory = factory
        results = []
        first = threading.Thread(
            target=lambda: results.append(
                self.controller.handle_hotkey_press(MODE_DICTATION)
            ),
            daemon=True,
        )
        second = threading.Thread(
            target=lambda: results.append(
                self.controller.handle_hotkey_press(MODE_DICTATION)
            ),
            daemon=True,
        )
        first.start()
        self.assertTrue(entered.wait(1.0))
        second.start()
        second.join(1.0)
        self.assertFalse(second.is_alive())
        release.set()
        first.join(1.0)

        self.assertFalse(first.is_alive())
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(len(captures), 1)
        self.assertEqual(self.controller.state, STATE_RECORDING)
        self.controller.cancel()

    def test_cancel_during_target_restore_blocks_late_insert(self):
        self._ready()
        runner = ThreadRunner()
        entered = threading.Event()
        release = threading.Event()

        def restore_target(target):
            del target
            entered.set()
            self.assertTrue(release.wait(1.0))
            return True

        self.controller.task_runner = runner
        self.controller._restore_target = restore_target
        self.assertTrue(self.controller.handle_hotkey_press(MODE_DICTATION))
        self.assertTrue(self.controller.handle_hotkey_release(MODE_DICTATION))
        self.assertTrue(entered.wait(1.0))
        self.controller.cancel()
        self.assertEqual(self.controller.state, STATE_IDLE)
        self.assertEqual(self.inserted, [])
        release.set()
        for thread in runner.threads:
            thread.join(1.0)

        self.assertEqual(self.inserted, [])
        self.assertFalse(any(thread.is_alive() for thread in runner.threads))
        self.assertEqual(self.controller.last_outcome, "cancelled")
        self.assertEqual(self.controller.history_entries()[0]["status"], "cancelled")

    def test_language_switch_fences_stale_history_retry(self):
        self._ready()
        recording = self.controller._history.begin(
            mode=MODE_DICTATION,
            provider="local",
            profile="balanced",
            language="pt-BR",
            target_kind="window",
        )
        recording.finish_capture([0.1, 0.2])
        self.controller._history.fail(recording.record_id, "offline")
        entered = threading.Event()
        release = threading.Event()
        copied = []

        def slow_transcribe(pcm, cancel_event=None):
            del pcm, cancel_event
            entered.set()
            self.assertTrue(release.wait(1.0))
            return "stale retry"

        self.backend.transcribe = slow_transcribe
        self.controller._leave_on_clipboard = lambda text: copied.append(text) or True
        runner = ThreadRunner()
        self.controller.task_runner = runner
        self.assertTrue(self.controller.retry_history(recording.record_id))
        self.assertTrue(entered.wait(1.0))

        with mock.patch("voice_support._SHUTDOWN_JOIN_SECONDS", 0.05), \
                mock.patch("voice_support.model_is_installed", return_value=True), \
                mock.patch("voice_support.installed_model_path", return_value="model.gguf"), \
                mock.patch.object(self.controller, "_start_monitor"):
            self.controller.set_language("en-US")
            deadline = time.monotonic() + 1.0
            while self.controller.state != STATE_IDLE and time.monotonic() < deadline:
                time.sleep(0.01)

        self.assertEqual(self.controller.history_entry(recording.record_id)["status"], "cancelled")
        release.set()
        for thread in runner.threads:
            thread.join(1.0)

        self.assertFalse(any(thread.is_alive() for thread in runner.threads))
        self.assertEqual(copied, [])
        self.assertEqual(
            self.controller.history_entry(recording.record_id)["status"],
            "cancelled",
        )

    def test_old_retry_cannot_cancel_a_new_retry_or_later_session(self):
        self._ready()
        recording = self.controller._history.begin(
            mode=MODE_DICTATION,
            provider="local",
            profile="balanced",
            language="pt-BR",
            target_kind="window",
        )
        recording.finish_capture([0.1, 0.2])
        self.controller._history.fail(recording.record_id, "offline")
        entered = threading.Event()
        release = threading.Event()
        copied = []
        calls = []

        def transcribe(pcm, cancel_event=None):
            del pcm, cancel_event
            calls.append(len(calls) + 1)
            if len(calls) == 1:
                entered.set()
                self.assertTrue(release.wait(1.0))
                return "stale retry"
            return "fresh retry"

        self.backend.transcribe = transcribe
        self.controller._leave_on_clipboard = lambda text: copied.append(text) or True
        runner = ThreadRunner()
        self.controller.task_runner = runner
        self.assertTrue(self.controller.retry_history(recording.record_id))
        self.assertTrue(entered.wait(1.0))
        self.controller.cancel()
        self.controller._history.fail(recording.record_id, "retry again")

        self.assertTrue(self.controller.retry_history(recording.record_id))
        second_retry = runner.threads[-1]
        second_retry.join(1.0)
        self.assertFalse(second_retry.is_alive())
        self.assertEqual(copied, ["fresh retry"])
        self.assertEqual(
            self.controller.history_entry(recording.record_id)["status"],
            "completed",
        )

        self.assertTrue(self.controller.handle_hotkey_press(MODE_DICTATION))
        self.controller.cancel()
        release.set()
        for thread in runner.threads:
            thread.join(1.0)

        self.assertFalse(any(thread.is_alive() for thread in runner.threads))
        self.assertEqual(copied, ["fresh retry"])
        self.assertEqual(
            self.controller.history_entry(recording.record_id)["status"],
            "completed",
        )

    def test_retry_commit_survives_cancel_during_clipboard_copy(self):
        self._ready()
        recording = self.controller._history.begin(
            mode=MODE_DICTATION,
            provider="local",
            profile="balanced",
            language="pt-BR",
            target_kind="window",
        )
        recording.finish_capture([0.1, 0.2])
        self.controller._history.fail(recording.record_id, "offline")
        entered = threading.Event()
        release = threading.Event()

        def blocked_copy(text):
            del text
            entered.set()
            self.assertTrue(release.wait(1.0))
            return True

        self.backend.transcript = "recovered"
        self.controller._leave_on_clipboard = blocked_copy
        runner = ThreadRunner()
        self.controller.task_runner = runner
        self.assertTrue(self.controller.retry_history(recording.record_id))
        self.assertTrue(entered.wait(1.0))

        self.controller.cancel()
        self.assertEqual(self.controller.state, STATE_IDLE)
        self.assertEqual(
            self.controller.history_entry(recording.record_id)["status"],
            "completed",
        )
        release.set()
        for thread in runner.threads:
            thread.join(1.0)
        self.assertFalse(any(thread.is_alive() for thread in runner.threads))
        self.assertEqual(
            self.controller.history_entry(recording.record_id)["status"],
            "completed",
        )

    def test_cancel_serializes_before_a_new_retry_of_the_same_record(self):
        self._ready()
        recording = self.controller._history.begin(
            mode=MODE_DICTATION,
            provider="local",
            profile="balanced",
            language="pt-BR",
            target_kind="window",
        )
        recording.finish_capture([0.1, 0.2])
        self.controller._history.fail(recording.record_id, "offline")
        with self.controller._lock:
            self.controller._state = STATE_TRANSCRIBING
            self.controller._retry_record_id = recording.record_id
            self.controller._retry_generation = self.controller._session_generation

        entered = threading.Event()
        release = threading.Event()
        original_cancel = self.controller._history.cancel

        def blocked_cancel(record_id):
            entered.set()
            self.assertTrue(release.wait(1.0))
            return original_cancel(record_id)

        self.controller._history.cancel = blocked_cancel
        cancelling = threading.Thread(target=self.controller.cancel, daemon=True)
        cancelling.start()
        self.assertTrue(entered.wait(1.0))

        retry_result = []
        retrying = threading.Thread(
            target=lambda: retry_result.append(
                self.controller.retry_history(recording.record_id)
            ),
            daemon=True,
        )
        retrying.start()
        retrying.join(0.05)
        self.assertTrue(retrying.is_alive())
        release.set()
        cancelling.join(1.0)
        retrying.join(1.0)

        self.assertFalse(cancelling.is_alive())
        self.assertFalse(retrying.is_alive())
        self.assertEqual(retry_result, [False])
        self.assertEqual(
            self.controller.history_entry(recording.record_id)["status"],
            "cancelled",
        )

    def test_completion_history_write_serializes_cancel(self):
        self._ready()
        entered = threading.Event()
        release = threading.Event()
        original_complete = self.controller._history.complete

        def blocked_complete(*args):
            entered.set()
            self.assertTrue(release.wait(1.0))
            return original_complete(*args)

        self.controller._history.complete = blocked_complete
        runner = ThreadRunner()
        self.controller.task_runner = runner
        self.assertTrue(self.controller.handle_hotkey_press(MODE_DICTATION))
        self.assertTrue(self.controller.handle_hotkey_release(MODE_DICTATION))
        self.assertTrue(entered.wait(1.0))

        cancel_thread = threading.Thread(target=self.controller.cancel, daemon=True)
        cancel_thread.start()
        cancel_thread.join(0.05)
        self.assertTrue(cancel_thread.is_alive())
        release.set()
        cancel_thread.join(1.0)
        for thread in runner.threads:
            thread.join(1.0)

        self.assertFalse(cancel_thread.is_alive())
        self.assertFalse(any(thread.is_alive() for thread in runner.threads))
        self.assertEqual(self.controller.state, STATE_IDLE)
        self.assertEqual(self.controller.history_entries()[0]["status"], "completed")

    def test_clipboard_exception_reports_that_dictation_was_not_recovered(self):
        self._ready()
        self.controller._insert_text = lambda text: False
        with mock.patch("voice_support.Clipboard.set_content", side_effect=OSError("busy")):
            self.controller.handle_hotkey_press(MODE_DICTATION)
            self.controller.handle_hotkey_release(MODE_DICTATION)

        self.assertEqual(self.controller.last_outcome, OUTCOME_FAILED)
        self.notify.assert_called_with(
            "Não foi possível inserir o texto de voz nem copiá-lo "
            "para a área de transferência.",
            key="voice-insert",
        )
        self.logger.warning.assert_called_once()

    def test_failed_insertion_reports_successful_clipboard_recovery(self):
        self._ready()
        self.controller._insert_text = lambda text: False
        with mock.patch("voice_support.Clipboard.set_content", return_value=True):
            self.controller.handle_hotkey_press(MODE_DICTATION)
            self.controller.handle_hotkey_release(MODE_DICTATION)

        self.assertEqual(self.controller.last_outcome, OUTCOME_FAILED)
        self.notify.assert_called_with(
            "Não foi possível inserir o texto de voz automaticamente. "
            "Ele está na área de transferência.",
            key="voice-insert",
        )

    def test_dispatch_exception_marks_history_failed_and_returns_to_idle(self):
        self._ready()
        self.controller._insert_text = mock.Mock(
            side_effect=RuntimeError("insert exploded")
        )

        self.assertTrue(self.controller.handle_hotkey_press(MODE_DICTATION))
        self.assertTrue(self.controller.handle_hotkey_release(MODE_DICTATION))

        self.assertEqual(self.controller.state, STATE_IDLE)
        entry = self.controller.history_entries()[0]
        self.assertEqual(entry["status"], "failed")
        self.notify.assert_called_with(
            "Falha ao processar o texto de voz: insert exploded",
            key="voice-error",
        )

    def test_secure_input_reports_clipboard_failure_truthfully(self):
        self._ready()
        self.controller._secure_input_blocks = lambda: True
        with mock.patch("voice_support.Clipboard.set_content", return_value=False):
            self.controller.handle_hotkey_press(MODE_DICTATION)
            self.controller.handle_hotkey_release(MODE_DICTATION)

        self.assertEqual(self.controller.last_outcome, OUTCOME_SECURE_INPUT)
        self.notify.assert_called_with(
            "Entrada segura do macOS ativa. Não foi possível copiar o texto "
            "para a área de transferência.",
            key="voice-secure",
        )
        self.logger.warning.assert_called_once()

    def test_lost_target_reports_clipboard_failure_truthfully(self):
        self._ready()
        self.controller._restore_target = lambda target: False
        with mock.patch("voice_support.Clipboard.set_content", return_value=False):
            self.controller.handle_hotkey_press(MODE_DICTATION)
            self.controller.handle_hotkey_release(MODE_DICTATION)

        self.assertEqual(self.controller.last_outcome, OUTCOME_TARGET_LOST)
        self.notify.assert_called_with(
            "O aplicativo de destino não está mais na frente e não foi "
            "possível copiar o texto para a área de transferência.",
            key="voice-target",
        )
        self.logger.warning.assert_called_once()

    def test_status_callback_covers_interactive_session_states(self):
        self._ready()
        seen = []
        self.controller._on_status_change = lambda: seen.append(
            self.controller.status_snapshot()
        )
        self.controller.handle_hotkey_press(MODE_DICTATION)
        self.controller.handle_hotkey_release(MODE_DICTATION)
        self.assertEqual(
            [item["state"] for item in seen],
            [STATE_RECORDING, STATE_TRANSCRIBING, STATE_ROUTING, STATE_IDLE],
        )
        self.assertEqual(seen[0]["mode"], MODE_DICTATION)
        self.assertIsNone(seen[-1]["mode"])

    def test_cancel_returns_indicator_state_to_idle(self):
        self._ready()
        seen = []
        self.controller._on_status_change = lambda: seen.append(self.controller.state)
        self.controller.handle_hotkey_press(MODE_DICTATION)
        self.controller.cancel()
        self.assertEqual(seen, [STATE_RECORDING, STATE_IDLE])

    def test_voice_command_expands(self):
        self._ready()
        self.controller.settings.voice_replacements = {"xadds": "wrong"}
        self.backend.transcript = "xadds"
        self.controller.handle_hotkey_press(MODE_COMMAND)
        self.controller.handle_hotkey_release(MODE_COMMAND)
        self.assertEqual(self.expanded, ["xadds"])

    def test_stream_tail_chunks_are_fed_before_finalize(self):
        self.backend.start_stream()
        capture = _ChunkCapture([[0.1, 0.2], [0.3]])

        self.controller._drain_stream_chunks(capture)

        self.assertEqual(self.backend._stream, [[0.1, 0.2], [0.3]])

    def test_stream_completion_is_scoped_to_its_generation(self):
        import threading

        old_done = threading.Event()
        new_done = threading.Event()
        self.controller._stream_worker_events[2] = new_done

        self.controller._stream_worker(1, old_done)

        self.assertTrue(old_done.is_set())
        self.assertFalse(new_done.is_set())

    def test_cancelled_stream_cannot_feed_the_next_session(self):
        import threading
        import time

        class ThreadRunner:
            def __init__(self):
                self.threads = []

            def start(self, fn, *args, name=None):
                thread = threading.Thread(target=fn, args=args, daemon=True, name=name)
                self.threads.append(thread)
                thread.start()
                return thread

        class BlockingCapture(FakeCapture):
            def __init__(self, chunk):
                super().__init__(samples=chunk)
                self.chunk = chunk
                self.read_entered = threading.Event()
                self.release_read = threading.Event()
                self.returned = False

            def read_chunk(self, timeout=0.1):
                del timeout
                self.read_entered.set()
                self.release_read.wait(1.0)
                if self.returned:
                    raise Exception("empty")
                self.returned = True
                return self.chunk

        class TrackingBackend(FakeAsrBackend):
            def __init__(self):
                super().__init__()
                self.stream_starts = 0
                self.second_stream_started = threading.Event()

            def start_stream(self):
                super().start_stream()
                self.stream_starts += 1
                if self.stream_starts == 2:
                    self.second_stream_started.set()

        runner = ThreadRunner()
        first_capture = BlockingCapture([0.1])
        second_capture = BlockingCapture([0.2])
        captures = iter((first_capture, second_capture))
        backend = TrackingBackend()
        controller = VoiceController(
            {"voice_enabled": False},
            task_runner=runner,
            insert_text=lambda text: True,
            expand_trigger=lambda trigger: True,
            notify=self.notify,
            logger=self.logger,
            capture_target=lambda: VoiceTarget("window", handle=1),
            restore_target=lambda target: True,
            secure_input_blocks=lambda: False,
            backend=backend,
            capture_factory=lambda: next(captures),
            cache_dir=self.tmp,
            download=lambda entry, cache_dir, progress=None, cancel_event=None: os.path.join(
                self.tmp, "model.gguf"
            ),
        )
        with mock.patch("voice_support.model_is_installed", return_value=True), \
                mock.patch("voice_support.installed_model_path", return_value="model.gguf"), \
                mock.patch.object(controller, "_start_monitor"):
            controller.enable()
            deadline = time.time() + 1.0
            while controller.state != STATE_IDLE and time.time() < deadline:
                time.sleep(0.01)
        controller.settings.profile = "streaming"

        self.assertTrue(controller.handle_hotkey_press(MODE_DICTATION))
        self.assertTrue(first_capture.read_entered.wait(1.0))
        first_stream_worker = runner.threads[-1]
        controller.cancel()
        self.assertEqual(controller.state, STATE_IDLE)
        self.assertTrue(controller.handle_hotkey_press(MODE_DICTATION))
        self.assertTrue(backend.second_stream_started.wait(1.0))
        self.assertTrue(second_capture.read_entered.wait(1.0))

        first_capture.release_read.set()
        first_stream_worker.join(1.0)

        self.assertFalse(first_stream_worker.is_alive())
        self.assertEqual(backend._stream, [])

        controller.cancel()
        second_capture.release_read.set()
        controller.shutdown()

    def test_cancelled_stream_closes_before_a_racing_next_start(self):
        class NativeStream:
            def __init__(self, name, start_in_progress):
                self.name = name
                self._start_in_progress = start_in_progress
                self.closed = False
                self.close_during_start = False

            def close(self):
                self.close_during_start = self._start_in_progress()
                self.closed = True

            def feed(self, chunk):
                del chunk

            def text(self):
                return ""

        stream_requested = threading.Event()
        release_start = threading.Event()
        start_in_progress = True
        streams = []
        stream_starts = []
        cancel_lock_acquired = []
        controller_ref = []

        def close_during_start():
            return start_in_progress

        streams.extend(
            (NativeStream("first", close_during_start),
             NativeStream("second", close_during_start))
        )

        class NativeSession:
            def stream(self):
                stream = streams[len(stream_starts)]
                stream_starts.append(stream)
                if len(stream_starts) == 1:
                    stream_requested.set()
                    if not release_start.wait(1.0):
                        raise AssertionError("start_stream release timed out")
                    nonlocal start_in_progress
                    start_in_progress = False
                return stream

            def cancel(self):
                acquired = controller_ref[0]._lock.acquire(timeout=1.0)
                cancel_lock_acquired.append(acquired)
                if acquired:
                    controller_ref[0]._lock.release()

        from voice_runtime import TranscribeCppBackend

        runner = ThreadRunner()
        native = TranscribeCppBackend()
        native._profile = PROFILE_STREAMING
        native._session = NativeSession()
        captures = iter((FakeCapture([0.1]), FakeCapture([0.2])))
        controller = VoiceController(
            {"voice_enabled": False},
            task_runner=runner,
            insert_text=lambda text: True,
            expand_trigger=lambda trigger: True,
            notify=self.notify,
            logger=self.logger,
            capture_target=lambda: VoiceTarget("window", handle=1),
            restore_target=lambda target: True,
            secure_input_blocks=lambda: False,
            backend=native,
            capture_factory=captures.__next__,
            cache_dir=self.tmp,
        )
        controller_ref.append(controller)
        controller.settings.enabled = True
        controller.settings.profile = PROFILE_STREAMING
        controller._state = STATE_IDLE

        self.assertTrue(controller.handle_hotkey_press(MODE_DICTATION))
        self.assertTrue(stream_requested.wait(1.0))
        controller.cancel()
        release_start.set()
        runner.threads[-1].join(1.0)

        self.assertEqual(cancel_lock_acquired, [True])
        self.assertTrue(streams[0].closed)
        self.assertFalse(streams[0].close_during_start)
        self.assertIsNone(native._stream)

        self.assertTrue(controller.handle_hotkey_press(MODE_DICTATION))
        deadline = time.monotonic() + 1.0
        while len(stream_starts) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(stream_starts, streams)
        controller.cancel()
        runner.threads[-1].join(1.0)
        self.assertTrue(streams[1].closed)

    def test_dictation_applies_term_correction_and_keeps_raw_transcript(self):
        self._ready()
        self.controller.settings.voice_replacements = {"Queen": "Qwen"}
        self.backend.transcript = "Testing Queen now"

        self.controller.handle_hotkey_press(MODE_DICTATION)
        self.controller.handle_hotkey_release(MODE_DICTATION)

        self.assertEqual(self.inserted, ["Testing Qwen now"])
        entry = self.controller.history_entries()[0]
        self.assertEqual(entry["transcript"], "Testing Qwen now")
        self.assertEqual(entry["raw_transcript"], "Testing Queen now")
        self.assertIn("inference_duration_seconds", entry)

    def test_form_target_does_not_paste(self):
        self._ready()
        seen = []
        self.controller.register_form_target(lambda text: seen.append(text))
        self.backend.transcript = "João"
        self.controller.handle_hotkey_press(MODE_DICTATION)
        self.controller.handle_hotkey_release(MODE_DICTATION)
        self.assertEqual(seen, ["João"])
        self.assertEqual(self.inserted, [])

    def test_queued_form_guard_expires_after_cancel(self):
        self._ready()
        queued = []
        self.controller.register_form_target(
            lambda text: None,
            lambda text, token: queued.append((text, token)),
        )
        self.backend.transcript = "João"

        self.controller.handle_hotkey_press(MODE_DICTATION)
        self.controller.handle_hotkey_release(MODE_DICTATION)

        self.assertEqual([text for text, _token in queued], ["João"])
        token = queued[0][1]
        self.assertTrue(self.controller.form_guard_valid(token))
        self.controller.cancel()
        self.assertFalse(self.controller.form_guard_valid(token))

    def test_second_press_ignored(self):
        self._ready()
        self.assertTrue(self.controller.handle_hotkey_press(MODE_DICTATION))
        self.assertFalse(self.controller.handle_hotkey_press(MODE_DICTATION))

    def test_overflow_does_not_insert(self):
        self.capture.overflow = True
        self._ready()
        self.controller.handle_hotkey_press(MODE_DICTATION)
        self.controller.handle_hotkey_release(MODE_DICTATION)
        self.assertEqual(self.inserted, [])

    def test_input_status_failure_is_not_reported_as_duration_limit(self):
        self.capture.issue = CaptureIssue.INPUT_STATUS
        self._ready()
        with mock.patch.object(
            self.backend, "transcribe", wraps=self.backend.transcribe
        ) as transcribe:
            self.controller.handle_hotkey_press(MODE_DICTATION)
            self.controller.handle_hotkey_release(MODE_DICTATION)

        transcribe.assert_not_called()
        entry = self.controller.history_entries()[0]
        self.assertEqual(entry["status"], "failed")
        self.assertEqual(entry["capture_issue"], "input_status")
        self.assertEqual(
            entry["capture_issues"],
            [{"issue": "input_status", "message": "capture failed"}],
        )
        self.assertTrue(self.controller._history.is_retryable(entry["id"]))
        self.notify.assert_called_with(
            "O microfone relatou uma falha durante a gravação. "
            "O áudio parcial foi salvo no histórico.",
            key="voice-error",
        )

    def test_unexpected_stop_failure_closes_the_history_journal(self):
        self._ready()
        self.controller.handle_hotkey_press(MODE_DICTATION)
        recording = self.controller._history_recording
        self.capture.stop = mock.Mock(side_effect=OSError("device lost"))

        self.controller.handle_hotkey_release(MODE_DICTATION)

        self.assertTrue(recording._closed)
        entry = self.controller.history_entry(recording.record_id)
        self.assertEqual(entry["status"], "failed")
        self.assertIn("device lost", entry["error"])

    def test_audio_error_stays_idle(self):
        self.capture.error = VoiceAudioError("recusado")
        self._ready()
        self.assertFalse(self.controller.handle_hotkey_press(MODE_DICTATION))
        self.assertEqual(self.controller.state, STATE_IDLE)

    def test_shutdown_blocks_later_press(self):
        self._ready()
        self.controller.shutdown()
        self.assertFalse(self.controller.handle_hotkey_press(MODE_DICTATION))

    def test_shutdown_stops_a_queued_finish_capture(self):
        finish_gate = threading.Event()

        class GatedRunner:
            def __init__(self):
                self.threads = []

            def start(self, fn, *args, name=None):
                def run():
                    if name == "voice-finish":
                        finish_gate.wait(1.0)
                    fn(*args)

                thread = threading.Thread(target=run, daemon=True, name=name)
                self.threads.append(thread)
                thread.start()
                return thread

        runner = GatedRunner()
        controller = VoiceController(
            {"voice_enabled": False},
            task_runner=runner,
            insert_text=lambda text: self.inserted.append(text) or True,
            expand_trigger=lambda trigger: True,
            notify=self.notify,
            logger=self.logger,
            capture_target=lambda: VoiceTarget("window", handle=1),
            restore_target=lambda target: True,
            secure_input_blocks=lambda: False,
            backend=self.backend,
            capture_factory=lambda: self.capture,
            cache_dir=self.tmp,
            download=lambda entry, cache_dir, progress=None, cancel_event=None: os.path.join(
                self.tmp, "model.gguf"
            ),
        )
        with mock.patch("voice_support.model_is_installed", return_value=True), \
                mock.patch("voice_support.installed_model_path", return_value="model.gguf"), \
                mock.patch.object(controller, "_start_monitor"):
            controller.enable()
            self.assertEqual(controller.state, STATE_IDLE)
            self.assertTrue(controller.handle_hotkey_press(MODE_DICTATION))
            self.assertTrue(self.capture.started)
            self.assertTrue(controller.handle_hotkey_release(MODE_DICTATION))
            controller.shutdown(timeout=0.05)
            self.assertEqual(self.capture.stop_calls, 0)

            finish_gate.set()
            runner.threads[-1].join(1.0)

        self.assertFalse(runner.threads[-1].is_alive())
        self.assertGreaterEqual(self.capture.stop_calls, 1)
        self.assertEqual(self.inserted, [])

    def test_failed_switch_restores_previous_when_possible(self):
        self._ready()
        self.backend.load = mock.Mock(side_effect=Exception("boom"))
        with mock.patch("voice_support.installed_model_path", return_value="old.gguf"):
            # first load after failure uses previous profile
            original_load = FakeAsrBackend.load
            self.backend.load = mock.Mock(side_effect=[Exception("boom"), None])
            self.controller.set_profile("accuracy")
        self.assertEqual(self.controller.settings.profile, "balanced")

    def test_profile_switch_emits_final_idle_status_after_success(self):
        self._ready()
        seen = []
        self.controller._on_status_change = lambda: seen.append(self.controller.state)
        with mock.patch("voice_support.model_is_installed", return_value=True), \
                mock.patch("voice_support.installed_model_path", return_value="new.gguf"), \
                mock.patch.object(self.controller, "_start_monitor"):
            self.controller.set_profile("accuracy")
        self.assertEqual(seen, [STATE_LOADING, STATE_IDLE])
        self.assertEqual(self.controller.settings.profile, "accuracy")

    def test_profile_switch_emits_final_idle_status_after_rollback(self):
        self._ready()
        seen = []
        self.controller._on_status_change = lambda: seen.append(self.controller.state)
        self.backend.load = mock.Mock(side_effect=[Exception("new model failed"), None])
        with mock.patch("voice_support.model_is_installed", return_value=True), \
                mock.patch("voice_support.installed_model_path", return_value="old.gguf"), \
                mock.patch.object(self.controller, "_start_monitor"):
            self.controller.set_profile("accuracy")
        self.assertEqual(seen, [STATE_LOADING, STATE_IDLE])
        self.assertEqual(self.controller.settings.profile, "balanced")

    def test_profile_switch_emits_unavailable_status_after_terminal_failure(self):
        self._ready()
        seen = []
        self.controller._on_status_change = lambda: seen.append(self.controller.state)
        self.backend.load = mock.Mock(side_effect=Exception("new model failed"))
        with mock.patch("voice_support.model_is_installed", return_value=True), \
                mock.patch("voice_support.installed_model_path", return_value=None), \
                mock.patch.object(self.controller, "_start_monitor"):
            self.controller.set_profile("accuracy")
        self.assertEqual(seen, [STATE_LOADING, STATE_UNAVAILABLE])
        self.assertEqual(self.controller.settings.profile, "balanced")

    def test_delete_disables(self):
        self._ready()
        with mock.patch("voice_support.delete_model"):
            self.assertTrue(self.controller.delete_active_model())
        self.assertFalse(self.controller.enabled)

    def test_reapplying_same_options_retries_an_unavailable_backend(self):
        self._ready()
        self.controller.disable()
        self.controller.settings.enabled = True
        with mock.patch("voice_support.model_is_installed", return_value=True), \
                mock.patch("voice_support.installed_model_path", return_value="model.gguf"), \
                mock.patch.object(self.controller, "_start_monitor"):
            self.controller.apply_options(profile="balanced")
        self.assertEqual(self.controller.state, STATE_IDLE)

    def test_language_change_reloads_the_idle_backend(self):
        self._ready()
        with mock.patch("voice_support.model_is_installed", return_value=True), \
                mock.patch("voice_support.installed_model_path", return_value="model.gguf"), \
                mock.patch.object(self.controller, "_start_monitor"):
            self.controller.set_language("en-US")
        self.assertEqual(self.backend.language, "en-US")
        self.assertEqual(self.controller.state, STATE_IDLE)

    def test_hotkey_change_restarts_monitor_without_reloading_backend(self):
        self._ready()
        with mock.patch.object(self.controller, "_start_monitor") as start_monitor, \
                mock.patch.object(self.backend, "load", wraps=self.backend.load) as load:
            self.controller.apply_options(
                hotkey="control+shift+f8",
                command_hotkey="ctrl+alt+f9",
            )
        self.assertEqual(self.controller.settings.hotkey, "ctrl+shift+f8")
        self.assertEqual(self.controller.settings.command_hotkey, "ctrl+alt+f9")
        start_monitor.assert_called_once_with()
        load.assert_not_called()

    def test_hotkey_change_cancels_an_active_recording(self):
        self._ready()
        self.assertTrue(self.controller.handle_hotkey_press(MODE_DICTATION))
        with mock.patch.object(self.controller, "_start_monitor"):
            self.controller.apply_options(hotkey="ctrl+shift+f8")
        self.assertGreaterEqual(self.capture.stop_calls, 1)
        self.assertFalse(self.capture.started)
        self.assertEqual(self.controller.state, STATE_IDLE)

    def test_denied_microphone_does_not_open_capture(self):
        self._ready()
        self.controller._microphone_status = lambda: "denied"
        self.assertFalse(self.controller.handle_hotkey_press(MODE_DICTATION))
        self.assertEqual(self.controller.state, STATE_IDLE)
        self.assertFalse(self.capture.started)

    def test_download_progress_reaches_the_tray_label(self):
        seen = []
        self.controller._on_status_change = lambda: seen.append(self.controller.status_label())

        def fake_download(entry, cache_dir, progress=None, cancel_event=None):
            if progress is not None:
                progress(50, 100)
            return os.path.join(self.tmp, "model.gguf")

        self.controller._download = fake_download
        with mock.patch("voice_support.model_is_installed", return_value=False), \
                mock.patch.object(self.controller, "_start_monitor"):
            self.controller.enable()
        self.assertTrue(any("baixando" in label for label in seen))

    def test_explicit_model_download_does_not_enable_or_load_voice(self):
        seen = []
        downloaded = []
        self.controller._on_status_change = lambda: seen.append(
            self.controller.status_label()
        )

        def fake_download(entry, cache_dir, progress=None, cancel_event=None):
            downloaded.append(entry["profile"])
            if progress is not None:
                progress(50, 100)
            return os.path.join(self.tmp, "model.gguf")

        self.controller._download = fake_download
        with mock.patch("voice_support.model_is_installed", return_value=False):
            started = self.controller.download_profile("compact")

        self.assertTrue(started)
        self.assertEqual(downloaded, ["compact"])
        self.assertFalse(self.controller.is_enabled())
        self.assertFalse(self.backend.is_loaded())
        self.assertTrue(any("baixando" in label for label in seen))
        self.notify.assert_called_with(
            "Modelo de voz baixado e verificado.", key="voice-model-download"
        )

    def test_duplicate_explicit_download_is_rejected_while_active(self):
        self.controller._model_download_active = True
        self.controller._model_download_profile = "compact"

        self.assertFalse(self.controller.download_profile("compact"))

    def test_enable_after_disable_returns_to_idle(self):
        self._ready()
        self.controller.disable()
        self.assertEqual(self.controller.state, STATE_UNAVAILABLE)
        self._ready()
        self.assertEqual(self.controller.state, STATE_IDLE)
        self.assertTrue(self.controller.handle_hotkey_press(MODE_DICTATION))
        self.assertEqual(self.controller.state, STATE_RECORDING)

    def test_disable_closes_admission_before_waiting_for_workers(self):
        entered = threading.Event()
        release = threading.Event()

        class BlockingBackend(FakeAsrBackend):
            def transcribe(self, pcm, cancel_event=None):
                entered.set()
                release.wait(2.0)
                return super().transcribe(pcm, cancel_event=cancel_event)

        self._ready()
        self.controller._provider.backend = BlockingBackend(transcript="late")
        self.controller.task_runner = ThreadRunner()
        with mock.patch.object(self.controller, "_start_monitor"):
            self.assertTrue(self.controller.handle_hotkey_press(MODE_DICTATION))
            self.controller.handle_hotkey_release(MODE_DICTATION)
        self.assertTrue(entered.wait(1.0))

        join_entered = threading.Event()
        allow_join = threading.Event()
        original_join = self.controller._join_workers

        def gated_join(timeout, workers=None):
            join_entered.set()
            self.assertTrue(allow_join.wait(1.0))
            return original_join(timeout, workers=workers)

        self.controller._join_workers = gated_join
        disable_thread = threading.Thread(target=self.controller.disable, daemon=True)
        disable_thread.start()
        self.assertTrue(join_entered.wait(1.0))
        self.assertFalse(self.controller.settings.enabled)
        self.assertEqual(self.controller.state, STATE_UNAVAILABLE)
        self.assertGreaterEqual(self.capture.stop_calls, 1)
        self.assertEqual(self.controller.history_entries()[0]["status"], "cancelled")
        self.assertFalse(self.controller.handle_hotkey_press(MODE_DICTATION))
        self.controller.apply_options(profile="accuracy")
        self.assertEqual(self.controller.settings.profile, "balanced")
        self.controller.enable()
        self.assertEqual(self.controller.state, STATE_UNAVAILABLE)
        self.assertFalse(self.controller.settings.enabled)

        release.set()
        allow_join.set()
        disable_thread.join(1.0)
        self.assertFalse(disable_thread.is_alive())

    def test_late_escape_does_not_cancel_during_disable(self):
        self._ready()
        self.controller.task_runner = ThreadRunner()
        join_entered = threading.Event()
        allow_join = threading.Event()
        original_join = self.controller._join_workers

        def gated_join(timeout, workers=None):
            join_entered.set()
            self.assertTrue(allow_join.wait(1.0))
            return original_join(timeout, workers=workers)

        self.controller._join_workers = gated_join
        disable_thread = threading.Thread(target=self.controller.disable, daemon=True)
        disable_thread.start()
        self.assertTrue(join_entered.wait(1.0))
        cancel_calls_before_escape = self.backend.cancel_calls

        self.controller._hotkey_escape_from_os()
        self.controller._wait_for_workers()
        self.assertEqual(self.backend.cancel_calls, cancel_calls_before_escape)

        allow_join.set()
        disable_thread.join(1.0)
        self.assertFalse(disable_thread.is_alive())

    def test_switch_rollback_persists_without_holding_controller_lock(self):
        self._ready()
        payloads = []
        lock_observed = []

        def persist(payload):
            payloads.append(payload)
            acquired = self.controller._lock.acquire(timeout=1.0)
            lock_observed.append(acquired)
            if acquired:
                self.controller._lock.release()

        self.controller._persist_settings = persist
        self.backend.load = mock.Mock(side_effect=[Exception("boom"), None])
        with mock.patch("voice_support.installed_model_path", return_value="old.gguf"):
            self.controller.set_profile("accuracy")

        self.assertEqual(self.controller.settings.profile, "balanced")
        self.assertEqual(payloads[-1]["voice_profile"], "balanced")
        self.assertTrue(lock_observed)
        self.assertTrue(all(lock_observed))

    def test_switch_during_recording_stops_capture(self):
        self._ready()
        self.assertTrue(self.controller.handle_hotkey_press(MODE_DICTATION))
        self.assertEqual(self.controller.state, STATE_RECORDING)
        with mock.patch("voice_support.model_is_installed", return_value=True), \
                mock.patch("voice_support.installed_model_path", return_value="model.gguf"), \
                mock.patch.object(self.controller, "_start_monitor"):
            self.controller.set_language("en-US")
        self.assertGreaterEqual(self.capture.stop_calls, 1)
        self.assertFalse(self.capture.started)
        self.assertFalse(self.controller.handle_hotkey_release(MODE_DICTATION))
        self.assertEqual(self.inserted, [])

    def test_closed_form_does_not_receive_a_later_forms_transcript(self):
        self._ready()
        first = []
        second = []
        self.controller.register_form_target(lambda text: first.append(text))
        self.controller.handle_hotkey_press(MODE_DICTATION)
        self.controller.unregister_form_target()
        self.controller.register_form_target(lambda text: second.append(text))
        self.backend.transcript = "João"
        self.controller.handle_hotkey_release(MODE_DICTATION)
        self.assertEqual(first, [])
        self.assertEqual(second, [])
        self.assertEqual(self.inserted, [])
        self.controller._invoke_session_form("tarde demais")
        self.assertEqual(first, [])
        self.assertEqual(second, [])

    def test_shutdown_cancels_native_inference_before_unload(self):
        import threading
        import time

        class ThreadRunner:
            def start(self, fn, *args, name=None):
                thread = threading.Thread(target=fn, args=args, daemon=True, name=name)
                thread.start()
                return thread

        entered = threading.Event()
        left = threading.Event()
        unloaded_before_exit = []

        class SlowBackend(FakeAsrBackend):
            def transcribe(self, pcm, cancel_event=None):
                entered.set()
                try:
                    deadline = time.time() + 1.0
                    while time.time() < deadline:
                        if self._cancelled(cancel_event):
                            raise VoiceRuntimeError("Transcrição cancelada.")
                        time.sleep(0.01)
                    return self.transcript
                finally:
                    left.set()

            def unload(self):
                unloaded_before_exit.append(not left.is_set())
                super().unload()

        backend = SlowBackend(transcript="late")
        controller = VoiceController(
            {"voice_enabled": False},
            task_runner=ThreadRunner(),
            insert_text=lambda text: self.inserted.append(text) or True,
            expand_trigger=lambda trigger: True,
            notify=self.notify,
            logger=self.logger,
            capture_target=lambda: VoiceTarget("window", handle=1),
            restore_target=lambda target: True,
            secure_input_blocks=lambda: False,
            backend=backend,
            capture_factory=lambda: FakeCapture(),
            cache_dir=self.tmp,
            download=lambda entry, cache_dir, progress=None, cancel_event=None: os.path.join(
                self.tmp, "model.gguf"
            ),
        )
        with mock.patch("voice_support.model_is_installed", return_value=True), \
                mock.patch("voice_support.installed_model_path", return_value="model.gguf"), \
                mock.patch.object(controller, "_start_monitor"):
            controller.enable()
        self.assertTrue(controller.handle_hotkey_press(MODE_DICTATION))
        self.assertTrue(controller.handle_hotkey_release(MODE_DICTATION))
        self.assertTrue(entered.wait(1.0))
        controller.shutdown(timeout=1.0)
        self.assertTrue(left.wait(1.0))
        self.assertGreaterEqual(backend.cancel_calls, 1)
        self.assertEqual(unloaded_before_exit, [False])
        self.assertEqual(self.inserted, [])

    def test_shutdown_defers_unload_when_backend_ignores_cancel(self):
        entered = threading.Event()
        release = threading.Event()
        left = threading.Event()
        unloaded = threading.Event()
        unloaded_before_exit = []

        class IgnoringCancelBackend(FakeAsrBackend):
            def transcribe(self, pcm, cancel_event=None):
                del pcm, cancel_event
                entered.set()
                try:
                    release.wait(2.0)
                    return self.transcript
                finally:
                    left.set()

            def cancel(self):
                self.cancel_calls += 1

            def unload(self):
                unloaded_before_exit.append(not left.is_set())
                unloaded.set()
                super().unload()

        backend = IgnoringCancelBackend(transcript="late")
        controller = VoiceController(
            {"voice_enabled": False},
            task_runner=ThreadRunner(),
            insert_text=lambda text: self.inserted.append(text) or True,
            expand_trigger=lambda trigger: True,
            notify=self.notify,
            logger=self.logger,
            capture_target=lambda: VoiceTarget("window", handle=1),
            restore_target=lambda target: True,
            secure_input_blocks=lambda: False,
            backend=backend,
            capture_factory=lambda: FakeCapture(),
            cache_dir=self.tmp,
            download=lambda entry, cache_dir, progress=None, cancel_event=None: os.path.join(
                self.tmp, "model.gguf"
            ),
        )
        with mock.patch("voice_support.model_is_installed", return_value=True), \
                mock.patch("voice_support.installed_model_path", return_value="model.gguf"), \
                mock.patch.object(controller, "_start_monitor"):
            controller.enable()
            deadline = time.monotonic() + 1.0
            while controller.state != STATE_IDLE and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(controller.state, STATE_IDLE)
            self.assertTrue(controller.handle_hotkey_press(MODE_DICTATION))
            self.assertTrue(controller.handle_hotkey_release(MODE_DICTATION))
            self.assertTrue(entered.wait(1.0))

            started = time.monotonic()
            controller.shutdown(timeout=0.05)
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 0.5)
            self.assertGreaterEqual(backend.cancel_calls, 1)
            self.assertFalse(unloaded.is_set())

            release.set()
            self.assertTrue(left.wait(1.0))
            self.assertTrue(unloaded.wait(1.0))

        self.assertEqual(unloaded_before_exit, [False])
        self.assertEqual(self.inserted, [])

    def test_concurrent_profile_switches_do_not_wait_on_each_other(self):
        entered = threading.Event()
        release = threading.Event()

        class IgnoringCancelBackend(FakeAsrBackend):
            def transcribe(self, pcm, cancel_event=None):
                del pcm, cancel_event
                entered.set()
                release.wait(2.0)
                return self.transcript

            def cancel(self):
                self.cancel_calls += 1

        backend = IgnoringCancelBackend(transcript="late")
        controller = VoiceController(
            {"voice_enabled": False},
            task_runner=ThreadRunner(),
            insert_text=lambda text: self.inserted.append(text) or True,
            expand_trigger=lambda trigger: True,
            notify=self.notify,
            logger=self.logger,
            capture_target=lambda: VoiceTarget("window", handle=1),
            restore_target=lambda target: True,
            secure_input_blocks=lambda: False,
            backend=backend,
            capture_factory=lambda: FakeCapture(),
            cache_dir=self.tmp,
            download=lambda entry, cache_dir, progress=None, cancel_event=None: os.path.join(
                self.tmp, "model.gguf"
            ),
        )
        with mock.patch("voice_support.model_is_installed", return_value=True), \
                mock.patch("voice_support.installed_model_path", return_value="model.gguf"), \
                mock.patch.object(controller, "_start_monitor"):
            controller.enable()
            deadline = time.monotonic() + 1.0
            while controller.state != STATE_IDLE and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(controller.state, STATE_IDLE)
            self.assertTrue(controller.handle_hotkey_press(MODE_DICTATION))
            self.assertTrue(controller.handle_hotkey_release(MODE_DICTATION))
            self.assertTrue(entered.wait(1.0))

            controller.set_language("en-US")
            controller.set_profile("accuracy")
            release.set()

            deadline = time.monotonic() + 2.0
            while controller.state != STATE_IDLE and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(controller.state, STATE_IDLE)
            self.assertFalse(any(thread.is_alive() for thread in controller.task_runner.threads))
            self.assertEqual(self.inserted, [])

    def test_repeated_shutdown_does_not_overlap_deferred_unload(self):
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        active = 0
        calls = []
        overlap = []
        active_lock = threading.Lock()

        class BlockingUnloadBackend(FakeAsrBackend):
            def unload(self):
                nonlocal active
                with active_lock:
                    active += 1
                    calls.append(active)
                    if active > 1:
                        overlap.append(True)
                entered.set()
                try:
                    if len(calls) == 1:
                        release.wait(1.0)
                    super().unload()
                finally:
                    with active_lock:
                        active -= 1
                    finished.set()

        backend = BlockingUnloadBackend()
        controller = VoiceController(
            {"voice_enabled": False},
            task_runner=InlineRunner(),
            insert_text=lambda text: self.inserted.append(text) or True,
            expand_trigger=lambda trigger: True,
            notify=self.notify,
            logger=self.logger,
            capture_target=lambda: VoiceTarget("window", handle=1),
            restore_target=lambda target: True,
            secure_input_blocks=lambda: False,
            backend=backend,
            capture_factory=lambda: FakeCapture(),
            cache_dir=self.tmp,
            download=lambda entry, cache_dir, progress=None, cancel_event=None: os.path.join(
                self.tmp, "model.gguf"
            ),
        )
        with mock.patch("voice_support.model_is_installed", return_value=True), \
                mock.patch("voice_support.installed_model_path", return_value="model.gguf"), \
                mock.patch.object(controller, "_start_monitor"):
            controller.enable()
            controller._schedule_unload_after_workers()
            self.assertTrue(entered.wait(1.0))

            started = time.monotonic()
            controller.shutdown(timeout=0.0)
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertEqual(calls, [1])
            self.assertEqual(overlap, [])

            release.set()
            self.assertTrue(finished.wait(1.0))

        self.assertEqual(calls, [1])
        self.assertEqual(overlap, [])

    def test_delete_model_waits_for_deferred_unload(self):
        entered = threading.Event()
        release = threading.Event()
        first_unload = True

        class BlockingUnloadBackend(FakeAsrBackend):
            def unload(self):
                nonlocal first_unload
                if first_unload:
                    first_unload = False
                    entered.set()
                    release.wait(1.0)
                super().unload()

        backend = BlockingUnloadBackend()
        controller = VoiceController(
            {"voice_enabled": False},
            task_runner=InlineRunner(),
            insert_text=lambda text: self.inserted.append(text) or True,
            expand_trigger=lambda trigger: True,
            notify=self.notify,
            logger=self.logger,
            capture_target=lambda: VoiceTarget("window", handle=1),
            restore_target=lambda target: True,
            secure_input_blocks=lambda: False,
            backend=backend,
            capture_factory=lambda: FakeCapture(),
            cache_dir=self.tmp,
            download=lambda entry, cache_dir, progress=None, cancel_event=None: os.path.join(
                self.tmp, "model.gguf"
            ),
        )
        with mock.patch("voice_support.model_is_installed", return_value=True), \
                mock.patch("voice_support.installed_model_path", return_value="model.gguf"), \
                mock.patch.object(controller, "_start_monitor"):
            controller.enable()
            controller._schedule_unload_after_workers()
            self.assertTrue(entered.wait(1.0))
            with mock.patch.object(controller._provider, "delete_profile") as delete:
                self.assertFalse(controller.delete_active_model())
                delete.assert_not_called()

            release.set()
            deadline = time.monotonic() + 1.0
            while controller._unload_pending and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(controller._unload_pending)
            with mock.patch.object(controller._provider, "delete_profile") as delete:
                self.assertTrue(controller.delete_active_model())
                delete.assert_called_once_with("balanced")

    def test_stale_switch_failure_cannot_restore_after_disable(self):
        entered = threading.Event()
        release = threading.Event()

        class FailingSwitchBackend(FakeAsrBackend):
            def load(self, model_path, profile, language):
                if profile == "accuracy":
                    entered.set()
                    release.wait(2.0)
                    raise RuntimeError("new model failed")
                super().load(model_path, profile, language)

        backend = FailingSwitchBackend()
        controller = VoiceController(
            {"voice_enabled": False},
            task_runner=ThreadRunner(),
            insert_text=lambda text: self.inserted.append(text) or True,
            expand_trigger=lambda trigger: True,
            notify=self.notify,
            logger=self.logger,
            capture_target=lambda: VoiceTarget("window", handle=1),
            restore_target=lambda target: True,
            secure_input_blocks=lambda: False,
            backend=backend,
            capture_factory=lambda: FakeCapture(),
            cache_dir=self.tmp,
            download=lambda entry, cache_dir, progress=None, cancel_event=None: os.path.join(
                self.tmp, "model.gguf"
            ),
        )
        with mock.patch("voice_support.model_is_installed", return_value=True), \
                mock.patch("voice_support.installed_model_path", return_value="model.gguf"), \
                mock.patch("voice_support._SHUTDOWN_JOIN_SECONDS", 0.05), \
                mock.patch.object(controller, "_start_monitor"):
            controller.enable()
            deadline = time.monotonic() + 1.0
            while controller.state != STATE_IDLE and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(controller.state, STATE_IDLE)

            controller.set_profile("accuracy")
            self.assertTrue(entered.wait(1.0))
            controller.disable()
            self.assertEqual(controller.state, STATE_UNAVAILABLE)
            self.assertFalse(controller.settings.enabled)

            release.set()
            for thread in controller.task_runner.threads:
                thread.join(1.0)

        self.assertFalse(any(thread.is_alive() for thread in controller.task_runner.threads))
        self.assertEqual(controller.state, STATE_UNAVAILABLE)
        self.assertFalse(controller.settings.enabled)
        self.assertEqual(controller.settings.profile, "accuracy")

    def test_superseded_switch_failure_cannot_restore_an_older_profile(self):
        entered = threading.Event()
        release = threading.Event()

        class FailingSwitchBackend(FakeAsrBackend):
            def load(self, model_path, profile, language):
                if (
                    profile == "balanced"
                    and language == "en-US"
                    and not entered.is_set()
                ):
                    entered.set()
                    release.wait(2.0)
                    raise RuntimeError("new model failed")
                super().load(model_path, profile, language)

        backend = FailingSwitchBackend()
        controller = VoiceController(
            {"voice_enabled": False},
            task_runner=ThreadRunner(),
            insert_text=lambda text: self.inserted.append(text) or True,
            expand_trigger=lambda trigger: True,
            notify=self.notify,
            logger=self.logger,
            capture_target=lambda: VoiceTarget("window", handle=1),
            restore_target=lambda target: True,
            secure_input_blocks=lambda: False,
            backend=backend,
            capture_factory=lambda: FakeCapture(),
            cache_dir=self.tmp,
            download=lambda entry, cache_dir, progress=None, cancel_event=None: os.path.join(
                self.tmp, "model.gguf"
            ),
        )
        with mock.patch("voice_support.model_is_installed", return_value=True), \
                mock.patch("voice_support.installed_model_path", return_value="model.gguf"), \
                mock.patch.object(controller, "_start_monitor"):
            controller.enable()
            deadline = time.monotonic() + 1.0
            while controller.state != STATE_IDLE and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(controller.state, STATE_IDLE)

            controller.set_language("en-US")
            self.assertTrue(entered.wait(1.0))
            controller.set_profile("accuracy")
            release.set()
            deadline = time.monotonic() + 2.0
            while controller.state != STATE_IDLE and time.monotonic() < deadline:
                time.sleep(0.01)

        self.assertEqual(controller.state, STATE_IDLE)
        self.assertTrue(controller.settings.enabled)
        self.assertEqual(controller.settings.profile, "accuracy")
        self.assertEqual(controller.settings.language, LANGUAGE_AUTO)

    def test_worker_is_tracked_before_runner_returns(self):
        started = threading.Event()
        allow_runner_return = threading.Event()
        entered = threading.Event()
        release = threading.Event()

        class DelayedRunner:
            def start(self, fn, *args, name=None):
                thread = threading.Thread(target=fn, args=args, daemon=True, name=name)
                thread.start()
                started.set()
                self.assertTrue(allow_runner_return.wait(1.0))
                return thread

            @staticmethod
            def assertTrue(value):
                if not value:
                    raise AssertionError("runner return gate timed out")

        self.controller.task_runner = DelayedRunner()

        def slow_worker():
            entered.set()
            self.assertTrue(release.wait(1.0))

        starter = threading.Thread(
            target=lambda: self.controller._start_worker(slow_worker, name="slow-test"),
            daemon=True,
        )
        starter.start()
        self.assertTrue(started.wait(1.0))
        self.assertTrue(entered.wait(1.0))

        joined = self.controller._join_workers(0.05)
        self.assertFalse(joined)
        self.assertEqual(len(self.controller._workers), 1)

        release.set()
        allow_runner_return.set()
        starter.join(1.0)
        self.assertFalse(starter.is_alive())
        self.assertTrue(self.controller._join_workers(1.0))

    def test_switch_during_transcription_keeps_loading_and_rejects_press(self):
        import threading
        import time

        class ThreadRunner:
            def start(self, fn, *args, name=None):
                thread = threading.Thread(target=fn, args=args, daemon=True, name=name)
                thread.start()
                return thread

        entered = threading.Event()
        in_unload = threading.Event()
        allow_unload = threading.Event()
        captures = []

        class SlowBackend(FakeAsrBackend):
            def transcribe(self, pcm, cancel_event=None):
                entered.set()
                deadline = time.time() + 1.0
                while time.time() < deadline:
                    if self._cancelled(cancel_event):
                        raise VoiceRuntimeError("Transcrição cancelada.")
                    time.sleep(0.01)
                return self.transcript

            def unload(self):
                in_unload.set()
                allow_unload.wait(1.0)
                super().unload()

        def factory():
            capture = FakeCapture()
            captures.append(capture)
            return capture

        backend = SlowBackend(transcript="late")
        controller = VoiceController(
            {"voice_enabled": False},
            task_runner=ThreadRunner(),
            insert_text=lambda text: self.inserted.append(text) or True,
            expand_trigger=lambda trigger: True,
            notify=self.notify,
            logger=self.logger,
            capture_target=lambda: VoiceTarget("window", handle=1),
            restore_target=lambda target: True,
            secure_input_blocks=lambda: False,
            backend=backend,
            capture_factory=factory,
            cache_dir=self.tmp,
            download=lambda entry, cache_dir, progress=None, cancel_event=None: os.path.join(
                self.tmp, "model.gguf"
            ),
        )
        with mock.patch("voice_support.model_is_installed", return_value=True), \
                mock.patch("voice_support.installed_model_path", return_value="model.gguf"), \
                mock.patch.object(controller, "_start_monitor"):
            controller.enable()
            deadline = time.time() + 1.0
            while controller.state != STATE_IDLE and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(controller.state, STATE_IDLE)
            self.assertTrue(controller.handle_hotkey_press(MODE_DICTATION))
            self.assertTrue(controller.handle_hotkey_release(MODE_DICTATION))
            self.assertTrue(entered.wait(1.0))
            controller.set_language("en-US")
            self.assertTrue(in_unload.wait(1.0))
            self.assertEqual(controller.state, STATE_LOADING)
            self.assertFalse(controller.handle_hotkey_press(MODE_DICTATION))
            allow_unload.set()
            deadline = time.time() + 1.0
            while controller.state == STATE_LOADING and time.time() < deadline:
                time.sleep(0.01)
        self.assertEqual(controller.state, STATE_IDLE)
        self.assertFalse(any(capture.started for capture in captures))
        self.assertEqual(self.inserted, [])


if __name__ == "__main__":
    unittest.main()
