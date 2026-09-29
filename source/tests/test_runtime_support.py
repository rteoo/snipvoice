import io
import logging
import os
import shutil
import tempfile
import threading
import unittest
from logging.handlers import RotatingFileHandler
from unittest import mock

import runtime_support
from runtime_support import (
    AppLogger,
    BackgroundTaskRunner,
    TextInserter,
    configure_logging,
    normalize_clipboard_text,
)

try:  # pynput is a Windows runtime dependency; guard so the file imports anywhere.
    import pynput  # noqa: F401

    HAS_PYNPUT = True
except Exception:  # pragma: no cover - only on a host without pynput installed
    HAS_PYNPUT = False


class NormalizeClipboardTextTests(unittest.TestCase):
    """The LF-side comparison helper used by the clipboard restore path."""

    def test_crlf_is_collapsed_to_lf(self):
        self.assertEqual("a\nb", normalize_clipboard_text("a\r\nb"))

    def test_lone_cr_is_collapsed_to_lf(self):
        self.assertEqual("a\nb", normalize_clipboard_text("a\rb"))

    def test_already_lf_is_unchanged(self):
        self.assertEqual("a\nb", normalize_clipboard_text("a\nb"))

    def test_rich_payload_uses_its_plain_text(self):
        payload = {"__kind__": "rich_text", "text": "linha\r\numa", "spans": []}
        self.assertEqual("linha\numa", normalize_clipboard_text(payload))

    def test_none_becomes_empty_string(self):
        self.assertEqual("", normalize_clipboard_text(None))


class BackgroundTaskRunnerTests(unittest.TestCase):
    def test_positional_and_keyword_arguments_are_forwarded(self):
        runner = BackgroundTaskRunner()
        seen = {}
        done = threading.Event()

        def target(a, b, c=None):
            seen.update(a=a, b=b, c=c)
            done.set()

        thread = runner.start(target, 1, 2, c=3)
        self.assertTrue(done.wait(timeout=2))
        thread.join(timeout=2)
        self.assertEqual({"a": 1, "b": 2, "c": 3}, seen)

    def test_thread_is_daemon_by_default_and_named(self):
        runner = BackgroundTaskRunner()
        done = threading.Event()
        thread = runner.start(done.set, name="bg-worker")

        self.assertTrue(done.wait(timeout=2))
        thread.join(timeout=2)
        self.assertTrue(thread.daemon)
        self.assertEqual("bg-worker", thread.name)

    def test_raising_task_is_isolated_from_the_caller(self):
        """A task that raises must not propagate to the launcher nor kill anything.

        The exception is confined to the worker thread (surfaced through
        ``threading.excepthook``), which is what keeps the keyboard listener and
        tray alive when a background job blows up.
        """
        captured = []
        original_hook = threading.excepthook
        threading.excepthook = lambda args: captured.append(args.exc_type)
        started = threading.Event()

        def boom():
            started.set()
            raise RuntimeError("task exploded")

        try:
            runner = BackgroundTaskRunner()
            thread = runner.start(boom)  # must not raise in the caller
            thread.join(timeout=2)
        finally:
            threading.excepthook = original_hook

        self.assertTrue(started.is_set())
        self.assertFalse(thread.is_alive())
        self.assertIn(RuntimeError, captured)


class ConfigureLoggingTests(unittest.TestCase):
    def setUp(self):
        self.logger = logging.getLogger(runtime_support.LOGGER_NAME)
        self._saved_handlers = self.logger.handlers[:]
        self._saved_level = self.logger.level
        self._saved_propagate = self.logger.propagate
        self.logger.handlers = []
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        # Close the file handlers we added so Windows releases the log file
        # before the temp dir is removed, then restore the shared logger state.
        for handler in self.logger.handlers:
            try:
                handler.close()
            except Exception:
                pass
        self.logger.handlers = self._saved_handlers
        self.logger.level = self._saved_level
        self.logger.propagate = self._saved_propagate
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _file_handlers(self):
        return [h for h in self.logger.handlers if isinstance(h, RotatingFileHandler)]

    def test_adds_a_rotating_file_handler_writing_into_the_log_dir(self):
        configure_logging(self.tmp)
        handlers = self._file_handlers()

        self.assertEqual(1, len(handlers))
        expected = os.path.abspath(os.path.join(self.tmp, runtime_support.LOG_FILE_NAME))
        self.assertEqual(expected, os.path.abspath(handlers[0].baseFilename))
        # The handler opens its file eagerly (no delay), so the log exists in the
        # target directory as soon as configure_logging returns.
        self.assertTrue(os.path.exists(expected))

    def test_repeated_calls_do_not_duplicate_the_file_handler(self):
        configure_logging(self.tmp)
        configure_logging(self.tmp)
        self.assertEqual(1, len(self._file_handlers()))

    def test_unwritable_log_dir_warns_and_does_not_crash(self):
        with mock.patch.object(
            runtime_support.os, "makedirs", side_effect=OSError("read-only")
        ), self.assertLogs(runtime_support.LOGGER_NAME, level=logging.WARNING) as logs:
            configure_logging(self.tmp)

        self.assertEqual([], self._file_handlers())
        self.assertTrue(any("log" in line.lower() for line in logs.output))

    def test_console_uses_safe_stderr_and_file_keeps_original_unicode(self):
        stdout = io.TextIOWrapper(
            io.BytesIO(), encoding="cp1252", errors="strict"
        )
        stderr = io.TextIOWrapper(
            io.BytesIO(), encoding="cp1252", errors="backslashreplace"
        )
        self.addCleanup(stdout.close)
        self.addCleanup(stderr.close)
        message = "✓ Unicode diagnostic"

        with mock.patch.object(runtime_support.sys, "stdout", stdout), \
                mock.patch.object(runtime_support.sys, "stderr", stderr):
            configure_logging(self.tmp)
            AppLogger().info(message)
            for handler in self.logger.handlers:
                handler.flush()

        console = stderr.buffer.getvalue().decode("cp1252")
        self.assertEqual(b"", stdout.buffer.getvalue())
        self.assertNotIn("--- Logging error ---", console)
        log_path = os.path.join(self.tmp, runtime_support.LOG_FILE_NAME)
        with open(log_path, encoding="utf-8") as handle:
            file_text = handle.read()

        self.assertIn(r"\u2713 Unicode diagnostic", console)
        self.assertIn(message, file_text)
        stream_handlers = [
            handler
            for handler in self.logger.handlers
            if isinstance(handler, logging.StreamHandler)
            and not isinstance(handler, RotatingFileHandler)
        ]
        self.assertEqual([stderr], [handler.stream for handler in stream_handlers])

    def test_missing_stderr_does_not_fallback_to_strict_stdout(self):
        stdout = io.TextIOWrapper(
            io.BytesIO(), encoding="cp1252", errors="strict"
        )
        self.addCleanup(stdout.close)
        with mock.patch.object(runtime_support.sys, "stdout", stdout), \
                mock.patch.object(runtime_support.sys, "stderr", None):
            configure_logging(self.tmp)

        self.assertEqual([], [
            handler
            for handler in self.logger.handlers
            if isinstance(handler, logging.StreamHandler)
            and not isinstance(handler, RotatingFileHandler)
        ])


class AppLoggerTests(unittest.TestCase):
    def test_levels_delegate_to_the_underlying_logger(self):
        app_logger = AppLogger()
        app_logger._logger = mock.Mock()

        app_logger.info("informação")
        app_logger.warning("aviso")
        app_logger.error("erro")

        app_logger._logger.info.assert_called_once_with("informação")
        app_logger._logger.warning.assert_called_once_with("aviso")
        app_logger._logger.error.assert_called_once_with("erro")


class TextInserterFallbackTests(unittest.TestCase):
    """Insertion-path behavior not already exercised by tests/test_hotpath.py."""

    def test_successful_paste_returns_true_without_typing_or_notifying(self):
        keyboard = mock.Mock()
        notify = mock.Mock()
        inserter = TextInserter(keyboard, notify=notify)

        with mock.patch.object(inserter, "_paste_value", return_value=True):
            self.assertTrue(inserter.insert_text("olá"))

        keyboard.type.assert_not_called()
        notify.assert_not_called()

    def test_multiline_total_failure_reports_neither_paste_nor_copy(self):
        # Paste fails AND the clipboard copy fallback also fails: the user is told
        # the payload could not even be placed for a manual Ctrl+V, and nothing is
        # ever typed (a multi-line typed insert would fire Enter per newline).
        keyboard = mock.Mock()
        notify = mock.Mock()
        logger = mock.Mock()
        clipboard = mock.Mock()
        clipboard.set_content.return_value = False
        snippet = "linha um\nlinha dois"

        with mock.patch.object(runtime_support, "Clipboard", clipboard):
            inserter = TextInserter(keyboard, logger=logger, notify=notify)
            with mock.patch.object(inserter, "_paste_value", return_value=False):
                self.assertFalse(inserter.insert_text(snippet))

        keyboard.type.assert_not_called()
        notify.assert_called_once()
        self.assertIn("nem copiá-lo", notify.call_args.args[0])
        self.assertEqual("paste-failed", notify.call_args.kwargs.get("key"))
        logger.warning.assert_called_once()

    def test_no_prior_clipboard_skips_the_restore_step(self):
        # When there was nothing on the clipboard, there is nothing to restore.
        clipboard = mock.Mock()
        clipboard.get_text.return_value = None
        clipboard.set_content.return_value = True
        inserter = TextInserter(mock.Mock(), restore_delay=0.0)

        with mock.patch.object(runtime_support, "Clipboard", clipboard), \
                mock.patch.object(runtime_support.time, "sleep"), \
                mock.patch.object(inserter, "_send_paste_shortcut"), \
                mock.patch.object(inserter, "_restore_clipboard") as restore:
            self.assertTrue(inserter.insert_text("olá"))

        restore.assert_not_called()

    def test_restore_sees_empty_clipboard_and_stays_silent(self):
        # get_text returns the snapshot first, then None inside _restore_clipboard
        # (a target that cleared the clipboard). No warning, no restore write.
        clipboard = mock.Mock()
        clipboard.get_text.side_effect = ["orig", None]
        clipboard.set_content.return_value = True
        logger = mock.Mock()
        inserter = TextInserter(mock.Mock(), logger=logger, restore_delay=0.0)

        with mock.patch.object(runtime_support, "Clipboard", clipboard), \
                mock.patch.object(runtime_support.time, "sleep"), \
                mock.patch.object(inserter, "_send_paste_shortcut"):
            self.assertTrue(inserter.insert_text("olá"))

        logger.warning.assert_not_called()
        self.assertEqual(1, clipboard.set_content.call_count)  # paste only, no restore

    @unittest.skipUnless(HAS_PYNPUT, "pynput required for the paste shortcut")
    def test_send_paste_shortcut_emits_ctrl_v_sequence(self):
        from pynput.keyboard import Key

        keyboard = mock.Mock()
        inserter = TextInserter(keyboard)

        with mock.patch("platform_support.paste_modifier_is_cmd", return_value=False):
            inserter._send_paste_shortcut()

        self.assertEqual(
            [
                mock.call.press(Key.ctrl),
                mock.call.press("v"),
                mock.call.release("v"),
                mock.call.release(Key.ctrl),
            ],
            keyboard.mock_calls,
        )

    @unittest.skipUnless(HAS_PYNPUT, "pynput required for the paste shortcut")
    def test_send_paste_shortcut_uses_cmd_when_platform_requests_it(self):
        from pynput.keyboard import Key

        keyboard = mock.Mock()
        inserter = TextInserter(keyboard)

        with mock.patch("platform_support.paste_modifier_is_cmd", return_value=True):
            inserter._send_paste_shortcut()

        self.assertEqual(mock.call.press(Key.cmd), keyboard.mock_calls[0])
        self.assertEqual(mock.call.release(Key.cmd), keyboard.mock_calls[-1])


class TextInserterTimingTests(unittest.TestCase):
    """The two paste delays come from platform_support, not from literals."""

    def test_delays_default_to_the_running_platform(self):
        import platform_support

        defaults = platform_support.default_insertion_timings()
        inserter = TextInserter(mock.Mock())
        self.assertEqual(defaults["clipboard_settle_delay"], inserter.settle_delay)
        self.assertEqual(defaults["paste_restore_delay"], inserter.restore_delay)

    def test_paste_sleeps_the_configured_settle_then_restore_delay(self):
        clipboard = mock.Mock()
        clipboard.get_text.return_value = "orig"
        clipboard.set_content.return_value = True
        inserter = TextInserter(mock.Mock(), settle_delay=0.33, restore_delay=0.44)

        with mock.patch.object(runtime_support, "Clipboard", clipboard), \
                mock.patch.object(runtime_support.time, "sleep") as sleep, \
                mock.patch.object(inserter, "_send_paste_shortcut") as paste, \
                mock.patch.object(inserter, "_restore_clipboard"):
            self.assertTrue(inserter.insert_text("olá"))

        # Order is load-bearing: settle before the shortcut, restore delay after.
        self.assertEqual([mock.call(0.33), mock.call(0.44)], sleep.mock_calls)
        paste.assert_called_once_with()

    def test_explicit_zero_delays_are_honored(self):
        # None means "use the platform default"; 0 must stay 0.
        inserter = TextInserter(mock.Mock(), settle_delay=0, restore_delay=0)
        self.assertEqual(0, inserter.settle_delay)
        self.assertEqual(0, inserter.restore_delay)


if __name__ == "__main__":
    unittest.main()
