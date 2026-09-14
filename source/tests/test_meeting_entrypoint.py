import builtins
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class MeetingEntrypointTests(unittest.TestCase):
    def test_capture_probe_exits_before_desktop_or_user_data_initialization(self):
        import meeting_audio
        path = Path(__file__).resolve().parents[1] / "snipvoice.pyw"
        code = compile(path.read_text(encoding="utf-8"), str(path), "exec")
        original_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name in {"platform_support", "tkinter", "app_paths", "pystray", "pynput.keyboard"}:
                self.fail("A non-recording capture probe imported desktop code")
            return original_import(name, *args, **kwargs)

        with mock.patch.object(sys, "argv", [str(path), "--meeting-capture-probe"]), \
                mock.patch.object(meeting_audio.NativeCapture, "self_test", return_value=True) as probe, \
                mock.patch("builtins.__import__", side_effect=guarded_import):
            with self.assertRaises(SystemExit) as stopped:
                exec(code, {"__name__": "__main__", "__file__": str(path)})
        self.assertEqual(stopped.exception.code, 0)
        probe.assert_called_once_with()
