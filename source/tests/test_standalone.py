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
                mock.patch.object(app.app_paths, "config_dir", return_value=self.temp.name), \
                mock.patch.object(app.os.path, "expanduser", return_value=self.temp.name), \
                mock.patch.object(app.os, "makedirs"):
            self.assertEqual(app.app_paths.ensure_data_dir(), self.temp.name)

    def test_data_relocation_waits_for_idle_meetings(self):
        self.instance.meetings = mock.Mock()
        self.instance.meetings.is_busy.return_value = True
        self.instance.quit_app = mock.Mock()
        message = self.instance.request_data_relocation(os.path.join(self.temp.name, "x"))
        self.assertIn("Aguarde", message)
        self.instance.quit_app.assert_not_called()
        self.assertFalse(self.instance.relaunch_requested)

    def test_data_relocation_records_the_move_then_quits_to_relaunch(self):
        config = os.path.join(self.temp.name, "config")
        target = os.path.join(self.temp.name, "moved")
        self.instance.data_dir = os.path.join(self.temp.name, "home")
        os.makedirs(self.instance.data_dir)
        self.instance.meetings = mock.Mock()
        self.instance.meetings.is_busy.return_value = False
        self.instance.quit_app = mock.Mock()
        with mock.patch.dict(os.environ, {"SNIPVOICE_HOME": ""}), \
                mock.patch.object(app.app_paths, "config_dir", return_value=config):
            self.assertEqual(self.instance.request_data_relocation(target), "")
            location = app.app_paths.read_location()
        self.assertEqual(location["pending_move"], {"from": self.instance.data_dir, "to": target})
        self.assertTrue(self.instance.relaunch_requested)
        self.instance.quit_app.assert_called_once_with(None, None)

    def test_data_relocation_error_is_returned_without_quitting(self):
        self.instance.meetings = mock.Mock()
        self.instance.meetings.is_busy.return_value = False
        self.instance.quit_app = mock.Mock()
        with mock.patch.dict(os.environ, {"SNIPVOICE_HOME": self.temp.name}):
            message = self.instance.request_data_relocation(os.path.join(self.temp.name, "x"))
        self.assertIn("SNIPVOICE_HOME", message)
        self.instance.quit_app.assert_not_called()

    def test_models_relocation_waits_for_downloads(self):
        self.instance.meetings = mock.Mock()
        self.instance.meetings.is_busy.return_value = False
        self.instance.voice = mock.Mock()
        self.instance.voice.model_download_in_progress.return_value = True
        self.instance.quit_app = mock.Mock()
        message = self.instance.request_models_relocation(os.path.join(self.temp.name, "models"))
        self.assertIn("downloads", message)
        self.instance.quit_app.assert_not_called()

    def test_models_relocation_records_the_move_then_quits_to_relaunch(self):
        config = os.path.join(self.temp.name, "config")
        current = os.path.join(self.temp.name, "local", "Snipvoice")
        target = os.path.join(self.temp.name, "shared-models")
        os.makedirs(current)
        self.instance.meetings = mock.Mock()
        self.instance.meetings.is_busy.return_value = False
        self.instance.voice = mock.Mock()
        self.instance.voice.model_download_in_progress.return_value = False
        self.instance.quit_app = mock.Mock()
        with mock.patch.dict(os.environ, {"SNIPVOICE_VOICE_CACHE": "", "SNIPVOICE_SUMMARY_CACHE": ""}), \
                mock.patch.object(app.app_paths, "config_dir", return_value=config), \
                mock.patch.object(app.app_paths, "default_models_dir", return_value=current):
            self.assertEqual(self.instance.request_models_relocation(target), "")
            location = app.app_paths.read_location()
        self.assertEqual(location["pending_models_move"], {"from": current, "to": target})
        self.assertTrue(self.instance.relaunch_requested)
        self.instance.quit_app.assert_called_once_with(None, None)

    def test_startup_notices_are_shown_when_the_tray_is_ready(self):
        self.instance._startup_notices = ["Dados movidos para D."]
        self.instance.notify_error = mock.Mock()
        self.instance.task_runner = mock.Mock()
        self.instance.on_tray_ready(mock.Mock())
        self.instance.notify_error.assert_called_once_with("Dados movidos para D.", key="data-dir-0")

    def test_main_completes_a_pending_move_before_opening_the_app_then_relaunches(self):
        events = []
        attempts = iter([False, False, True])
        instance = mock.Mock(relaunch_requested=True)

        def construct(startup_notices=()):
            events.append(("app", startup_notices))
            return instance

        with mock.patch.object(app.sys, "argv", ["snipvoice.pyw", app.RELAUNCH_FLAG]), \
                mock.patch.object(app.platform_support, "IS_WINDOWS", True), \
                mock.patch.object(app, "acquire_single_instance_mutex", side_effect=lambda: next(attempts)), \
                mock.patch.object(app.time, "sleep"), \
                mock.patch.object(app.data_relocation, "complete_pending_relocation",
                                  side_effect=lambda: events.append("move") or "moved"), \
                mock.patch.object(app.data_relocation, "complete_pending_models_relocation",
                                  side_effect=lambda: events.append("models") or ""), \
                mock.patch.object(app, "Snipvoice", side_effect=construct), \
                mock.patch.object(app, "relaunch", side_effect=lambda: events.append("relaunch")):
            app.main()
        self.assertEqual(events, ["move", "models", ("app", ("moved", "")), "relaunch"])
        instance.run.assert_called_once_with(show_settings=False)

    def test_main_without_relaunch_flag_does_not_wait_for_the_lock(self):
        with mock.patch.object(app.sys, "argv", ["snipvoice.pyw"]), \
                mock.patch.object(app.platform_support, "IS_WINDOWS", True), \
                mock.patch.object(app, "acquire_single_instance_mutex", return_value=False) as mutex, \
                mock.patch.object(app.time, "sleep") as sleep, \
                mock.patch.object(app.data_relocation, "complete_pending_relocation") as move:
            app.main()
        mutex.assert_called_once_with()
        sleep.assert_not_called()
        move.assert_not_called()

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

    def test_meeting_startup_recovers_before_rebuilding_hotkey(self):
        events = []
        self.instance.meetings = mock.Mock()
        self.instance.meetings.refresh_privacy_defaults.side_effect = (
            lambda: events.append("privacy")
        )
        self.instance.meetings.recover_retention_operations.side_effect = (
            lambda: events.append("recovery") or ()
        )
        self.instance.voice = mock.Mock()
        self.instance.voice.is_enabled.return_value = False
        self.instance.gui = mock.Mock()
        self.instance.refresh_tray_menu = mock.Mock()
        self.instance._rebuild_meeting_monitor = mock.Mock(
            side_effect=lambda: events.append("hotkey")
        )
        with mock.patch.object(app.platform_support, "IS_MAC", False), \
                mock.patch.object(app.platform_support, "autostart_state", return_value="absent"):
            self.instance._resolve_startup()
        self.assertEqual(events, ["privacy", "recovery", "hotkey"])
        self.assertTrue(self.instance._meeting_startup_ready)

    def test_meeting_recovery_error_blocks_hotkey_without_disclosing_path(self):
        self.instance.meetings = mock.Mock()
        self.instance.meetings.refresh_privacy_defaults.return_value = None
        self.instance.meetings.recover_retention_operations.side_effect = OSError(
            r"C:\Users\private\meetings\retention.json"
        )
        self.instance.voice = mock.Mock()
        self.instance.voice.is_enabled.return_value = False
        self.instance.gui = mock.Mock()
        self.instance.notify_error = mock.Mock()
        self.instance.refresh_tray_menu = mock.Mock()
        self.instance._rebuild_meeting_monitor = mock.Mock()
        with mock.patch.object(app.platform_support, "IS_MAC", False), \
                mock.patch.object(app.platform_support, "autostart_state", return_value="absent"):
            self.instance._resolve_startup()
        self.assertFalse(self.instance._meeting_startup_ready)
        self.assertEqual(self.instance._meeting_startup_error, "OSError")
        self.instance._rebuild_meeting_monitor.assert_not_called()
        messages = [call.args[0] for call in self.instance.notify_error.call_args_list]
        self.assertTrue(messages)
        self.assertTrue(all("C:\\Users" not in message for message in messages))
        self.assertTrue(any("revisão manual" in message for message in messages))

    def test_meeting_hotkey_callback_only_queues_gui_work(self):
        self.instance._meeting_startup_ready = True
        self.instance.gui = mock.Mock()
        self.instance.gui.submit.return_value = True
        self.instance.meetings = mock.Mock()
        self.assertTrue(self.instance._meeting_hotkey_request())
        self.instance.gui.submit.assert_called_once()
        queued = self.instance.gui.submit.call_args.args[0]
        self.assertEqual(getattr(queued, "__name__", ""), "_meeting_hotkey_on_gui")
        self.instance.meetings.snapshot.assert_not_called()

        self.instance._meeting_startup_ready = False
        self.assertFalse(self.instance._meeting_hotkey_request())
        self.assertEqual(self.instance.gui.submit.call_count, 1)

    def test_tray_start_and_stop_callbacks_only_queue_gui_work(self):
        self.instance._meeting_startup_ready = True
        self.instance.gui = mock.Mock()
        self.instance.gui.submit.return_value = True
        self.assertTrue(self.instance.request_meeting_start())
        self.assertTrue(self.instance.request_meeting_stop())
        self.assertEqual(self.instance.gui.submit.call_count, 2)
        names = [getattr(call.args[0], "__name__", "")
                 for call in self.instance.gui.submit.call_args_list]
        self.assertEqual(names, ["_request_meeting_start_on_gui", "_request_meeting_stop_on_gui"])

    def test_tray_stop_without_open_window_runs_controller_off_tk(self):
        self.instance._manager_meeting_view = None
        self.instance.meetings = mock.Mock()
        self.instance.task_runner = mock.Mock()
        self.instance._request_meeting_stop_on_gui(mock.Mock())
        self.instance.meetings.stop.assert_not_called()
        self.instance.task_runner.start.assert_called_once_with(
            self.instance.meetings.stop, name="meeting-stop",
        )

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

    def test_packaged_autostart_opens_windows_startup_settings(self):
        self.instance._autostart_state = app.platform_support.AUTOSTART_MANAGED
        self.instance.refresh_tray_menu = mock.Mock()
        with mock.patch.object(app.platform_support, "open_startup_settings") as open_settings, \
                mock.patch.object(app.platform_support, "install_autostart") as install:
            self.instance._toggle_autostart()
        open_settings.assert_called_once_with()
        install.assert_not_called()
        self.assertEqual(self.instance._autostart_menu_label(), "Iniciar com o sistema…")

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

    def test_tray_default_opens_recording_and_hides_manual_command_reload(self):
        self.instance.gui = mock.Mock()
        self.instance.gui.ensure_started.return_value = True
        self.instance.task_runner = mock.Mock()
        icon = mock.Mock()
        image = mock.MagicMock()
        image.__enter__.return_value.copy.return_value = object()
        items = []

        def menu_item(text, action, **options):
            items.append((text, action, options))
            return mock.Mock()

        with mock.patch.object(app.platform_support, "tk_runs_on_main_thread", return_value=False), \
                mock.patch.object(app.platform_support, "tray_icon_options", return_value={}), \
                mock.patch.object(app.pystray, "MenuItem", side_effect=menu_item), \
                mock.patch.object(app.pystray, "Menu", side_effect=lambda *entries: entries), \
                mock.patch.object(app.pystray, "Icon", return_value=icon), \
                mock.patch.object(app.Image, "open", return_value=image):
            self.instance.run()

        # Labels are callables so a language change re-renders the menu.
        labels = [text(None) if callable(text) else text for text, _action, _options in items]
        self.assertEqual(labels[0], "Abrir Gravação…")
        self.assertEqual(items[0][1], self.instance.open_meetings)
        self.assertTrue(items[0][2]["default"])
        self.assertNotIn("Recarregar comandos", labels)
        with mock.patch.object(app.i18n, "_language", "en-US"):
            self.assertEqual(items[0][0](None), "Open Recording…")


if __name__ == "__main__":
    unittest.main()
