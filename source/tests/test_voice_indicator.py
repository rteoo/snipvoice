import os
import subprocess
import sys
import textwrap
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from voice_indicator import VISIBLE_STATES, indicator_content, indicator_subtitle, meeting_indicator_content


MAIN_TK_AVAILABLE = (
    subprocess.run(
        [sys.executable, "-c", "import tkinter; tkinter.Tk().destroy()"],
        capture_output=True,
    ).returncode
    == 0
)


class VoiceIndicatorContentTests(unittest.TestCase):
    def test_visible_states_have_copy(self):
        for state in VISIBLE_STATES:
            with self.subTest(state=state):
                self.assertIsNotNone(indicator_content(state))

    def test_recording_explains_release_and_cancel(self):
        title, accent = indicator_content("recording", "dictation")
        self.assertEqual(title, "Ouvindo")
        self.assertEqual(accent, "warning")
        self.assertIn("Solte para transcrever", indicator_subtitle("recording", "dictation"))
        self.assertIn("Esc cancela", indicator_subtitle("recording", "dictation"))

    def test_command_mode_is_distinct(self):
        title, _accent = indicator_content("recording", "command")
        self.assertIn("comando", title)

    def test_idle_is_hidden(self):
        self.assertIsNone(indicator_content("idle"))
        self.assertIsNone(indicator_content("unavailable"))

    def test_meeting_elapsed_pause_and_partial_capture_are_distinct(self):
        state, title, _, subtitle = meeting_indicator_content({"state": "recording", "elapsed": 3676})
        self.assertEqual(state, "recording")
        self.assertEqual(title, "Gravando · 01:01:16")
        self.assertNotIn("Solte", subtitle)
        self.assertIn("Pausado", meeting_indicator_content({"state": "paused"})[1])
        content = meeting_indicator_content({"state": "recording", "partial": True})
        self.assertIn("parcial", content[1])
        self.assertIn("perdeu áudio", content[3])

    def test_meeting_interruption_remains_visible_until_review(self):
        for status in ("failed", "interrupted"):
            content = meeting_indicator_content({"state": "idle", "last_status": status})
            self.assertEqual(content[1], "Gravação interrompida")
        self.assertEqual(meeting_indicator_content({"state": "idle", "last_status": "partial"})[1],
                         "Gravação parcial salva")
        self.assertIsNone(meeting_indicator_content({"state": "idle", "last_status": "completed"}))
        self.assertIsNone(meeting_indicator_content({"state": "idle"}))

    def test_meeting_starting_and_saving_do_not_claim_active_capture(self):
        for state in ("starting", "stopping", "postprocessing"):
            content = meeting_indicator_content({"state": state, "elapsed": float("nan")})
            self.assertEqual(content[0], "transcribing")
            self.assertNotIn("Gravando", content[1])


class MacVoiceIndicatorRoutingTests(unittest.TestCase):
    def test_macos_uses_the_native_nonactivating_panel(self):
        from voice_indicator import VoiceStatusIndicator

        panel = mock.Mock()
        with mock.patch("voice_indicator.current_os", return_value="darwin"), \
                mock.patch("voice_indicator.MacVoiceStatusPanel", return_value=panel):
            indicator = VoiceStatusIndicator(mock.Mock())
            indicator.update("recording", "dictation")
            indicator.hide()
            indicator.destroy()

        panel.update.assert_called_once_with("Ouvindo", "warning")
        panel.hide.assert_called_once_with()
        panel.destroy.assert_called_once_with()
        self.assertIsNone(indicator.window)


@unittest.skipUnless(MAIN_TK_AVAILABLE, "Tk display not available")
class VoiceIndicatorGuiSmokeTests(unittest.TestCase):
    def test_overlay_shows_and_hides_in_an_isolated_tk_process(self):
        script = textwrap.dedent(
            """
            import tkinter as tk
            from platform_support import current_os
            from voice_indicator import (
                VoiceStatusIndicator,
                _GWL_EXSTYLE,
                _WS_EX_NOACTIVATE,
                _windows_user32,
            )

            root = tk.Tk()
            root.withdraw()
            if current_os() == "darwin":
                import AppKit
                AppKit.NSApplication.sharedApplication().setActivationPolicy_(
                    AppKit.NSApplicationActivationPolicyAccessory
                )
            indicator = VoiceStatusIndicator(root)
            if current_os() == "windows":
                import ctypes
                from ctypes import wintypes
                user32 = _windows_user32()
                user32.GetForegroundWindow.restype = wintypes.HWND
                foreground = user32.GetForegroundWindow()
            indicator.update("recording", "dictation")
            root.update()
            if current_os() == "darwin":
                assert indicator._mac_panel.is_visible()
            else:
                assert indicator.window.state() == "normal"
                assert indicator.title_label.cget("text") == "Ouvindo"
                assert "Solte para transcrever" in indicator.subtitle_label.cget("text")
                assert len(indicator.waveform_bars) == 5
            if current_os() == "windows":
                user32 = _windows_user32()
                widget_hwnd = indicator.window.winfo_id()
                hwnd = user32.GetParent(widget_hwnd) or widget_hwnd
                assert user32.GetWindowLongW(hwnd, _GWL_EXSTYLE) & _WS_EX_NOACTIVATE
                assert user32.GetForegroundWindow() == foreground, "Overlay stole focus"
            indicator.update("idle")
            root.update()
            if current_os() == "darwin":
                assert not indicator._mac_panel.is_visible()
            else:
                assert indicator.window.state() == "withdrawn"
            indicator.update_meeting({"state": "recording", "elapsed": 136})
            root.update()
            if current_os() == "darwin":
                assert indicator._mac_panel.is_visible()
            else:
                assert indicator.title_label.cget("text") == "Gravando · 00:02:16"
            indicator.update_meeting({"state": "paused", "elapsed": 136})
            root.update()
            indicator.update_meeting({"state": "idle", "last_status": "failed"})
            root.update()
            indicator.update_meeting({"state": "idle", "last_status": "completed"})
            root.update()
            if current_os() == "windows":
                import sys
                sys.path.insert(0, "tests")
                from app_module import snipvoice
                app = snipvoice.Snipvoice.__new__(snipvoice.Snipvoice)
                app.voice_status_indicator = indicator
                manager = tk.Toplevel(root)
                app.manager_window = manager
                manager.withdraw()
                app._render_recording_indicator(root, {"state": "recording", "elapsed": 136})
                root.update()
                assert indicator.window.state() == "normal"
                manager.deiconify()
                root.update()
                app._render_recording_indicator(root, {"state": "recording", "elapsed": 136})
                assert indicator.window.state() == "withdrawn"
                manager.iconify()
                root.update()
                assert manager.state() == "iconic"
                app._render_recording_indicator(root, {"state": "recording", "elapsed": 136})
                root.update()
                assert indicator.window.state() == "normal"
                manager.destroy()
            indicator.destroy()
            root.destroy()
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
