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
import inspect
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


@unittest.skipUnless(TK_AVAILABLE, TK_SKIP_REASON)
class ManagerGuiSmokeTests(unittest.TestCase):
    def setUp(self):
        self.app = _make_app(tempfile.mkdtemp())
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
            return checked, labels, buttons, len(selectors)

        checked, labels, buttons, selector_count = self._on_gui(build)
        self.assertTrue(checked)
        self.assertIn("Entrada por voz (pronta)", labels)
        self.assertIn("Salvar e usar", buttons)
        self.assertIn("Remover modelo", buttons)
        self.assertIn("Recarregar comandos", buttons)
        self.assertIn("Histórico de voz…", buttons)
        self.assertIn("Licenças e atribuições…", buttons)
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
            button = next(
                widget for widget in _descendants(frame)
                if isinstance(widget, tk.Button)
                and str(widget.cget("text")) == "Licenças e atribuições…"
            )
            button.invoke()
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
                if isinstance(widget, tk.Button)
            }
            expected = {
                "Salvar e usar",
                "Remover modelo",
                "Histórico de voz…",
                "Licenças e atribuições…",
            }
            self.assertTrue(expected.issubset(actions), actions)
            result = frame.winfo_height(), {
                name: widget.winfo_rooty() - frame.winfo_rooty()
                + widget.winfo_height()
                for name, widget in actions.items()
                if name in expected
            }, {name: int(widget.cget("pady")) for name, widget in actions.items()
                if name in expected}
            root.destroy()
            return result

        available_height, action_bottoms, action_padding = self._on_gui(measure)
        self.assertGreater(available_height, 1)
        for name, bottom in action_bottoms.items():
            self.assertLessEqual(
                bottom,
                available_height,
                f"{name!r} is clipped below the voice tab",
            )
        if not IS_MAC:
            for name, padding in action_padding.items():
                self.assertGreaterEqual(padding, 6, f"{name!r} is too thin")

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
            selected, language, _hotkey, _command = self.app._manager_voice_tk_vars
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
        self.assertTrue(any("Precisão" in text for text in after_display), after_display)
        self.assertTrue(any("Automático" in text for text in after_display), after_display)

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
        voice.download_profile.assert_called_once_with("compact")
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
        self.assertEqual(notebook_style, "Manager.TNotebook")
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
            profile_box.set("Compacto · Qwen 0.6B")
            profile_box.event_generate("<<ComboboxSelected>>")
            compact_language_values = tuple(language_box["values"])
            profile_box.set("Whisper Large v3 Turbo")
            profile_box.event_generate("<<ComboboxSelected>>")
            whisper_language_values = tuple(language_box["values"])
            whisper_selected = self.app._manager_voice_tk_vars[0].get()
            profile_box.set("Equilibrado · Parakeet TDT")
            profile_box.event_generate("<<ComboboxSelected>>")
            language_box.set("Português (Brasil)")
            language_box.event_generate("<<ComboboxSelected>>")
            self.app.manager_window.update_idletasks()
            selected, language, _hotkey, _command = self.app._manager_voice_tk_vars
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
        self.assertIn("Equilibrado · Parakeet TDT", profile_values)
        self.assertIn("Compacto · Qwen 0.6B", profile_values)
        self.assertIn("Precisão · Qwen 1.7B", profile_values)
        self.assertIn("Whisper Small", profile_values)
        self.assertIn("Whisper Large v3 Turbo", profile_values)
        self.assertIn("Whisper Large v3", profile_values)
        self.assertNotIn("Transcrição contínua", profile_values)
        self.assertEqual(whisper_selected, "whisper-turbo")
        self.assertEqual(whisper_language_values, language_values)
        self.assertIn("Automático", language_values)
        self.assertIn("Português (Brasil)", language_values)
        self.assertEqual(compact_language_values, ("Automático",))
        self.assertEqual(selected, "balanced")
        self.assertEqual(language, "pt-BR")
        self.assertNotIn("Qwen3-ASR-0.6B", profile_values)
        self.assertNotIn("0.0.0.00000000", profile_values)

    def test_meeting_shortcut_reuses_manager_and_selects_recording_tab(self):
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
        self.assertEqual(title, "Gravação")
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

    def test_manager_reopen_rebinds_voice_refresher(self):
        _ensure_voice(self.app)

        def cycle(shared_root):
            self.app._show_manager_window(shared_root)
            first = self.app._manager_voice_refresher
            window = self.app.manager_window
            handler = window.protocol("WM_DELETE_WINDOW")
            window.tk.call(handler)
            closed_refresher = self.app._manager_voice_refresher
            self.app._show_manager_window(shared_root)
            second = self.app._manager_voice_refresher
            titles = _notebook_titles(self.app.manager_window)
            return (
                first is not None,
                closed_refresher is None,
                second is not None,
                first is second,
                titles,
            )

        bound, cleared, rebound, same, titles = self._on_gui(cycle)
        self.assertTrue(bound)
        self.assertTrue(cleared)
        self.assertTrue(rebound)
        self.assertFalse(same, "reopen must register a new refresher")
        self.assertIn("Ditado", titles)


class VoiceTabStructureTests(unittest.TestCase):
    def test_voice_tab_does_not_render_removed_benefit_cards(self):
        source = inspect.getsource(tx.Snipvoice._create_voice_tab)
        for label in ("Fale naturalmente", "Texto pronto", "Em qualquer app"):
            self.assertNotIn(label, source)


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
