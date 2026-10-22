"""GUI construction smoke test.

Builds every manager tab and the notification window on the app's shared Tk
root, driven through the GUI thread exactly the way the app drives it. Catches
widget-wiring bugs that the headless unit tests miss. Skipped automatically
where Tk cannot open a display (e.g. headless CI), and on macOS, where the
app's worker-thread Tk root is not something AppKit permits at all.
"""

import os
import sys
import gc
import tempfile
import threading
import time
import traceback
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app_module import snipvoice as tx  # .pyw is not importable off Windows
from gui_thread import GuiThread
from platform_support import IS_MAC
import ui_theme

TK_AVAILABLE = False
TK_SKIP_REASON = "Tk display not available"

if IS_MAC:
    # The app owns its Tk root on a dedicated worker thread (see gui_thread).
    # macOS AppKit refuses to build an NSWindow off the main thread and aborts
    # the process ("NSWindow should only be instantiated on the main thread!")
    # instead of raising, so this has to be decided *before* the probe below:
    # the probe would take the whole suite down with it rather than fail over.
    # The app resolves this by moving the root to the main thread
    # there (``GuiThread`` main-thread mode); these smoke tests still drive the
    # worker-thread mode, which macOS does not permit, so they stay skipped.
    TK_SKIP_REASON = "macOS requires AppKit on the main thread; these tests drive the worker-thread root"
else:
    try:
        import tkinter as tk
        from tkinter import ttk
        _probe = GuiThread(main_thread=False)
        _probe.ensure_started()
        _probe.stop()
        TK_AVAILABLE = True
    except Exception:
        pass




def _make_app(base_dir):
    with mock.patch.dict(os.environ, {"SNIPVOICE_HOME": base_dir}), \
            mock.patch.object(tx, "configure_logging"), \
            mock.patch.object(tx, "Controller"):
        return tx.Snipvoice()


def _descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from _descendants(child)


def _ancestors(widget):
    parent = widget.master
    while parent is not None:
        yield parent
        parent = parent.master


class DictationPresentationTests(unittest.TestCase):
    def test_download_bytes_belong_only_to_the_explicit_or_current_runtime_profile(self):
        snapshot = {"state": "loading", "download_active": True,
                    "download_profile": "compact", "download_done": 25, "download_total": 200}
        self.assertTrue(tx.voice_profile_downloading(snapshot, "compact", "accuracy"))
        self.assertFalse(tx.voice_profile_downloading(snapshot, "accuracy", "accuracy"))
        snapshot.update(download_active=False, download_profile=None, download_done=0, download_total=0)
        self.assertFalse(tx.voice_profile_downloading(snapshot, "accuracy", "accuracy"))
        snapshot.update(download_done=50, download_total=100)
        self.assertTrue(tx.voice_profile_downloading(snapshot, "accuracy", "accuracy"))
        self.assertFalse(tx.voice_profile_downloading(snapshot, "compact", "accuracy"))
        snapshot["state"] = "idle"
        self.assertFalse(tx.voice_profile_downloading(snapshot, "accuracy", "accuracy"))

    def test_header_uses_controller_health_instead_of_enabled_setting(self):
        voice = mock.Mock()
        voice.settings.profile, voice.settings.language = "balanced", "pt-BR"
        voice.settings.hotkey, voice.settings.activation_mode = "ctrl+alt+space", "hold"
        voice.is_enabled.return_value = True
        for state, flags, expected, tone in (
            ("loading", {}, "Carregando ditado", "info"),
            ("loading", {"load_failed": True}, "Carregando ditado", "info"),
            ("unavailable", {}, "Ditado indisponível", "warning"),
            ("unavailable", {"load_failed": True}, "Falha ao carregar", "danger"),
            ("idle", {}, "Ditado pronto", "success"),
            ("idle", {"load_failed": True}, "Ditado pronto", "success"),
            ("idle", {"meeting_busy": True}, "Aguardando a gravação", "warning"),
        ):
            voice.status_snapshot.return_value = dict(state=state, **flags)
            label, actual_tone = tx.voice_status_presentation(voice)
            self.assertIn(expected, label)
            self.assertEqual(actual_tone, tone)
        voice.is_enabled.return_value = False
        voice.status_snapshot.return_value = {"state": "unavailable"}
        self.assertIn("Ditado desligado", tx.voice_status_presentation(voice)[0])

    def test_history_states_are_translated_and_have_written_tones(self):
        import i18n
        self.addCleanup(i18n.set_language, "pt-BR")
        i18n.set_language("en-US")
        self.assertEqual(tx.voice_history_status("failed"), ("Failed", "danger"))
        self.assertEqual(tx.voice_history_status("interrupted"), ("Interrupted", "warning"))
        self.assertEqual(tx.voice_history_status("completed"), ("Completed", "text"))
        self.assertEqual(tx.voice_history_status("other"), ("Unknown", "text_muted"))

    def test_attention_caption_gives_neutral_actions_in_both_languages(self):
        import i18n
        self.addCleanup(i18n.set_language, "pt-BR")
        for language, single, plural, action in (
            ("pt-BR", "1 ditado precisa", "2 ditados precisam", "Selecione um item"),
            ("en-US", "1 dictation needs", "2 dictations need", "Select an item"),
        ):
            i18n.set_language(language)
            self.assertIn(single, tx.voice_history_attention(1))
            self.assertIn(plural, tx.voice_history_attention(2))
            for count in (1, 2):
                text = tx.voice_history_attention(count)
                self.assertIn(action, text)
                self.assertNotIn("Ative", text)
                self.assertNotIn("Enable", text)

    def test_download_ticks_do_not_rebuild_the_tray(self):
        app = tx.Snipvoice.__new__(tx.Snipvoice)
        app.voice, app.gui, app.logger = mock.Mock(), mock.Mock(), mock.Mock()
        app.refresh_tray_menu = mock.Mock()
        state = {"state": "unavailable", "download_active": True, "enabled": False}
        app.voice.status_snapshot.return_value = state
        app._voice_status_changed()
        state["download_done"] = 50
        app._voice_status_changed()
        app.refresh_tray_menu.assert_called_once_with()
        state["download_active"] = False
        app._voice_status_changed()
        self.assertEqual(app.refresh_tray_menu.call_count, 2)

    def test_recording_observer_request_cannot_cross_a_shortcut_edit_epoch(self):
        app = tx.Snipvoice.__new__(tx.Snipvoice)
        app.voice, app.gui = mock.Mock(), mock.Mock()
        app._quitting = threading.Event()
        app._meeting_startup_ready = True
        app._manager_meeting_view = mock.Mock(closed=False)
        app.meetings = mock.Mock()
        app.voice.hotkey_edit_state.return_value = (0, False)
        self.assertTrue(app._meeting_hotkey_request())
        queued = app.gui.submit.call_args.args[0]
        app.voice.hotkey_edit_state.return_value = (2, False)
        queued("root")
        app.meetings.snapshot.assert_not_called()
        app.voice.hotkey_edit_state.return_value = (3, True)
        self.assertFalse(app._meeting_hotkey_request())
        app._manager_meeting_view.request_start.assert_not_called()

    def test_enable_keeps_persistence_waits_off_the_gui_caller(self):
        app = tx.Snipvoice.__new__(tx.Snipvoice)
        app.voice, app.gui, app.task_runner = mock.Mock(), mock.Mock(), mock.Mock()
        app.voice.settings.profile = "balanced"
        app.voice.model_installed.return_value = True
        app._quitting = threading.Event()
        app.refresh_tray_menu, app._refresh_manager_voice_tab = mock.Mock(), mock.Mock()
        entered, release = threading.Event(), threading.Event()
        observed = []

        def blocked_enable():
            observed.append(threading.get_ident())
            entered.set()
            if not release.wait(2):
                raise AssertionError("synthetic persistence wait timed out")
        app.voice.enable.side_effect = blocked_enable
        with mock.patch.object(tx.macos_permissions, "check_microphone", return_value="granted"):
            app._confirm_and_enable_voice()
            app.voice.enable.assert_not_called()
            work = app.task_runner.start.call_args.args[0]
            worker = threading.Thread(target=work)
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                app._confirm_and_enable_voice()
                app.task_runner.start.assert_called_once()
            finally:
                release.set()
                worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertNotEqual(observed, [threading.get_ident()])
        app.gui.submit.call_args.args[0]("root")
        self.assertFalse(app._voice_enable_pending)

    def test_recording_monitor_uses_the_shared_passthrough_state(self):
        app = tx.Snipvoice.__new__(tx.Snipvoice)
        app._settings_lock, app._quitting = threading.RLock(), threading.Event()
        app._meeting_monitor, app._meeting_startup_ready = None, True
        app.settings = {"meeting_hotkey": "ctrl+alt+f9"}
        app.voice = mock.Mock()
        app.notify_error = mock.Mock()
        with mock.patch("voice_hotkey.VoiceHotkeyMonitor") as monitor:
            app._rebuild_meeting_monitor()
        self.assertIs(monitor.call_args.kwargs["passthrough_state"], app.voice.hotkey_edit_state)
        app.notify_error.assert_not_called()


class ManagerLifecycleTests(unittest.TestCase):
    def test_close_hides_and_notifies_once_without_closing_the_view(self):
        app = tx.Snipvoice.__new__(tx.Snipvoice)
        app.manager_window, app._manager_meeting_view, app.icon = mock.Mock(), mock.Mock(), mock.Mock()
        app._destroy_manager_window = mock.Mock()
        app._close_settings_window()
        app._close_settings_window()
        self.assertEqual(app.manager_window.withdraw.call_count, 2)
        self.assertEqual(app._manager_meeting_view.set_visible.call_args_list,
                         [mock.call(False), mock.call(False)])
        app.icon.notify.assert_called_once()
        app._manager_meeting_view.close.assert_not_called()
        app._manager_meeting_view.close_without_prompt.assert_not_called()
        app._destroy_manager_window.assert_not_called()

    def test_tray_open_keeps_idle_destination_but_selects_recording_while_capturing(self):
        app = tx.Snipvoice.__new__(tx.Snipvoice)
        app._show_manager_window, app._manager_notebook = mock.Mock(), mock.Mock()
        app._manager_recording_tab, app.meetings = "recording", mock.Mock()
        app.meetings.snapshot.return_value = {"state": "idle"}
        app._show_meetings("root")
        app._manager_notebook.select.assert_not_called()
        app.meetings.snapshot.return_value = {"state": "recording"}
        app._show_meetings("root")
        app._manager_notebook.select.assert_called_once_with("recording")

    def test_rebuild_captures_python_context_before_closing_and_destroys_on_callback(self):
        app = tx.Snipvoice.__new__(tx.Snipvoice)
        app._manager_meeting_view, app._destroy_manager_window = mock.Mock(), mock.Mock()
        app._manager_meeting_view.closed = False
        root = mock.Mock()
        app._reopen_manager(root)
        self.assertIs(app._manager_view_state, app._manager_meeting_view.capture_view_state.return_value)
        app._destroy_manager_window.assert_not_called()
        app._manager_meeting_view.close.call_args.kwargs["after_close"]()
        app._destroy_manager_window.assert_called_once_with()
        root.after_idle.assert_called_once()

    def test_overlay_open_queues_through_gui_instead_of_touching_tk_in_cocoa_callback(self):
        app = tx.Snipvoice.__new__(tx.Snipvoice)
        app.gui, app._open_indicator_target = mock.Mock(), mock.Mock()
        app._queue_indicator_target("synthetic")
        app._open_indicator_target.assert_not_called()
        app.gui.submit.call_args.args[0]("root")
        app._open_indicator_target.assert_called_once_with("synthetic")


@unittest.skipUnless(TK_AVAILABLE, TK_SKIP_REASON)
class ManagerGuiSmokeTests(unittest.TestCase):
    def setUp(self):
        directory = os.path.join(os.path.dirname(__file__), "tmp")
        os.makedirs(directory, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=directory)
        self.addCleanup(temporary.cleanup)
        self.app = _make_app(temporary.name)
        self.app.gui.ensure_started()

    def tearDown(self):
        def cleanup(root):
            self.app._close_settings_window(force=True)
            for child in list(root.winfo_children()):
                try:
                    child.destroy()
                except Exception:
                    pass
            gc.collect()

        try:
            if self.app.gui.running:
                self.app.gui.call(cleanup, timeout=10)
        except Exception:
            self.app._manager_voice_refresher = None
        self.app.gui.stop()

    def _on_gui(self, func):
        """Run func(root) on the GUI thread, propagating assertion failures."""
        try:
            return self.app.gui.call(func, timeout=30)
        except TimeoutError:
            # Name where the GUI thread is blocked; the deaf-timer stall this
            # caught is guarded by GuiThread's Windows waker.
            thread = self.app.gui._thread
            frame = sys._current_frames().get(getattr(thread, "ident", None))
            stack = "".join(traceback.format_stack(frame)) if frame else "GUI thread is not running"
            raise TimeoutError(
                "GUI call did not complete in time; GUI thread stack:\n" + stack
            ) from None



    def _static_rows(self, frame):
        """{trigger: (preview, markers)} from the static tab's Treeview."""
        trees = [w for w in _descendants(frame) if isinstance(w, ttk.Treeview)]
        self.assertTrue(trees, "expected a snippet Treeview")
        tree = trees[0]
        return {iid: tuple(tree.item(iid, "values"))[1:] for iid in tree.get_children()}



    def _save_static_from_editor(self, trigger, value):
        """Type trigger/value into the static editor and click Salvar."""

        def build(shared_root):
            root = tk.Toplevel(shared_root)
            root.withdraw()
            frame = tk.Frame(root)
            self.app._create_static_snippets_tab(frame, root)
            root.update_idletasks()

            entries = [w for w in _descendants(frame) if isinstance(w, tk.Entry)]
            text = [w for w in _descendants(frame) if isinstance(w, tk.Text)][0]
            entries[1].insert(0, trigger)
            text.insert("1.0", value)
            button = [w for w in _descendants(frame)
                      if isinstance(w, tk.Button) and w.cget("text") == "Salvar"][0]
            button.invoke()

        self._on_gui(build)






    def _build_mappings_tab(self, shared_root, set_count=None):
        root = tk.Toplevel(shared_root)
        root.withdraw()
        frame = tk.Frame(root)
        self.app._create_dynamic_mappings_tab(frame, root, set_count=set_count)
        root.update_idletasks()
        return frame

    def _tree_rows(self, frame):
        """{key: (preview, markers)} from the only Treeview in a tab."""
        trees = [w for w in _descendants(frame) if isinstance(w, ttk.Treeview)]
        self.assertTrue(trees, "expected a snippet Treeview")
        tree = trees[0]
        return {iid: tuple(tree.item(iid, "values"))[1:] for iid in tree.get_children()}













    def test_voice_tab_embeds_settings_and_delegates_enable(self):
        voice = mock.Mock()
        voice.is_enabled.return_value = True
        voice.status_label.return_value = "Entrada por voz (pronta)"
        voice.settings.profile = "balanced"
        voice.settings.language = "auto"
        voice.settings.hotkey = "ctrl+alt+space"
        voice.settings.command_hotkey = "ctrl+alt+shift+space"
        voice.cache_dir = tempfile.mkdtemp()
        voice.model_download_in_progress.return_value = False
        self.app.voice = voice
        self.app.toggle_voice = mock.Mock()

        def build(shared_root):
            root = tk.Toplevel(shared_root)
            root.withdraw()
            frame = tk.Frame(root)
            self.app._create_voice_tab(frame, root)
            root.update_idletasks()
            checkbox = [
                widget for widget in _descendants(frame)
                if isinstance(widget, tk.Checkbutton)
            ][0]
            buttons = [
                str(widget.cget("text")) for widget in _descendants(frame)
                if isinstance(widget, tk.Button)
            ]
            menus = [
                widget for widget in _descendants(frame)
                if isinstance(widget, tk.Menu)
            ]
            selectors = [
                widget for widget in _descendants(frame)
                if isinstance(widget, ttk.Combobox)
            ]
            labels = [
                str(widget.cget("text")) for widget in _descendants(frame)
                if isinstance(widget, tk.Label)
            ]
            checked = bool(int(checkbox.getvar(checkbox.cget("variable"))))
            checkbox.invoke()
            menu_items = [
                menus[0].entrycget(index, "label")
                for index in range(menus[0].index("end") + 1)
            ]
            return checked, labels, buttons, menu_items, len(selectors)

        checked, labels, buttons, menu_items, selector_count = self._on_gui(build)
        self.assertTrue(checked)
        self.assertTrue(any("Ditado" in text for text in labels), labels)
        self.assertIn("Salvar", buttons)
        self.assertIn("Histórico de voz…", buttons)
        self.assertEqual(menu_items, [
            "Correções…", "Recarregar comandos",
            "Licenças e atribuições…", "Remover modelo",
        ])
        self.assertFalse(any("transcribe.cpp — MIT" in text for text in labels))
        self.assertFalse(any(text.startswith("Configurar ditado") for text in buttons))
        self.assertGreaterEqual(selector_count, 2)
        self.app.toggle_voice.assert_called_once_with()

    def test_voice_licenses_open_in_a_bounded_scrollable_window(self):
        _ensure_voice(self.app)

        def open_notices(shared_root):
            root = tk.Toplevel(shared_root)
            root.withdraw()
            frame = tk.Frame(root)
            self.app._create_voice_tab(frame, root)
            menu = next(
                widget for widget in _descendants(frame)
                if isinstance(widget, tk.Menu)
            )
            menu.invoke(2)
            dialog = next(
                child for child in root.winfo_children()
                if isinstance(child, tk.Toplevel)
                and child.title() == "Licenças e atribuições"
            )
            dialog.update_idletasks()
            text_widget = next(
                widget for widget in _descendants(dialog)
                if isinstance(widget, tk.Text)
            )
            scrollbars = [
                widget for widget in _descendants(dialog)
                if isinstance(widget, ttk.Scrollbar)
            ]
            return (
                dialog.winfo_width(),
                dialog.winfo_height(),
                str(text_widget.cget("state")),
                text_widget.get("1.0", tk.END),
                len(scrollbars),
            )

        width, height, state, notices, scrollbar_count = self._on_gui(open_notices)
        self.assertGreaterEqual(width, 520)
        self.assertGreaterEqual(height, 300)
        self.assertEqual(state, tk.DISABLED)
        self.assertIn("transcribe.cpp — MIT", notices)
        self.assertIn("Qwen/Qwen3-ASR-0.6B", notices)
        self.assertEqual(scrollbar_count, 1)

    def test_voice_secondary_tools_are_available_in_menu(self):
        _ensure_voice(self.app)

        def inspect(shared_root):
            root = tk.Toplevel(shared_root)
            frame = tk.Frame(root)
            frame.pack(fill=tk.BOTH, expand=True)
            self.app._create_voice_tab(frame, root)
            buttons = [
                widget
                for widget in _descendants(frame)
                if isinstance(widget, tk.Button)
            ]
            more = next(widget for widget in _descendants(frame)
                        if isinstance(widget, tk.Menubutton))
            menu = next(widget for widget in _descendants(frame)
                        if isinstance(widget, tk.Menu))
            labels = [menu.entrycget(index, "label")
                      for index in range(menu.index("end") + 1)]
            primary_visible = all(
                button.winfo_manager()
                for button in buttons
                if button.cget("text") in ("Salvar", "Histórico de voz…")
            )
            has_secondary_buttons = any(
                button.cget("text") in labels for button in buttons
            )
            title = more.cget("text")
            root.destroy()
            return title, labels, primary_visible, has_secondary_buttons

        title, labels, primary_visible, has_secondary_buttons = self._on_gui(inspect)
        self.assertEqual(title, "Mais opções ▾")
        self.assertEqual(len(labels), 4)
        self.assertTrue(primary_visible)
        self.assertFalse(has_secondary_buttons)

    def test_voice_actions_fit_inside_the_default_manager_height(self):
        _ensure_voice(self.app)

        def measure(shared_root):
            root = tk.Toplevel(shared_root)
            root.geometry("960x660")
            try:
                root.attributes("-alpha", 0.0)
            except tk.TclError:
                pass
            frame = tk.Frame(root)
            frame.pack(fill=tk.BOTH, expand=True)
            self.app._create_voice_tab(frame, root)
            root.deiconify()
            root.update()
            actions = {
                str(widget.cget("text")): widget
                for widget in _descendants(frame)
                if isinstance(widget, (tk.Button, tk.Menubutton))
            }
            expected = {
                "Salvar",
                "Histórico de voz…",
                "Mais opções ▾",
            }
            self.assertTrue(expected.issubset(actions), actions)
            result = frame.winfo_height(), {
                name: widget.winfo_rooty() - frame.winfo_rooty()
                + widget.winfo_height()
                for name, widget in actions.items()
                if name in expected
            }, {name: widget.winfo_reqheight() for name, widget in actions.items()
                if name in expected}
            root.destroy()
            return result

        available_height, action_bottoms, action_heights = self._on_gui(measure)
        self.assertGreater(available_height, 1)
        for name, bottom in action_bottoms.items():
            self.assertLessEqual(
                bottom,
                available_height,
                f"{name!r} is clipped below the voice tab",
            )
        if not IS_MAC:
            # Compact density: at least 32px, and one height for the whole row.
            for name, height in action_heights.items():
                self.assertGreaterEqual(height, 32, f"{name!r} is too thin")
            self.assertEqual(len(set(action_heights.values())), 1, action_heights)

    def test_voice_tab_refresh_updates_install_labels_and_normalized_settings(self):
        voice = mock.Mock()
        voice.is_enabled.return_value = False
        voice.status_label.return_value = "Entrada por voz"
        voice.settings.profile = "balanced"
        voice.settings.language = "pt-BR"
        voice.settings.hotkey = "ctrl+alt+space"
        voice.settings.command_hotkey = "ctrl+alt+shift+space"
        voice.cache_dir = tempfile.mkdtemp()
        voice.model_download_in_progress.return_value = False
        self.app.voice = voice

        def build_and_refresh(shared_root):
            root = tk.Toplevel(shared_root)
            root.withdraw()
            frame = tk.Frame(root)
            self.app._create_voice_tab(frame, root)
            root.update_idletasks()
            combos = [
                widget for widget in _descendants(frame)
                if isinstance(widget, ttk.Combobox)
            ]
            self.assertGreaterEqual(len(combos), 2)
            selected, language, _hotkey, _command, _activation = self.app._manager_voice_tk_vars
            before_state = (selected.get(), language.get())
            voice.settings.profile = "accuracy"
            voice.settings.language = "auto"
            self.app._manager_voice_refresher()
            after_display = tuple(combo.get() for combo in combos)
            after_state = (selected.get(), language.get())
            return before_state, after_display, after_state

        before_state, after_display, after_state = self._on_gui(build_and_refresh)
        self.assertEqual(before_state, ("balanced", "pt-BR"))
        self.assertEqual(after_state, ("accuracy", "auto"))
        self.assertIn("Muito preciso · Qwen 1.7B", after_display)
        self.assertTrue(any("Automático" in text for text in after_display), after_display)

    def test_voice_status_ticks_preserve_dirty_fields_and_discard_reloads_settings(self):
        voice = _ensure_voice(self.app)
        queued = []

        def build(root):
            owner = tk.Toplevel(root)
            frame = tk.Frame(owner)
            frame.pack(fill=tk.BOTH, expand=True)
            self.app._create_voice_tab(frame, owner)
            selected, language, hotkey, command, mode = self.app._manager_voice_tk_vars
            selected.set("accuracy")
            language.set("auto")
            hotkey.set("ctrl+shift+f8")
            command.set("ctrl+shift+f9")
            mode.set("toggle")
            edited = tuple(var.get() for var in self.app._manager_voice_tk_vars)
            for _ in range(10):
                self.app._manager_voice_refresher()
            self.assertEqual(tuple(var.get() for var in self.app._manager_voice_tk_vars), edited)
            buttons = {str(widget.cget("text")): widget for widget in _descendants(frame)
                       if isinstance(widget, tk.Button)}
            self.assertEqual(str(buttons["Salvar"].cget("state")), tk.NORMAL)
            buttons["Descartar"].invoke()
            self.assertEqual(selected.get(), voice.settings.profile)
            self.assertEqual(hotkey.get(), voice.settings.hotkey)
            self.assertEqual(str(buttons["Salvar"].cget("state")), tk.DISABLED)
        with mock.patch.object(self.app.task_runner, "start", side_effect=lambda fn, **kw: queued.append(fn)):
            self._on_gui(build)

    def test_shortcut_edit_status_callback_updates_the_real_header_without_manual_refresh(self):
        voice = self.app.voice
        self.app.voice_status_indicator = mock.Mock()
        with voice._lock:
            voice.settings.enabled = True
            voice._state = "idle"
        built = {}

        def build(root):
            owner = tk.Toplevel(root)
            owner.withdraw()
            frame = tk.Frame(owner)
            frame.pack(fill=tk.BOTH, expand=True)
            self.app._create_voice_tab(frame, owner)
            built["header"] = next(widget for widget in _descendants(frame)
                                   if isinstance(widget, tk.Label)
                                   and str(widget.cget("text")).startswith("Ditado pronto"))
        self._on_gui(build)
        self.assertIn("Ditado pronto", self._on_gui(lambda _root: built["header"].cget("text")))
        token = threading.Event()
        token.set()
        self.assertTrue(self._on_gui(lambda _root: voice.begin_hotkey_edit(token)))
        paused = self._on_gui(lambda _root: (str(built["header"].cget("text")),
                                           str(built["header"].cget("fg"))))
        self.assertIn("Atalhos pausados para configurar", paused[0])
        self.assertEqual(paused[1], ui_theme.theme().info)
        self._on_gui(lambda _root: voice.end_hotkey_edit(token))
        ready = self._on_gui(lambda _root: (str(built["header"].cget("text")),
                                          str(built["header"].cget("fg"))))
        self.assertIn("Ditado pronto", ready[0])
        self.assertEqual(ready[1], ui_theme.theme().success)

    def test_model_download_percentages_fit_in_both_languages_and_window_sizes(self):
        import i18n
        self.addCleanup(i18n.set_language, "pt-BR")
        voice = mock.Mock()
        voice.settings.profile, voice.settings.language = "balanced", "auto"
        voice.settings.hotkey, voice.settings.command_hotkey = "ctrl+alt+space", "ctrl+alt+shift+space"
        voice.settings.activation_mode = "hold"
        voice.is_enabled.return_value = False
        voice.history_rows.return_value = []
        voice.model_download_in_progress.side_effect = lambda profile=None: profile == "balanced"
        voice.status_snapshot.return_value = {
            "state": "unavailable", "download_active": True, "download_profile": "balanced",
            "download_done": 100, "download_total": 100,
        }
        voice.cache_dir = self.app.voice.cache_dir
        self.app.voice = voice

        built = {}

        def build(root, language, width):
            i18n.set_language(language)
            owner = tk.Toplevel(root)
            owner.geometry(f"{width}x700")
            built["owner"] = owner
            frame, models = tk.Frame(owner), tk.Frame(owner)
            models.pack(fill=tk.X)
            self.app._build_voice_settings_controls(frame, owner, models_parent=models)
            button = next(widget for widget in _descendants(models)
                          if isinstance(widget, tk.Button)
                          and str(widget.cget("text")) == i18n.tr("Baixando {percent}%", percent=100))
            built.update(models=models, button=button)

        def measure(_root):
            owner, models, button = (built[name] for name in ("owner", "models", "button"))
            # Map notifications are normal events; idle layout alone leaves
            # the new child hierarchy unmapped on Windows.
            owner.update()
            self.assertTrue(owner.winfo_viewable())
            self.assertTrue(button.winfo_viewable())
            # Copying a pixel font through Font rounds it to a point size.
            # Measure the exact configured font that Tk uses for this button.
            text_width = int(button.tk.call(
                "font", "measure", button.cget("font"), button.cget("text")))
            inset = 2 * sum(int(button.cget(option)) for option in ("padx", "bd", "highlightthickness"))
            return (text_width, button.winfo_width() - inset,
                    button.winfo_rootx() + button.winfo_width(),
                    models.winfo_rootx() + models.winfo_width())

        def close(_root):
            owner = built.pop("owner", None)
            if owner is not None and owner.winfo_exists():
                owner.destroy()

        with mock.patch("voice_models.model_is_installed", return_value=False):
            for language in ("pt-BR", "en-US"):
                for width in (1040, 1440):
                    with self.subTest(language=language, width=width):
                        try:
                            self._on_gui(lambda root: build(root, language, width))
                            text_width, available, right, parent_right = self._on_gui(measure)
                            self.assertGreaterEqual(available, text_width)
                            self.assertLessEqual(right, parent_right)
                        finally:
                            self._on_gui(close)

    def test_shortcut_capture_hint_distinguishes_pause_busy_and_invalid_input(self):
        voice = self.app.voice
        self.app.voice_status_indicator = mock.Mock()
        with voice._lock:
            voice.settings.enabled = True
            voice._state = "idle"

        built = {}

        def build(root):
            owner = tk.Toplevel(root)
            owner.geometry("1040x700")
            built["owner"] = owner
            frame = tk.Frame(owner)
            frame.pack(fill=tk.BOTH, expand=True)
            self.app._create_voice_tab(frame, owner)
            entry = next(widget for widget in _descendants(frame)
                         if isinstance(widget, tk.Entry) and widget.get() == "Ctrl + Alt + Space")
            hint = next(widget for widget in entry.master.winfo_children()
                        if isinstance(widget, tk.Label) and int(widget.grid_info().get("row", -1)) == 6)
            built.update(entry=entry, hint=hint)

        def inspect(_root):
            owner, entry, hint = (built[name] for name in ("owner", "entry", "hint"))
            owner.update()
            self.assertTrue(owner.winfo_viewable())
            self.assertTrue(entry.winfo_viewable())
            entry.event_generate("<FocusIn>")
            self.assertIn("Atalhos globais pausados", str(hint.cget("text")))
            self.assertEqual(str(hint.cget("fg")), ui_theme.theme().info)
            self.app._manager_voice_tk_vars[3].set(voice.settings.hotkey)
            entry.event_generate("<FocusOut>")
            self.assertIn("Use atalhos diferentes", str(hint.cget("text")))
            self.assertEqual(str(hint.cget("fg")), ui_theme.theme().danger)
            self.app._manager_voice_tk_vars[3].set(voice.settings.command_hotkey)
            entry.event_generate("<FocusOut>")
            self.assertEqual(str(hint.cget("text")), "")
            with voice._lock:
                voice._state = "transcribing"
            try:
                entry.event_generate("<FocusIn>")
                self.assertIn("Aguarde o ditado terminar", str(hint.cget("text")))
                self.assertEqual(str(hint.cget("fg")), ui_theme.theme().warning)
                entry.event_generate("<FocusOut>")
            finally:
                with voice._lock:
                    voice._state = "idle"
        try:
            self._on_gui(build)
            self._on_gui(inspect)
        finally:
            def close(_root):
                owner = built.get("owner")
                if owner is not None and owner.winfo_exists():
                    owner.destroy()
            self._on_gui(close)

    def test_recent_history_reads_off_tk_once_and_disables_nonretryable_selection(self):
        voice = mock.Mock()
        voice.status_snapshot.return_value = {"state": "idle", "retry_available": True, "enabled": True}
        voice.history_rows.return_value = [
            {"id": "completed", "created_at": "2026-01-01T12:00:00Z", "status": "completed",
             "transcript": "synthetic receipt", "retryable": False},
            {"id": "failed", "created_at": "2026-01-01T12:01:00Z", "status": "failed",
             "transcript": "", "error": "synthetic failure", "retryable": True},
        ]
        self.app.voice = voice
        queued, built = [], {}

        def build(root):
            owner = tk.Toplevel(root)
            frame = tk.Frame(owner)
            frame.pack(fill=tk.BOTH, expand=True)
            tree, refresh = self.app._build_voice_history_rows(frame, owner, limit=5, height=3)
            built.update(frame=frame, owner=owner, tree=tree, refresh=refresh)
            for n in range(10):
                voice.status_snapshot.return_value["download_done"] = n
                refresh()
            voice.history_rows.assert_not_called()

        with mock.patch.object(self.app.task_runner, "start", side_effect=lambda fn, **kw: queued.append(fn)):
            self._on_gui(build)
            self.assertEqual(len(queued), 1)
            queued.pop()()  # Test thread, never the Tk owner.

            def inspect(_root):
                tree = built["tree"]
                self.assertEqual(str(tree.cget("style")), "Manager.Treeview")
                self.assertEqual(tuple(tree.get_children()), ("completed", "failed"))
                self.assertNotIn("T12:", tree.item("completed", "values")[0])
                self.assertEqual(tree.item("failed", "values")[1], "Falhou")
                retry = next(widget for widget in _descendants(built["frame"])
                             if isinstance(widget, tk.Button) and str(widget.cget("text")) == "Tentar novamente")
                tree.selection_set("completed")
                tree.event_generate("<<TreeviewSelect>>")
                self.assertEqual(str(retry.cget("state")), tk.DISABLED)
                tree.selection_set("failed")
                tree.event_generate("<<TreeviewSelect>>")
                self.assertEqual(str(retry.cget("state")), tk.NORMAL)
                built["owner"].destroy()
                self.assertFalse(getattr(self.app, "_voice_history_refreshers", []))
            self._on_gui(inspect)

    def test_history_error_is_not_an_empty_row_and_destroy_fences_delivery(self):
        voice = mock.Mock()
        voice.status_snapshot.return_value = {"state": "idle", "retry_available": True}
        voice.history_rows.side_effect = OSError("synthetic read failure")
        self.app.voice = voice
        queued, built = [], {}

        def build(root):
            owner = tk.Toplevel(root)
            frame = tk.Frame(owner)
            frame.pack(fill=tk.BOTH, expand=True)
            tree, refresh = self.app._build_voice_history_rows(frame, owner, limit=200, height=4)
            built.update(owner=owner, frame=frame, tree=tree, refresh=refresh)

        with mock.patch.object(self.app.task_runner, "start", side_effect=lambda fn, **kw: queued.append(fn)):
            self._on_gui(build)
            queued.pop()()

            def inspect(_root):
                self.assertFalse(built["tree"].get_children())
                labels = [str(widget.cget("text")) for widget in _descendants(built["frame"])
                          if isinstance(widget, tk.Label)]
                self.assertTrue(any("Não foi possível ler" in text for text in labels))
                self.assertNotIn("Nenhuma gravação.", labels)
                built["refresh"](force=True)
                built["owner"].destroy()
            self._on_gui(inspect)
            queued.pop()()
            self._on_gui(lambda _root: self.assertFalse(getattr(self.app, "_voice_history_refreshers", [])))

    def test_rejected_dictation_save_keeps_the_form_dirty(self):
        voice = _ensure_voice(self.app)
        queued, built = [], {}

        def build(root):
            owner = tk.Toplevel(root)
            frame = tk.Frame(owner)
            frame.pack(fill=tk.BOTH, expand=True)
            self.app._create_voice_tab(frame, owner)
            self.app._manager_voice_tk_vars[2].set("ctrl+shift+f8")
            save = next(widget for widget in _descendants(frame) if isinstance(widget, tk.Button)
                        and str(widget.cget("text")) == "Salvar")
            built.update(frame=frame, save=save)
            save.invoke()

        with mock.patch.object(self.app.task_runner, "start", side_effect=lambda fn, **kw: queued.append((kw.get("name"), fn))), \
                mock.patch.object(voice, "apply_options", return_value=False), \
                mock.patch("voice_models.model_is_installed", return_value=True):
            self._on_gui(build)
            next(fn for name, fn in queued if name == "voice-settings")()

            def inspect(_root):
                self.assertEqual(str(built["save"].cget("state")), tk.NORMAL)
                self.assertEqual(self.app._manager_voice_tk_vars[2].get(), "ctrl+shift+f8")
                labels = [str(widget.cget("text")) for widget in _descendants(built["frame"])
                          if isinstance(widget, tk.Label)]
                self.assertIn("Não foi possível salvar. Tente novamente.", labels)
            self._on_gui(inspect)

    def test_voice_tab_download_button_downloads_its_model_without_enabling(self):
        voice = mock.Mock()
        voice.is_enabled.return_value = False
        voice.status_label.return_value = "Entrada por voz"
        voice.settings.profile = "balanced"
        voice.settings.language = "auto"
        voice.settings.hotkey = "ctrl+alt+space"
        voice.settings.command_hotkey = "ctrl+alt+shift+space"
        voice.cache_dir = tempfile.mkdtemp()
        voice.model_download_in_progress.return_value = False
        voice.download_profile.return_value = True
        self.app.voice = voice

        def build_and_download(shared_root):
            with mock.patch("voice_models.model_is_installed", return_value=False), \
                    mock.patch.object(tx.messagebox, "askokcancel", return_value=True):
                self.app._show_manager_window(shared_root)
                settings_tab = next(
                    notebook_tab for notebook_tab in self.app._manager_notebook.tabs()
                    if self.app._manager_notebook.tab(notebook_tab, "text") == "Configurações"
                )
                settings = self.app._manager_notebook.nametowidget(settings_tab)
                rows = [
                    widget for widget in _descendants(settings)
                    if isinstance(widget, tk.Button)
                    and str(widget.cget("text")) == "Baixar"
                ]
                self.assertTrue(rows)
                rows[1].invoke()

        self._on_gui(build_and_download)
        voice.download_profile.assert_called_once_with("balanced")
        voice.enable.assert_not_called()

    def test_voice_replacements_editor_uses_shared_root_and_persists(self):
        voice = _ensure_voice(self.app)
        voice.settings.voice_replacements = {}

        def open_and_save(shared_root):
            with mock.patch.object(tx.tk, "Tk", wraps=tx.tk.Tk) as tk_constructor, \
                    mock.patch.object(tx.simpledialog, "askstring", side_effect=["Quen", "Qwen"]):
                dialog = self.app._show_voice_replacements(shared_root)
                self.assertEqual(dialog.master, shared_root)
                self.assertEqual(dialog.title(), "Correções da transcrição")
                buttons = {
                    str(widget.cget("text")): widget
                    for widget in _descendants(dialog)
                    if isinstance(widget, tk.Button)
                }
                buttons["Adicionar"].invoke()
                buttons["Salvar"].invoke()
                return tk_constructor.call_count

        with mock.patch.object(
            self.app, "_persist_voice_settings", return_value=True
        ) as persist:
            constructor_calls = self._on_gui(open_and_save)
        persist.assert_called_once_with({"voice_replacements": {"Quen": "Qwen"}})
        self.assertEqual(constructor_calls, 0)
        self.assertEqual(voice.settings.voice_replacements, {"Quen": "Qwen"})

    def _command_editor_widgets(self, shared_root):
        dialog = self.app._show_voice_commands(shared_root)
        widgets = list(_descendants(dialog))
        return (
            dialog,
            next(widget for widget in widgets if isinstance(widget, tk.Entry)),
            next(widget for widget in widgets if isinstance(widget, tk.Text)),
            next(widget for widget in widgets if isinstance(widget, ttk.Treeview)),
            {str(widget.cget("text")): widget for widget in widgets
             if isinstance(widget, tk.Button)},
        )

    def test_voice_commands_are_accessible_inside_dictation_on_the_shared_root(self):
        _ensure_voice(self.app)

        def open_editor(shared_root):
            frame = tk.Frame(shared_root)
            self.app._create_voice_tab(frame, shared_root)
            button = next(widget for widget in _descendants(frame)
                          if isinstance(widget, tk.Button)
                          and widget.cget("text") == "Configurar comandos…")
            with mock.patch.object(tx.tk, "Tk", wraps=tx.tk.Tk) as constructor:
                button.invoke()
                dialog = next(child for child in shared_root.winfo_children()
                              if isinstance(child, tk.Toplevel)
                              and child.title() == "Comandos por voz")
                self.assertEqual(dialog.master, shared_root)
                self.assertEqual(constructor.call_count, 0)
                dialog.destroy()

        self._on_gui(open_editor)

    def test_mode_selection_is_passed_to_the_controller_off_the_gui_thread(self):
        voice = mock.Mock()
        voice.is_enabled.return_value = True
        voice.settings.profile = "balanced"
        voice.settings.language = "auto"
        voice.settings.hotkey = "ctrl+alt+space"
        voice.settings.command_hotkey = "ctrl+alt+shift+space"
        voice.settings.activation_mode = "hold"
        voice.cache_dir = self.app.data_dir
        voice.model_download_in_progress.return_value = False
        self.app.voice = voice
        applied = threading.Event()
        threads = []

        def apply(**_options):
            threads.append(threading.current_thread())
            applied.set()

        voice.apply_options.side_effect = apply

        def choose_and_save(shared_root):
            frame = tk.Frame(shared_root)
            self.app._create_voice_tab(frame, shared_root)
            widgets = list(_descendants(frame))
            choice = next(widget for widget in widgets if isinstance(widget, tk.Radiobutton)
                          and widget.cget("value") == "toggle")
            choice.invoke()
            next(widget for widget in widgets if isinstance(widget, tk.Button)
                 and widget.cget("text") == "Salvar").invoke()
            return threading.current_thread()

        with mock.patch("voice_models.model_is_installed", return_value=True), \
                mock.patch.object(self.app, "refresh_tray_menu"):
            gui_thread = self._on_gui(choose_and_save)
            self.assertTrue(applied.wait(2))
        self.assertNotEqual(threads, [gui_thread])
        self.assertEqual(voice.apply_options.call_args.kwargs["activation_mode"], "toggle")

    def test_command_editor_adds_edits_and_saves_the_library_on_a_worker(self):
        from voice_commands import load_commands
        from voice_dispatch import match_voice_command

        saved = threading.Event()
        threads = []
        original_save = self.app._save_commands

        def persist(commands, expected):
            threads.append(threading.current_thread())
            original_save(commands, expected)
            saved.set()

        def edit(shared_root):
            dialog, phrase, text, tree, buttons = self._command_editor_widgets(shared_root)
            phrase.insert(0, " Quick   Greeting ")
            text.insert("1.0", "First draft")
            buttons["Aplicar comando"].invoke()
            self.assertEqual(tree.get_children(), ("quick greeting",))
            phrase.delete(0, tk.END)
            phrase.insert(0, "final greeting")
            text.delete("1.0", tk.END)
            text.insert("1.0", "Hello!\nHow can I help?")
            buttons["Salvar comandos"].invoke()
            return dialog, threading.current_thread()

        with mock.patch.object(self.app, "_save_commands", side_effect=persist):
            dialog, gui_thread = self._on_gui(edit)
            self.assertTrue(saved.wait(2))
            self._on_gui(lambda _root: None)  # Drain the save completion on Tk.
        library = {"final greeting": "Hello!\nHow can I help?"}
        self.assertEqual(load_commands(os.path.join(self.app.data_dir, "commands.json")), library)
        self.assertEqual(self.app.snippets, library)
        self.assertEqual(match_voice_command("Final greeting", self.app.snippets,
                                           self.app.trigger_index), "final greeting")
        self.assertNotEqual(threads, [gui_thread])
        self.assertFalse(self._on_gui(lambda _root: dialog.winfo_exists()))

    def test_command_editor_removal_requires_confirmation_and_cancel_keeps_the_library(self):
        library = {"greeting": "Hello!"}
        self.app._save_commands(library, {})

        def remove_and_cancel(shared_root):
            dialog, _phrase, _text, tree, buttons = self._command_editor_widgets(shared_root)
            tree.selection_set("greeting")
            tree.event_generate("<<TreeviewSelect>>")
            with mock.patch.object(tx.messagebox, "askyesno", return_value=False):
                buttons["Remover"].invoke()
                self.assertEqual(tree.get_children(), ("greeting",))
            with mock.patch.object(tx.messagebox, "askyesno", return_value=True):
                buttons["Remover"].invoke()
                self.assertEqual(tree.get_children(), ())
            buttons["Cancelar"].invoke()
            self.assertFalse(dialog.winfo_exists())

        self._on_gui(remove_and_cancel)
        self.assertEqual(self.app.snippets, library)
        self.app._load_commands()
        self.assertEqual(self.app.snippets, library)

    def test_failed_command_save_preserves_library_and_editable_draft(self):
        library = {"greeting": "Hello!"}
        self.app._save_commands(library, {})
        error_shown = threading.Event()

        def save_draft(shared_root):
            dialog, phrase, text, _tree, buttons = self._command_editor_widgets(shared_root)
            phrase.insert(0, "new command")
            text.insert("1.0", "New text")
            buttons["Salvar comandos"].invoke()
            return dialog, text, buttons

        with mock.patch.object(self.app, "_save_commands", side_effect=OSError("synthetic failure")), \
                mock.patch.object(tx.messagebox, "showerror", side_effect=lambda *_args, **_kw: error_shown.set()):
            dialog, text, buttons = self._on_gui(save_draft)
            self.assertTrue(error_shown.wait(2))
            exists, value, state = self._on_gui(
                lambda _root: (dialog.winfo_exists(), text.get("1.0", "end-1c"),
                               str(buttons["Salvar comandos"].cget("state"))),
            )
        self.assertTrue(exists)
        self.assertEqual(value, "New text")
        self.assertEqual(state, tk.NORMAL)
        self.assertEqual(self.app.snippets, library)
        self.app._load_commands()
        self.assertEqual(self.app.snippets, library)

    def test_saving_an_unchanged_legacy_command_preserves_its_phrase_and_empty_value(self):
        library = {"Existing Phrase": ""}
        self.app._save_commands(library, {})
        saved = threading.Event()
        original_save = self.app._save_commands

        def persist(commands, expected):
            original_save(commands, expected)
            saved.set()

        def select_and_save(shared_root):
            _dialog, _phrase, _text, tree, buttons = self._command_editor_widgets(shared_root)
            tree.selection_set("Existing Phrase")
            tree.event_generate("<<TreeviewSelect>>")
            buttons["Salvar comandos"].invoke()

        with mock.patch.object(self.app, "_save_commands", side_effect=persist), \
                mock.patch.object(tx.messagebox, "showerror") as showerror:
            self._on_gui(select_and_save)
            self.assertTrue(saved.wait(2))
            self._on_gui(lambda _root: None)
        showerror.assert_not_called()
        self.assertEqual(self.app.snippets, library)

    def test_open_voice_settings_selects_the_manager_tab(self):
        _ensure_voice(self.app)

        def open_settings(shared_root):
            self.app._show_voice_settings(shared_root)
            self.app.manager_window.update_idletasks()
            notebook = self.app._manager_notebook
            selected = notebook.select()
            labels = {
                str(widget.cget("text"))
                for widget in _descendants(self.app.manager_window)
                if isinstance(widget, (tk.Label, ttk.Label))
            }
            return (
                notebook.tab(selected, "text"),
                _notebook_titles(self.app.manager_window),
                str(notebook.cget("style")),
                labels,
                (
                    self.app.manager_window.winfo_width(),
                    self.app.manager_window.winfo_height(),
                ),
            )

        title, titles, notebook_style, labels, manager_size = self._on_gui(open_settings)
        self.assertEqual(title, "Ditado")
        self.assertEqual(notebook_style, "Pages.TNotebook")
        self.assertGreaterEqual(manager_size[0], 900)
        self.assertGreaterEqual(manager_size[1], 700)
        self.assertLessEqual(manager_size[0], 1120)
        self.assertLessEqual(manager_size[1], 820)
        self.assertIn("100% local", labels)
        self.assertIn("sem upload automático", labels)
        self.assertNotIn("Fale naturalmente", labels)
        self.assertNotIn("Texto pronto", labels)
        self.assertNotIn("Em qualquer app", labels)
        self.assertIn("Modelo e idioma", labels)
        self.assertIn("Atalhos", labels)
        self.assertIn("Modelos de transcrição", labels)
        self.assertIn("Modelos de resumo de texto", labels)
        self.assertEqual(
            titles,
            [
                "Gravação",
                "Biblioteca",
                "Ditado",
                "Configurações",
            ],
        )

    def test_manager_destination_shortcuts_follow_tab_order(self):
        def use_shortcuts(shared_root):
            self.app._show_manager_window(shared_root)
            window = self.app.manager_window
            notebook = self.app._manager_notebook
            window.update_idletasks()
            window.focus_force()
            window.update()
            selected = []
            for key in ("2", "4", "1"):
                window.event_generate(f"<Control-Key-{key}>")
                window.update()
                selected.append(notebook.tab(notebook.select(), "text"))
            return selected

        self.assertEqual(
            self._on_gui(use_shortcuts),
            ["Biblioteca", "Configurações", "Gravação"],
        )

    def test_manager_applies_persisted_dark_appearance(self):
        _ensure_voice(self.app)
        self.app.settings["appearance"] = "dark"

        def inspect_manager(shared_root):
            self.app._show_manager_window(shared_root)
            self.app.manager_window.update_idletasks()
            view = self.app._manager_meeting_view
            result = (
                ui_theme.theme().kind,
                ui_theme.theme().preference,
                view.appearance_display.get(),
                tuple(view.appearance_box.cget("values")),
            )
            ui_theme.reset()
            return result

        kind, preference, selected, choices = self._on_gui(inspect_manager)
        self.assertEqual((kind, preference), ("dark", "dark"))
        self.assertEqual(selected, "Escuro")
        self.assertEqual(choices, ("Sistema", "Claro", "Escuro"))

    def test_manager_dialogs_take_the_app_icon_from_the_host(self):
        _ensure_voice(self.app)

        def build(shared_root):
            self.app._show_manager_window(shared_root)
            return self.app._manager_meeting_view.set_window_icon

        self.assertEqual(self._on_gui(build), self.app._set_window_icon)

    def _general_data_card(self, shared_root):
        self.app._show_manager_window(shared_root)
        view = self.app._manager_meeting_view
        notebook = self.app._manager_notebook
        settings = next(
            notebook.nametowidget(tab_id) for tab_id in notebook.tabs()
            if notebook.tab(tab_id, "text") == "Configurações"
        )
        labels = [
            str(widget.cget("text")) for widget in _descendants(settings)
            if isinstance(widget, (tk.Label, ttk.Label))
        ]
        return view, labels

    def test_general_settings_show_the_data_folder_and_request_a_move(self):
        import meeting_gui

        _ensure_voice(self.app)
        target = os.path.join(tempfile.mkdtemp(), "snipvoice")
        self.app.request_data_relocation = mock.Mock(return_value="")

        def inspect_card(shared_root):
            view, labels = self._general_data_card(shared_root)
            card = view.location_cards["data"]
            states = (str(card["move"]["state"]), str(card["default"]["state"]))
            with mock.patch.object(meeting_gui.messagebox, "askokcancel", return_value=True) as ask, \
                    mock.patch.object(meeting_gui.messagebox, "showerror") as error:
                view._confirm_location_move("data", target)
                view._confirm_location_move("data", os.path.join(self.app.data_dir, "inside"))
            return labels, card["display"].get(), states, ask.call_args, error.call_args

        labels, shown, states, asked, error = self._on_gui(inspect_card)
        self.assertIn("Pasta de dados", labels)
        self.assertEqual(shown, self.app.data_dir)
        self.assertEqual(states, ("normal", "normal"))
        self.assertIn(target, asked.args[1])
        self.assertIn(self.app.data_dir, asked.args[1])
        self.app.request_data_relocation.assert_called_once_with(target)
        self.assertIn("dentro", error.args[1])

    def test_env_override_locks_the_data_folder_card(self):
        _ensure_voice(self.app)

        def inspect_card(shared_root):
            with mock.patch.dict(os.environ, {"SNIPVOICE_HOME": self.app.data_dir}):
                view, labels = self._general_data_card(shared_root)
            card = view.location_cards["data"]
            return labels, str(card["move"]["state"]), str(card["default"]["state"])

        labels, move_state, default_state = self._on_gui(inspect_card)
        self.assertEqual((move_state, default_state), ("disabled", "disabled"))
        self.assertTrue(any("SNIPVOICE_HOME" in label for label in labels))

    def test_models_settings_move_models_to_a_shared_folder(self):
        import meeting_gui

        voice = _ensure_voice(self.app)
        voice.settings.cache_dir = None
        base = tempfile.mkdtemp()
        current = os.path.join(base, "local", "Snipvoice")
        os.makedirs(os.path.join(current, "voice-models", "m"))
        shared = os.path.join(base, "shared")
        os.makedirs(os.path.join(shared, "other-app"))
        self.app.request_models_relocation = mock.Mock(return_value="")

        def inspect_card(shared_root):
            view, labels = self._general_data_card(shared_root)
            card = view.location_cards["models"]
            states = (str(card["move"]["state"]), str(card["default"]["state"]))
            placed = (str(card["move"]).rsplit(".", 2)[0], str(view.settings_sections.frames["models"]))
            with mock.patch.object(meeting_gui.messagebox, "askokcancel", return_value=True) as ask:
                view._confirm_location_move("models", shared)
                view.summary_download_cancel = threading.Event()
                with mock.patch.object(meeting_gui.messagebox, "showerror") as error:
                    view._confirm_location_move("models", shared)
                view.summary_download_cancel = None
            return labels, card["display"].get(), states, ask.call_args, error.call_args, placed

        with mock.patch.dict(os.environ, {"SNIPVOICE_VOICE_CACHE": "", "SNIPVOICE_SUMMARY_CACHE": ""}), \
                mock.patch.object(tx.app_paths, "config_dir", return_value=os.path.join(base, "config")), \
                mock.patch.object(tx.app_paths, "default_models_dir", return_value=current):
            labels, shown, states, asked, error, placed = self._on_gui(inspect_card)
        self.assertIn("Pasta dos modelos", labels)
        self.assertEqual(placed[0], placed[1])
        self.assertEqual(shown, current)
        self.assertEqual(states, ("normal", "disabled"))
        self.assertIn(shared, asked.args[1])
        self.app.request_models_relocation.assert_called_once_with(shared)
        self.assertIn("download", error.args[1])

    def test_manager_separates_model_settings_from_ditado_selectors(self):
        """Configurações owns downloads; Ditado keeps compact friendly selectors."""
        _ensure_voice(self.app)

        def inspect_manager(shared_root):
            self.app._show_manager_window(shared_root)
            notebook = self.app._manager_notebook
            tabs = {
                notebook.tab(tab_id, "text"): notebook.nametowidget(tab_id)
                for tab_id in notebook.tabs()
            }
            settings = tabs["Configurações"]
            ditado = tabs["Ditado"]
            settings_text = [
                str(widget.cget("text"))
                for widget in _descendants(settings)
                if isinstance(widget, (tk.Label, ttk.Label))
            ]
            combos = [
                widget for widget in _descendants(ditado)
                if isinstance(widget, ttk.Combobox)
            ]
            profile_box = next(
                widget for widget in combos
                if any("Equilibrado" in value for value in widget["values"])
            )
            language_box = next(
                widget for widget in combos
                if any("Automático" in value for value in widget["values"])
            )
            profile_values = tuple(profile_box["values"])
            language_values = tuple(language_box["values"])
            profile_box.set("Preciso · Qwen 0.6B")
            profile_box.event_generate("<<ComboboxSelected>>")
            compact_language_values = tuple(language_box["values"])
            profile_box.set("Intermediário · Whisper Turbo")
            profile_box.event_generate("<<ComboboxSelected>>")
            whisper_language_values = tuple(language_box["values"])
            whisper_selected = self.app._manager_voice_tk_vars[0].get()
            profile_box.set("Equilibrado · Parakeet")
            profile_box.event_generate("<<ComboboxSelected>>")
            language_box.set("Português (Brasil)")
            language_box.event_generate("<<ComboboxSelected>>")
            self.app.manager_window.update_idletasks()
            selected, language, _hotkey, _command, _activation = self.app._manager_voice_tk_vars
            return (
                settings_text,
                profile_values,
                language_values,
                compact_language_values,
                whisper_language_values,
                whisper_selected,
                selected.get(),
                language.get(),
            )

        (settings_text, profile_values, language_values, compact_language_values,
         whisper_language_values, whisper_selected,
         selected, language) = self._on_gui(inspect_manager)
        self.assertIn("Modelos de transcrição", settings_text)
        self.assertIn("Modelos de resumo de texto", settings_text)
        self.assertEqual(
            profile_values,
            (
                "Leve · Whisper Small",
                "Equilibrado · Parakeet",
                "Intermediário · Whisper Turbo",
                "Preciso · Qwen 0.6B",
                "Muito preciso · Qwen 1.7B",
                "Máxima precisão · Whisper Large",
            ),
        )
        self.assertEqual(whisper_selected, "whisper-turbo")
        self.assertEqual(whisper_language_values, language_values)
        self.assertIn("Automático", language_values)
        self.assertIn("Português (Brasil)", language_values)
        self.assertEqual(compact_language_values, ("Automático",))
        self.assertEqual(selected, "balanced")
        self.assertEqual(language, "pt-BR")

    def test_tray_open_reuses_manager_and_keeps_the_last_destination_while_idle(self):
        _ensure_voice(self.app)

        def select_recording(shared_root):
            self.app._show_voice_settings(shared_root)
            manager = self.app.manager_window
            self.app._show_meetings(shared_root)
            notebook = self.app._manager_notebook
            return (
                self.app.manager_window is manager,
                notebook.tab(notebook.select(), "text"),
                len([
                    child for child in shared_root.winfo_children()
                    if isinstance(child, tk.Toplevel)
                ]),
            )

        reused, title, top_levels = self._on_gui(select_recording)
        self.assertTrue(reused)
        self.assertEqual(title, "Ditado")
        self.assertEqual(top_levels, 1)

    def test_recording_level_meters_fit_inside_the_manager_viewport(self):
        _ensure_voice(self.app)

        def measure(shared_root):
            with mock.patch.object(tk.Misc, "winfo_screenwidth", return_value=1024), \
                    mock.patch.object(tk.Misc, "winfo_screenheight", return_value=768):
                self.app._show_meetings(shared_root)
            manager = self.app.manager_window
            manager.geometry("1024x749+0+0")
            manager.update_idletasks()
            tab = self.app._manager_recording_tab
            meters = self.app._manager_meeting_view.meters.values()
            visible_bottom = tab.winfo_rooty() + tab.winfo_height()
            controls_bottom = max(
                meter.winfo_rooty() + meter.winfo_height()
                for meter in meters
            )
            return controls_bottom, visible_bottom

        controls_bottom, visible_bottom = self._on_gui(measure)
        self.assertLessEqual(controls_bottom, visible_bottom)

    def test_recording_page_fits_without_scrolling_at_minimum_size(self):
        def measure(shared_root):
            self.app._show_manager_window(shared_root)
            manager = self.app.manager_window
            _geometry, min_width, min_height = ui_theme.theme().manager_window_size
            manager.geometry(f"{min_width}x{min_height}")
            manager.update()
            view = self.app._manager_meeting_view
            canvas = view.recording_canvas
            canvas_box = (canvas.winfo_rootx(), canvas.winfo_rooty(),
                          canvas.winfo_rootx() + canvas.winfo_width(),
                          canvas.winfo_rooty() + canvas.winfo_height())
            # Idle: Iniciar gravação is the one transport control on show.
            self.assertTrue(view.start_button.winfo_ismapped())
            self.assertFalse(view.stop_button.winfo_ismapped())
            self.assertFalse(view.pause_button.winfo_ismapped())
            controls = [*view.meters.values(), view.record_chip, view.start_button,
                        view.preview_button]
            boxes = [(widget.winfo_rootx(), widget.winfo_rooty(),
                      widget.winfo_rootx() + widget.winfo_width(),
                      widget.winfo_rooty() + widget.winfo_height()) for widget in controls]
            return canvas.yview(), canvas_box, boxes

        view_range, canvas_box, boxes = self._on_gui(measure)
        self.assertEqual(view_range, (0.0, 1.0))
        for left, top, right, bottom in boxes:
            self.assertGreaterEqual(left, canvas_box[0])
            self.assertGreaterEqual(top, canvas_box[1])
            self.assertLessEqual(right, canvas_box[2])
            self.assertLessEqual(bottom, canvas_box[3])

    def test_lost_source_warning_keeps_stop_visible_at_minimum_size(self):
        health = {"microphone": {"state": "receiving", "reason": "", "last_packet_age": 0.0},
                  "system": {"state": "disconnected", "reason": "endpoint removed",
                             "last_packet_age": 65.0}}
        snapshot = {"state": "recording", "levels": {}, "elapsed": 2754, "processing": False,
                    "capture_health": health,
                    "error": "A gravação não está recebendo áudio do sistema."}

        def measure(shared_root):
            self.app._show_manager_window(shared_root)
            manager = self.app.manager_window
            _geometry, min_width, min_height = ui_theme.theme().manager_window_size
            manager.geometry(f"{min_width}x{min_height}")
            manager.update()
            view = self.app._manager_meeting_view
            # One poll against the synthetic snapshot, without the repeating timer.
            shared_root.after_cancel(view.after_id)
            with mock.patch.object(view.controller, "snapshot", return_value=snapshot):
                view._poll()
            shared_root.after_cancel(view.after_id)
            manager.update()
            canvas = view.recording_canvas
            titles = [widget.cget("text") for widget in _descendants(view.recording_trouble)
                      if isinstance(widget, tk.Label)]
            return {
                "yview": canvas.yview(),
                "stop": (view.stop_button.winfo_ismapped(),
                         view.stop_button.winfo_rooty() + view.stop_button.winfo_height(),
                         str(view.stop_button.cget("bg"))),
                "start": view.start_button.winfo_ismapped(),
                "pause": view.pause_button.winfo_ismapped(),
                "canvas_bottom": canvas.winfo_rooty() + canvas.winfo_height(),
                "primary": ui_theme.theme().accent,
                "trouble": titles,
                "captions": {key: caption.cget("text") for key, caption in view.source_captions.items()},
                "status": view.record_status.get(),
            }

        measured = self._on_gui(measure)
        # The page has not scrolled away from the top: the line under the recorder
        # may sit below the fold at this height, the meters, the warning and the
        # transport do not.
        self.assertEqual(measured["yview"][0], 0.0)
        mapped, bottom, background = measured["stop"]
        self.assertTrue(mapped)
        self.assertLessEqual(bottom, measured["canvas_bottom"])
        # Parar e salvar is the copper action while recording, beside Pausar.
        self.assertEqual(background, measured["primary"])
        self.assertFalse(measured["start"])
        self.assertTrue(measured["pause"])
        self.assertIn("Sem áudio do sistema há 1 min 05 s", measured["trouble"])
        self.assertEqual(measured["captions"]["system"], "Sem áudio · há 1 min 05 s")
        self.assertEqual(measured["status"], "Gravando · 00:45:54 · Áudio do sistema: sem áudio")

    def test_sidebar_navigates_pages_in_tab_order(self):
        def navigate(shared_root):
            self.app._show_manager_window(shared_root)
            window = self.app.manager_window
            notebook = self.app._manager_notebook
            window.update()
            buttons = [
                widget for widget in _descendants(window)
                if isinstance(widget, tk.Button)
                and widget.cget("text").partition("\n")[0] in {"Gravação", "Biblioteca", "Ditado", "Configurações"}
                and notebook not in _ancestors(widget)
            ]
            labels = [button.cget("text").partition("\n")[0] for button in buttons]
            logos = [
                widget for widget in _descendants(window)
                if isinstance(widget, tk.Label) and widget.cget("image")
                and notebook not in _ancestors(widget)
            ]
            logo_size = (logos[0].winfo_reqwidth(), logos[0].winfo_reqheight()) if logos else None
            library = buttons[labels.index("Biblioteca")]
            library.invoke()
            window.update()
            return (labels, notebook.tab(notebook.select(), "text"),
                    str(library.cget("font")), str(buttons[0].cget("font")), len(logos), logo_size)

        labels, selected, library_font, recording_font, logos, logo_size = self._on_gui(navigate)
        self.assertEqual(logos, 1, "expected the app icon beside the name")
        self.assertGreaterEqual(min(logo_size), 16)
        self.assertEqual(labels, ["Gravação", "Biblioteca", "Ditado", "Configurações"])
        self.assertEqual(selected, "Biblioteca")
        self.assertIn("bold", library_font)
        self.assertNotIn("bold", recording_font)

    def test_ditado_tab_is_last_and_recording_is_default_when_voice_is_unavailable(self):
        self.app.voice = None

        def open_manager(shared_root):
            self.app._show_manager_window(shared_root)
            notebook = self.app._manager_notebook
            return (_notebook_titles(self.app.manager_window),
                    notebook.tab(notebook.select(), "text"))

        titles, selected = self._on_gui(open_manager)
        self.assertTrue(titles, "expected manager notebook tabs")
        self.assertEqual(
            titles,
            [
                "Gravação",
                "Biblioteca",
                "Ditado",
                "Configurações",
            ],
        )
        self.assertEqual(selected, "Gravação")
        self.assertIsNone(self.app._manager_voice_refresher)

    def test_manager_hide_keeps_voice_refresher_query_and_selection(self):
        _ensure_voice(self.app)

        def cycle(shared_root):
            self.app._show_manager_window(shared_root)
            first = self.app._manager_voice_refresher
            window = self.app.manager_window
            view = self.app._manager_meeting_view
            view.query.set("synthetic query")
            view.selected = "synthetic-recording"
            self.app._manager_notebook.select(view.library_tab)
            handler = window.protocol("WM_DELETE_WINDOW")
            window.tk.call(handler)
            closed_refresher = self.app._manager_voice_refresher
            self.app._show_manager_window(shared_root)
            second = self.app._manager_voice_refresher
            titles = _notebook_titles(self.app.manager_window)
            return (
                first is not None,
                closed_refresher is first,
                second is not None,
                first is second,
                titles,
                self.app.manager_window is window,
                view.query.get(), view.selected,
                self.app._manager_notebook.select() == str(view.library_tab),
            )

        bound, retained, rebound, same, titles, same_window, query, selected, library = self._on_gui(cycle)
        self.assertTrue(bound)
        self.assertTrue(retained)
        self.assertTrue(rebound)
        self.assertTrue(same, "hiding keeps the existing refresher and Tk variables alive")
        self.assertTrue(same_window)
        self.assertEqual((query, selected), ("synthetic query", "synthetic-recording"))
        self.assertTrue(library)
        self.assertIn("Ditado", titles)


def _ensure_voice(app):
    if app.voice is not None:
        return app.voice
    voice = mock.Mock()
    voice.is_enabled.return_value = False
    voice.status_label.return_value = "Entrada por voz"
    voice.settings.profile = "balanced"
    voice.settings.language = "auto"
    voice.settings.hotkey = "ctrl+alt+space"
    voice.settings.command_hotkey = "ctrl+alt+shift+space"
    voice.cache_dir = tempfile.mkdtemp()
    voice.model_download_in_progress.return_value = False
    app.voice = voice
    return voice


def _notebook_titles(window):
    notebooks = [
        widget for widget in _descendants(window)
        if isinstance(widget, ttk.Notebook)
    ]
    if not notebooks:
        return []
    notebook = notebooks[0]
    return [notebook.tab(tab_id, "text") for tab_id in notebook.tabs()]


def _form_windows(root):
    return [c for c in root.winfo_children()
            if isinstance(c, tk.Toplevel) and c.title() == "Preencher campos"]


def _find_form(root, label_text):
    for window in _form_windows(root):
        for widget in _descendants(window):
            if isinstance(widget, tk.Label) and label_text in str(widget.cget("text")):
                return window
    return None


@unittest.skipUnless(TK_AVAILABLE, TK_SKIP_REASON)
class ModalDialogSerializationTests(unittest.TestCase):
    """A second expansion dialog must be refused, never stacked.

    Stacked dialogs block their workers in nested event loops that unwind
    strictly LIFO: answering the older one first stranded its caller and lost
    its result. See Snipvoice._run_modal_dialog.
    """

    def setUp(self):
        self.app = _make_app(tempfile.mkdtemp())
        for name, result in (
            ("capture_text_target", ("hwnd", 42)),
            ("restore_text_target", True),
        ):
            patcher = mock.patch.object(tx.platform_support, name, return_value=result)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.app.gui.ensure_started()
        self.results = {}

    def tearDown(self):
        self.app.gui.stop()

    def _open_form(self, field):
        def worker():
            self.results[field] = self.app._show_form_dialog([field])

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        return thread

    def _wait_for_form(self, label, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.app.gui.call(lambda root: _find_form(root, label) is not None, timeout=10):
                return
            time.sleep(0.05)
        self.fail(f"form dialog {label!r} never appeared")

    def _answer_form(self, label, value):
        def act(root):
            window = _find_form(root, label)
            if window is None:
                return False
            entry = [w for w in _descendants(window) if isinstance(w, tk.Entry)][0]
            entry.insert(0, value)
            window.event_generate("<Return>")
            return True

        self.assertTrue(self.app.gui.call(act, timeout=10), f"could not answer {label!r}")




# GuiThread's own marshaling contract (call/submit/stop, exceptions, reentrancy,
# stranded callers) is covered directly and adversarially in test_gui_thread.py.
# This file keeps only the manager-GUI construction and dialog-serialization
# smoke tests that genuinely exercise Snipvoice widgets on the shared root.


if __name__ == "__main__":
    unittest.main()
