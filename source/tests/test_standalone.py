"""Verify the independent app boundary without a microphone or desktop mutation."""

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from app_module import snipvoice as app
import voice_models

TMP = Path(__file__).resolve().parent / "tmp"
TMP.mkdir(exist_ok=True)


class StandaloneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TMP)
        self.addCleanup(self.temp.cleanup)
        with mock.patch.dict(os.environ, {"SNIPVOICE_HOME": self.temp.name}), \
                mock.patch.object(app, "Controller"), \
                mock.patch.object(app, "configure_logging"):
            self.instance = app.Snipvoice()

    def test_data_and_listener_are_independent(self):
        self.assertEqual(self.instance.data_dir, self.temp.name)
        self.assertFalse(self.instance.voice.is_enabled())
        self.assertFalse(hasattr(self.instance, "on_press"))
        self.assertFalse(hasattr(self.instance, "run_keyboard_listener"))
        self.assertEqual(app.APP_MUTEX_NAME, r"Local\SnipvoiceSingleton")
        self.assertEqual(Path(app.platform_support.default_autostart_command()[-1]).name, "snipvoice.pyw")

    def test_legacy_home_override_is_ignored(self):
        with mock.patch.dict(os.environ, {"SNIPTYPE_HOME": "legacy", "SNIPVOICE_HOME": ""}), \
                mock.patch.object(app.os.path, "expanduser", return_value=self.temp.name), \
                mock.patch.object(app.os, "makedirs"):
            self.assertEqual(app.ensure_data_dir(), self.temp.name)

    def test_legacy_cache_overrides_are_ignored(self):
        with mock.patch.dict(os.environ, {"SNIPTYPE_VOICE_CACHE": "legacy",
                                          "TXT_XPANDER_VOICE_CACHE": "legacy2"}), \
                mock.patch.dict(os.environ, {"SNIPVOICE_VOICE_CACHE": ""}):
            path = voice_models.default_voice_cache_dir("windows")
        self.assertIn("Snipvoice", path)
        self.assertNotIn("legacy", path)

    def test_spoken_commands_insert_literal_text(self):
        command_file = Path(self.temp.name) / "commands.json"
        command_file.write_text(json.dumps({"hello": "Hello %%name%%"}), encoding="utf-8")
        self.assertTrue(self.instance._load_commands())
        self.instance.text_inserter = mock.Mock()
        self.instance.text_inserter.insert_text.return_value = True
        self.assertTrue(self.instance.expand_from_voice("hello"))
        self.instance.text_inserter.insert_text.assert_called_once_with("Hello %%name%%")
        self.assertFalse(self.instance.expand_from_voice("missing"))

    def test_bad_commands_preserve_last_loaded_values_and_bytes(self):
        path = Path(self.temp.name) / "commands.json"
        path.write_text('{"hello": "Hello"}', encoding="utf-8")
        self.assertTrue(self.instance._load_commands())
        path.write_text('{"hello": 42}', encoding="utf-8")
        self.assertFalse(self.instance._load_commands())
        self.assertEqual(self.instance.snippets, {"hello": "Hello"})
        self.assertEqual(path.read_text(encoding="utf-8"), '{"hello": 42}')

    def test_setting_save_preserves_other_keys(self):
        Path(self.instance.settings_file).write_text('{"custom": 3}', encoding="utf-8")
        self.assertTrue(self.instance._persist_voice_settings({"voice_enabled": True}))
        self.assertEqual(app.load_settings(self.instance.settings_file), {"custom": 3, "voice_enabled": True})

    def test_failed_setting_save_keeps_live_settings(self):
        before = dict(self.instance.settings)
        with mock.patch.object(app, "save_settings", return_value=False):
            self.assertFalse(self.instance._persist_voice_settings({"voice_enabled": True}))
        self.assertEqual(self.instance.settings, before)

    def test_voice_shutdown_runs_off_tray_callback(self):
        self.instance.voice = mock.Mock()
        self.instance.task_runner = mock.Mock()
        self.instance.quit_app(mock.Mock(), None)
        self.instance.voice.shutdown.assert_not_called()
        self.instance.task_runner.start.assert_called_once_with(self.instance._shutdown, name="voice-shutdown")

    def test_permission_denial_has_a_restart_instruction(self):
        self.instance.notify_error = mock.Mock()
        self.instance.voice = mock.Mock()
        self.instance.voice.is_enabled.return_value = False
        with mock.patch.object(app.platform_support, "IS_MAC", True), \
                mock.patch.object(app.platform_support, "autostart_state", return_value="absent"), \
                mock.patch.object(app.macos_permissions, "check_permissions", return_value={"input_monitoring": "denied"}):
            self.instance._resolve_startup()
        self.instance.notify_error.assert_called_once()
        self.assertIn("reinicie", self.instance.notify_error.call_args.args[0])

    def test_probe_exits_before_mutex_or_tray(self):
        probe = mock.Mock(return_value=0)
        with mock.patch.dict(sys.modules, {"voice_runtime_probe": types.SimpleNamespace(main=probe)}), \
                mock.patch.object(app, "acquire_single_instance_mutex") as mutex, \
                self.assertRaises(SystemExit) as raised:
            app.run_voice_runtime_probe_if_requested(["--voice-runtime-probe"])
        self.assertEqual(raised.exception.code, 0)
        mutex.assert_not_called()
        probe.assert_called_once_with()

    def test_summary_probe_exits_before_mutex_or_tray(self):
        probe = mock.Mock(return_value=0)
        with mock.patch.dict(sys.modules, {"summary_runtime_probe": types.SimpleNamespace(main=probe)}), \
                mock.patch.object(app, "acquire_single_instance_mutex") as mutex, \
                self.assertRaises(SystemExit) as raised:
            app.run_summary_runtime_probe_if_requested(["--summary-runtime-probe"])
        self.assertEqual(raised.exception.code, 0)
        mutex.assert_not_called()
        probe.assert_called_once_with()

    def test_failed_autostart_change_reports_failure(self):
        self.instance.notify_error = mock.Mock()
        with mock.patch.object(app.platform_support, "install_autostart", return_value=False), \
                mock.patch.object(app.platform_support, "autostart_state", return_value="absent"):
            self.instance._toggle_autostart()
        self.instance.notify_error.assert_called_once()
        self.assertEqual(self.instance._autostart_state, "absent")

    def test_startup_creates_root_before_mac_tray(self):
        self.instance.gui = mock.Mock()
        self.instance.gui.adopt_main_thread.return_value = True
        events = []
        self.instance.gui.adopt_main_thread.side_effect = lambda: events.append("root") or True
        self.instance.gui.run_mainloop.side_effect = lambda: events.append("loop")
        icon = mock.Mock()
        with mock.patch.object(app.platform_support, "tk_runs_on_main_thread", return_value=True), \
                mock.patch.object(app.platform_support, "hide_dock_icon", side_effect=lambda: events.append("dock")), \
                mock.patch.object(app.platform_support, "tray_icon_options", return_value={}), \
                mock.patch.object(app.pystray, "Icon", side_effect=lambda *args, **kwargs: events.append("tray") or icon):
            self.instance.run()
        self.assertEqual(events, ["root", "dock", "tray", "loop"])
        icon.run_detached.assert_called_once_with(setup=self.instance.on_tray_ready)
        icon.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
