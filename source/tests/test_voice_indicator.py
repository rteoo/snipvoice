import os
import subprocess
import sys
import textwrap
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from voice_indicator import VISIBLE_STATES, indicator_content


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
        self.assertEqual(title, "Ouvindo…")
        self.assertEqual(accent, "warning")

    def test_command_mode_is_distinct(self):
        title, _accent = indicator_content("recording", "command")
        self.assertIn("comando", title)

    def test_idle_is_hidden(self):
        self.assertIsNone(indicator_content("idle"))
        self.assertIsNone(indicator_content("unavailable"))


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

        panel.update.assert_called_once_with("Ouvindo…", "warning")
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
            indicator.update("recording", "dictation")
            root.update()
            if current_os() == "darwin":
                assert indicator._mac_panel.is_visible()
            else:
                assert indicator.window.state() == "normal"
                assert indicator.title_label.cget("text") == "Ouvindo…"
            if current_os() == "windows":
                user32 = _windows_user32()
                widget_hwnd = indicator.window.winfo_id()
                hwnd = user32.GetParent(widget_hwnd) or widget_hwnd
                assert user32.GetWindowLongW(hwnd, _GWL_EXSTYLE) & _WS_EX_NOACTIVATE
            indicator.update("idle")
            root.update()
            if current_os() == "darwin":
                assert not indicator._mac_panel.is_visible()
            else:
                assert indicator.window.state() == "withdrawn"
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
