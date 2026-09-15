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
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app_module import snipvoice as tx  # .pyw is not importable off Windows
from gui_thread import GuiThread
from platform_support import IS_MAC

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
        return self.app.gui.call(func, timeout=30)



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
            radios = [
                widget for widget in _descendants(frame)
                if isinstance(widget, tk.Radiobutton)
            ]
            labels = [
                str(widget.cget("text")) for widget in _descendants(frame)
                if isinstance(widget, tk.Label)
            ]
            checked = bool(int(checkbox.getvar(checkbox.cget("variable"))))
            checkbox.invoke()
            return checked, labels, buttons, len(radios)

        checked, labels, buttons, radio_count = self._on_gui(build)
        self.assertTrue(checked)
        self.assertIn("Entrada por voz (pronta)", labels)
        self.assertIn("Salvar e usar", buttons)
        self.assertIn("Remover modelo", buttons)
        self.assertIn("Histórico de voz…", buttons)
        self.assertIn("Licenças e atribuições…", buttons)
        self.assertFalse(any("transcribe.cpp — MIT" in text for text in labels))
        self.assertFalse(any(text.startswith("Configurar voz") for text in buttons))
        self.assertGreaterEqual(radio_count, 2)
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
            }
            root.destroy()
            return result

        available_height, action_bottoms = self._on_gui(measure)
        self.assertGreater(available_height, 1)
        for name, bottom in action_bottoms.items():
            self.assertLessEqual(
                bottom,
                available_height,
                f"{name!r} is clipped below the voice tab",
            )

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
            with mock.patch("voice_models.model_is_installed", return_value=False):
                self.app._create_voice_tab(frame, root)
            root.update_idletasks()
            before = [
                str(widget.cget("text")) for widget in _descendants(frame)
                if isinstance(widget, tk.Radiobutton)
            ]
            selected, language, _hotkey, _command = self.app._manager_voice_tk_vars
            before_state = (selected.get(), language.get())
            voice.settings.profile = "accuracy"
            voice.settings.language = "auto"
            with mock.patch("voice_models.model_is_installed", return_value=True):
                self.app._manager_voice_refresher()
            after = [
                str(widget.cget("text")) for widget in _descendants(frame)
                if isinstance(widget, tk.Radiobutton)
            ]
            after_state = (selected.get(), language.get())
            return before, before_state, after, after_state

        before, before_state, after, after_state = self._on_gui(build_and_refresh)
        self.assertTrue(any("não baixado" in text for text in before), before)
        self.assertEqual(before_state, ("balanced", "pt-BR"))
        self.assertTrue(any("instalado" in text for text in after), after)
        self.assertEqual(after_state, ("accuracy", "auto"))

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
            root = tk.Toplevel(shared_root)
            root.withdraw()
            frame = tk.Frame(root)
            with mock.patch("voice_models.model_is_installed", return_value=False), \
                    mock.patch.object(tx.messagebox, "askokcancel", return_value=True):
                self.app._create_voice_tab(frame, root)
                root.update_idletasks()
                rows = [
                    widget for widget in _descendants(frame)
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
                if isinstance(widget, tk.Label)
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
        self.assertIn("Voz", title)
        self.assertEqual(notebook_style, "Manager.TNotebook")
        self.assertEqual(manager_size, (1120, 820))
        self.assertIn("Processamento local", labels)
        self.assertIn("Modelo e idioma", labels)
        self.assertIn("Atalhos", labels)
        self.assertEqual(
            titles,
            [
                "Voz",
                "Gravação",
                "Biblioteca",
                "Resumo",
            ],
        )

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

    def test_voice_tab_absent_when_controller_missing(self):
        self.app.voice = None

        def open_manager(shared_root):
            self.app._show_manager_window(shared_root)
            return _notebook_titles(self.app.manager_window)

        titles = self._on_gui(open_manager)
        self.assertTrue(titles, "expected manager notebook tabs")
        self.assertTrue(
            all("Voz" not in title for title in titles),
            titles,
        )
        self.assertEqual(
            titles,
            [
                "Diagnóstico",
                "Gravação",
                "Biblioteca",
                "Resumo",
            ],
        )
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
        self.assertTrue(any("Voz" in title for title in titles), titles)


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
