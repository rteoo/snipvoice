"""Device chooser identity, feedback, native popup, and keyboard regressions."""

import gc
import time
import tkinter as tk
from tkinter import font as tkfont
import unittest
from unittest import mock

from meeting_gui import MeetingWindow, endpoint_hint, endpoint_options
from meeting_settings import EndpointSelection
import ui_theme


DEVICES = [
    {"id": "mic-usb", "name": "Microfone USB", "kind": "microphone",
     "default": True, "communications_default": False},
    {"id": "mic-headset", "name": "Microfone do headset", "kind": "microphone",
     "default": False, "communications_default": True},
    {"id": "headset", "name": "Fones de ouvido USB", "kind": "system",
     "default": True, "communications_default": True},
    {"id": "speakers", "name": "Alto-falantes", "kind": "system",
     "default": False, "communications_default": False},
]


class DeviceChoiceTests(unittest.TestCase):
    def test_automatic_choices_name_the_matching_role_without_pinning_it(self):
        options = endpoint_options(DEVICES, "microphone", EndpointSelection())
        self.assertEqual(options[0][0], "Padrão do sistema · Microfone USB")
        self.assertEqual(options[1][0], "Padrão para chamadas · Microfone do headset")
        self.assertEqual(options[0][1].argument(), "default:multimedia")
        self.assertEqual(options[1][1].argument(), "default:communications")
        self.assertEqual(options[2][1].argument(), "mic-usb")

    def test_missing_selection_label_cannot_collide_with_a_real_device_name(self):
        selection = EndpointSelection("manual", "disconnected")
        options = endpoint_options([
            {"id": "connected", "kind": "microphone",
             "name": "Dispositivo selecionado indisponível"},
        ], "microphone", selection)
        self.assertEqual(len(dict(options)), len(options))
        self.assertEqual(options[-1][1], selection)

    def test_hints_distinguish_disabled_automatic_fixed_missing_and_loading(self):
        for selection, kwargs, expected, warning in (
            (EndpointSelection(), {}, "Automático", False),
            (EndpointSelection(default_role="communications"), {}, "chamadas", False),
            (EndpointSelection("manual", "mic-usb"), {}, "sempre este dispositivo", False),
            (EndpointSelection("manual", "gone"), {}, "desconectado", True),
            (EndpointSelection("manual", "gone"), {"enabled": False}, "Desativado", False),
            (EndpointSelection(), {"loaded": False}, "Buscando", False),
        ):
            with self.subTest(expected=expected):
                text, flagged = endpoint_hint(DEVICES, "microphone", selection, **kwargs)
                self.assertIn(expected, text)
                self.assertEqual(flagged, warning)

    def test_no_default_device_is_actionable_without_exposing_identifiers(self):
        text, warning = endpoint_hint([], "system", EndpointSelection())
        self.assertTrue(warning)
        self.assertIn("Conecte", text)
        devices = [dict(DEVICES[0], default=False)]
        self.assertTrue(endpoint_hint(devices, "microphone", EndpointSelection())[1])
        self.assertTrue(endpoint_hint(DEVICES[:2], "system", EndpointSelection())[1])

    def test_refresh_only_reports_success_after_worker_completion(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.preview_status = mock.Mock()
        view.status = mock.Mock()
        view.device_refresh_button = mock.Mock()
        view.controller = mock.Mock()
        view._submit = mock.Mock(return_value=True)
        view._render_devices = mock.Mock()
        view._remember_operation_error = mock.Mock()
        view.refresh_devices()
        view.preview_status.set.assert_called_once_with("Buscando dispositivos…")
        view.device_refresh_button.configure.assert_called_with(text="Atualizando…", state="disabled")
        view._devices_loaded(DEVICES, None)
        self.assertTrue(view.devices_loaded)
        self.assertEqual(view.devices, DEVICES)
        view._render_devices.assert_called_once()
        view.device_refresh_button.configure.assert_called_with(text="Atualizar dispositivos", state="normal")
        view._devices_loaded(None, "synthetic failure")
        self.assertEqual(view.devices, DEVICES)
        self.assertIn("Não foi possível", view.preview_status.set.call_args.args[0])


class DeviceMenuNativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
            cls.root.withdraw()
        except tk.TclError as exc:
            raise unittest.SkipTest(f"Native Tk unavailable: {exc}") from exc

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()
        cls.root = None
        gc.collect()

    def setUp(self):
        self.callback_errors = []
        previous = self.root.report_callback_exception
        self.root.report_callback_exception = lambda _kind, value, _tb: self.callback_errors.append(str(value))
        self.addCleanup(setattr, self.root, "report_callback_exception", previous)
        self.addCleanup(self.assert_no_callback_errors)

    def assert_no_callback_errors(self):
        self.root.update()
        self.assertEqual(self.callback_errors, [])

    def make_view(self, preference="dark"):
        controller = mock.Mock()
        controller.snapshot.return_value = {"state": "idle", "levels": {}, "elapsed": 0, "processing": False}
        controller.devices.return_value = DEVICES
        controller.list_sessions.return_value = []
        controller.read_workspace.return_value = {"generation": 0, "collections": [], "series": []}
        # Keep this native Tk smoke portable: the preference exercises the
        # Windows palette, while the system must describe the host running Tk.
        # ``SystemButtonText`` is valid on Win32 but rejected by X11 Tk.
        theme = ui_theme.build_theme(preference, system=ui_theme.current_os())
        with mock.patch("meeting_gui.ui_theme.bind", return_value=theme):
            view = MeetingWindow(self.root, controller, lambda: {}, mock.Mock())
        self.addCleanup(view.close)
        view.window.geometry("920x700+20+20")
        deadline = time.monotonic() + 3
        while not (view.settings_loaded and view.devices_loaded):
            self.root.update()
            if time.monotonic() > deadline:
                self.fail("Device/settings worker did not complete")
            time.sleep(.01)
        self.root.update()
        return view

    def test_native_popup_palette_keyboard_commit_and_escape(self):
        for preference in ("dark", "windows"):
            with self.subTest(preference=preference):
                view = self.make_view(preference)
                combo = view.endpoint_boxes["microphone"]
                view.window.deiconify()
                view.window.update()
                combo.tk.call("ttk::combobox::Post", str(combo))
                self.root.update()
                popup = str(combo.tk.call("ttk::combobox::PopdownWindow", str(combo)))
                listbox = popup + ".f.l"
                for option, expected in (("background", view.ui.field), ("foreground", view.ui.text),
                                         ("selectbackground", view.ui.select_bg),
                                         ("selectforeground", view.ui.select_fg)):
                    self.assertEqual(combo.tk.call(listbox, "cget", "-" + option), expected)
                combo.tk.call("event", "generate", listbox, "<KeyPress-Down>")
                combo.tk.call("event", "generate", listbox, "<KeyPress-Return>")
                self.root.update()
                self.assertEqual(view._current_settings().microphone.argument(), "default:communications")
                self.assertIn("chamadas", view.endpoint_hints["microphone"].cget("text"))
                combo.tk.call("ttk::combobox::Post", str(combo))
                self.root.update()
                combo.tk.call("event", "generate", listbox, "<KeyPress-Down>")
                combo.tk.call("event", "generate", listbox, "<KeyPress-Escape>")
                self.root.update()
                self.assertEqual(view._current_settings().microphone.argument(), "default:communications")
                self.assertFalse(int(combo.tk.call("winfo", "ismapped", popup)))
                view.close()
                self.root.update()

    def test_refresh_preserves_manual_identity_and_disabled_source(self):
        view = self.make_view()
        view.endpoint_vars["microphone"].set(view.options["microphone"][3][0])
        view._source_selection_changed()
        view._devices_loaded(list(reversed(DEVICES)), None)
        self.assertEqual(view._current_settings().microphone.endpoint_id, "mic-headset")
        view.input_enabled.set(False)
        view._source_toggled()
        self.assertTrue(view.endpoint_boxes["microphone"].instate(["disabled"]))
        self.assertTrue(view.endpoint_boxes["system"].instate(["readonly"]))
        self.assertIn("Desativado", view.endpoint_hints["microphone"].cget("text"))
        view._devices_loaded([device for device in DEVICES if device["id"] != "mic-headset"], None)
        view.input_enabled.set(True)
        view._source_toggled()
        self.assertIn("desconectado", view.endpoint_hints["microphone"].cget("text"))
        self.assertEqual(view._current_settings().microphone.endpoint_id, "mic-headset")
        for combo in view.endpoint_boxes.values():
            self.assertLessEqual(combo.winfo_rootx() + combo.winfo_width(),
                                 view.window.winfo_rootx() + view.window.winfo_width())

    def test_long_names_fit_popup_at_larger_text_size(self):
        scaling = self.root.tk.call("tk", "scaling")
        self.root.tk.call("tk", "scaling", 2.0)
        self.addCleanup(self.root.tk.call, "tk", "scaling", scaling)
        view = self.make_view()
        devices = [dict(device) for device in DEVICES]
        devices[2]["name"] = "Fones de ouvido (Headset Wireless — modo USB)"
        view._devices_loaded(devices, None)
        self.root.update()
        combo = view.endpoint_boxes["system"]
        combo.tk.call("ttk::combobox::Post", str(combo))
        self.root.update()
        popup = str(combo.tk.call("ttk::combobox::PopdownWindow", str(combo)))
        listbox = popup + ".f.l"
        width = int(combo.tk.call("winfo", "width", listbox))
        padding = 2 * int(combo.tk.call(listbox, "cget", "-borderwidth"))
        font = tkfont.Font(root=combo, font=view.ui.font(10))
        self.assertLessEqual(max(font.measure(value) for value in combo.cget("values")), width - padding)
        left = int(combo.tk.call("winfo", "rootx", popup))
        right = left + int(combo.tk.call("winfo", "width", popup))
        self.assertGreaterEqual(left, view.window.winfo_rootx())
        self.assertLessEqual(right, view.window.winfo_rootx() + view.window.winfo_width())
        combo.tk.call("ttk::combobox::Unpost", str(combo))


if __name__ == "__main__":
    unittest.main()
