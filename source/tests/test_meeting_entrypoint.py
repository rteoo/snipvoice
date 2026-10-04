import builtins
from pathlib import Path
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))


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

    def test_sqlite_probe_exits_before_desktop_or_user_data_initialization(self):
        path = Path(__file__).resolve().parents[1] / "snipvoice.pyw"
        code = compile(path.read_text(encoding="utf-8"), str(path), "exec")
        with mock.patch.object(sys, "argv", [str(path), "--sqlite-runtime-probe"]):
            with self.assertRaises(SystemExit) as stopped:
                exec(code, {"__name__": "__main__", "__file__": str(path)})
        self.assertEqual(stopped.exception.code, 0)


class RecordingIndicatorEntrypointTests(unittest.TestCase):
    def setUp(self):
        from app_module import snipvoice

        self.app = snipvoice.Snipvoice.__new__(snipvoice.Snipvoice)
        self.app.voice_status_indicator = mock.Mock()
        self.app.manager_window = None
        self.app.meetings = mock.Mock()
        self.app.voice = mock.Mock()
        self.app.voice.status_snapshot.return_value = {"state": "idle", "mode": None}
        self.app._quitting = threading.Event()
        self.app._recording_indicator_after = None
        self.root = mock.Mock()

    def test_poll_survives_closed_manager_and_observes_controller_state(self):
        for state in ("starting", "recording", "paused", "stopping", "postprocessing"):
            snapshot = {"state": state, "elapsed": 136}
            self.app.meetings.snapshot.return_value = snapshot
            self.app._poll_recording_indicator(self.root)
            self.app.voice_status_indicator.update_meeting.assert_called_with(snapshot)
            self.root.after.assert_called_with(200, mock.ANY)

    def test_recording_indicator_follows_manager_minimize_and_restore(self):
        self.app.manager_window = mock.Mock()
        snapshot = {"state": "recording"}
        for state in ("iconic", "withdrawn"):
            self.app.manager_window.state.return_value = state
            self.app._render_recording_indicator(self.root, snapshot)
            self.app.voice_status_indicator.update_meeting.assert_called_with(snapshot)
        self.app.manager_window.state.return_value = "normal"
        self.app._render_recording_indicator(self.root, snapshot)
        self.app.voice_status_indicator.hide.assert_called_once_with()

    def test_poll_leaves_active_dictation_indicator_owned_by_voice(self):
        self.app.meetings.snapshot.return_value = {"state": "idle"}
        self.app.voice.status_snapshot.return_value = {"state": "recording"}
        self.app._poll_recording_indicator(self.root)
        self.app.voice_status_indicator.update_meeting.assert_not_called()
        self.app.voice_status_indicator.hide.assert_not_called()

    def test_shutdown_stops_poll_and_hides_overlay(self):
        self.app._quitting.set()
        self.app._poll_recording_indicator(self.root)
        self.app.voice_status_indicator.hide.assert_called_once_with()
        self.root.after.assert_not_called()
