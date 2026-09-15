"""Snipvoice - local push-to-talk dictation for Windows and macOS.
Version: 2.0.0
Channel: stable

Recording is explicit and local. Audio and transcripts are kept in recoverable
history; model downloads are the only transcription-related network operation.
"""

import ctypes
import gc
import json
import os
import sys
import threading
import time


def run_voice_runtime_probe_if_requested(argv=None):
    """Run packaged diagnostics before desktop imports or instance locks."""
    arguments = sys.argv[1:] if argv is None else argv
    if "--voice-runtime-probe" not in arguments:
        return False
    from voice_runtime_probe import main as probe_main
    raise SystemExit(probe_main())


def run_summary_runtime_probe_if_requested(argv=None):
    """Run the packaged llama.cpp diagnostic before desktop imports."""
    arguments = sys.argv[1:] if argv is None else argv
    if "--summary-runtime-probe" not in arguments:
        return False
    from summary_runtime_probe import main as probe_main
    raise SystemExit(probe_main())


run_voice_runtime_probe_if_requested()
run_summary_runtime_probe_if_requested()

if "--meeting-capture-probe" in sys.argv[1:]:
    from meeting_audio import NativeCapture
    NativeCapture().self_test()
    raise SystemExit(0)

import platform_support
platform_support.pin_tray_backend()

from pynput.keyboard import Controller
import pystray
from PIL import Image
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from app_paths import ensure_data_dir
from gui_support import center_dialog, center_on_screen
from gui_thread import GuiThread
import macos_permissions
from runtime_support import AppLogger, BackgroundTaskRunner, TextInserter, configure_logging
from settings_support import load_settings, save_settings
from trigger_index import compile_trigger_index
import ui_theme
from voice_dispatch import VoiceTarget
from voice_indicator import VoiceStatusIndicator
from voice_support import VoiceController
from meeting_support import MeetingController
from meeting_settings import resolve_meeting_settings, validate_hotkey_conflicts

APP_VERSION = "2.0.0"
RELEASE_CHANNEL = "stable"
APP_DISPLAY_NAME = f"Snipvoice v{APP_VERSION}"
if RELEASE_CHANNEL != "stable":
    APP_DISPLAY_NAME = f"{APP_DISPLAY_NAME} {RELEASE_CHANNEL}"
APP_MUTEX_NAME = r"Local\SnipvoiceSingleton"
APP_MUTEX_HANDLES = []


def acquire_single_instance_mutex():
    """Keep Snipvoice copies exclusive while allowing Sniptype to coexist."""
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.GetLastError.restype = ctypes.c_ulong
    handle = kernel32.CreateMutexW(None, False, APP_MUTEX_NAME)
    if not handle:
        raise OSError("Could not acquire the Snipvoice instance mutex")
    if kernel32.GetLastError() == 183:
        kernel32.CloseHandle(handle)
        return False
    APP_MUTEX_HANDLES.append(handle)
    return True


def get_runtime_resource_dir():
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


class Snipvoice:
    """Standalone voice tray app; the only global listener observes voice chords."""

    def __init__(self):
        self.data_dir = ensure_data_dir()
        configure_logging(os.path.join(self.data_dir, "logs"))
        self.logger = AppLogger()
        self.task_runner = BackgroundTaskRunner()
        self.gui = GuiThread(logger=self.logger)
        self.settings_file = os.path.join(self.data_dir, "settings.json")
        self.settings = load_settings(self.settings_file)
        self.keyboard_controller = Controller()
        timings = platform_support.insertion_timings(self.settings)
        self.text_inserter = TextInserter(
            self.keyboard_controller, logger=self.logger, notify=self.notify_error,
            settle_delay=timings["clipboard_settle_delay"],
            restore_delay=timings["paste_restore_delay"],
        )
        self.icon = None
        self.manager_window = None
        self._manager_notebook = None
        self._manager_voice_tab = None
        self._manager_voice_refresher = None
        self._manager_voice_tk_vars = []
        self._manager_meeting_view = None
        self._manager_recording_tab = None
        self._manager_library_tab = None
        self.voice_status_indicator = None
        self._quitting = threading.Event()
        self._autostart_state = platform_support.AUTOSTART_ABSENT
        self._notification_lock = threading.Lock()
        self._notification_times = {}
        self.voice = None
        self._meeting_monitor = None
        self._settings_lock = threading.RLock()
        self.snippets = {}
        self.trigger_index = compile_trigger_index({}, set())
        self._load_commands()
        self.voice = VoiceController(
            self.settings, task_runner=self.task_runner,
            insert_text=self._insert_voice_text, expand_trigger=self.expand_from_voice,
            notify=self.notify_error, logger=self.logger,
            persist_settings=self._persist_voice_settings,
            capture_target=self._capture_voice_target, restore_target=self._restore_voice_target,
            secure_input_blocks=macos_permissions.secure_input_enabled,
            microphone_status=macos_permissions.check_microphone,
            on_status_change=self._voice_status_changed,
            history_dir=os.path.join(self.data_dir, "voice-history"),
        )
        self.voice.bind_library(lambda: self.snippets, lambda: self.trigger_index)
        self.meetings = MeetingController(os.path.join(self.data_dir, "meetings"),
                                          self.voice, notify=self.notify_error)

    def open_meetings(self, icon=None, item=None):
        self.gui.submit(self._show_meetings)

    def _show_meetings(self, root):
        self._show_manager_window(root)
        notebook = self._manager_notebook
        tab = self._manager_recording_tab
        if notebook is None or tab is None:
            return
        try:
            notebook.select(tab)
        except tk.TclError:
            pass

    def _rebuild_meeting_monitor(self):
        # Called from the GUI worker after persistence; listener construction stays off Tk.
        from voice_hotkey import VoiceHotkeyMonitor, parse_chord
        with self._settings_lock:
            if self._meeting_monitor is not None:
                self._meeting_monitor.stop()
                self._meeting_monitor = None
            if self._quitting.is_set():
                return
            try:
                validate_hotkey_conflicts(self.settings)
                settings = resolve_meeting_settings(self.settings)
                if not settings.hotkey:
                    return
                chord = parse_chord(settings.hotkey)
                monitor = VoiceHotkeyMonitor(chord, chord,
                    on_press=lambda mode: self.meetings.toggle(settings),
                    on_release=lambda mode: None)
                monitor.start()
                self._meeting_monitor = monitor
            except Exception:
                self.notify_error("Atalho de gravação indisponível. Revise os atalhos nas configurações.")

    def _load_commands(self):
        path = os.path.join(self.data_dir, "commands.json")
        try:
            with open(path, encoding="utf-8") as handle:
                commands = json.load(handle)
            if not isinstance(commands, dict) or any(
                not isinstance(key, str) or not key.strip() or key.startswith("_")
                or not isinstance(value, str) for key, value in commands.items()
            ):
                raise ValueError("Expected a dictionary of trigger names and literal text")
        except FileNotFoundError:
            commands = {}
        except (OSError, ValueError) as exc:
            self.logger.warning(f"Could not load spoken commands: {type(exc).__name__}")
            self.notify_error("Não foi possível ler commands.json. Os comandos anteriores foram preservados.")
            return False
        self.snippets = commands
        self.trigger_index = compile_trigger_index(commands, set())
        return True

    def reload_commands(self, icon=None, item=None):
        self.task_runner.start(self._load_commands, name="commands-load")

    def expand_from_voice(self, trigger):
        value = self.snippets.get(trigger)
        return bool(isinstance(value, str) and self.text_inserter.insert_text(value))

    def notify_error(self, message, key=None, cooldown_seconds=8):
        # Notifications never include recorded audio or transcript text.
        with self._notification_lock:
            now = time.monotonic()
            if key and now - self._notification_times.get(key, -float("inf")) < cooldown_seconds:
                return
            if key:
                self._notification_times[key] = now
        if self.icon is not None:
            self.icon.notify(message, "Snipvoice")
        else:
            self.logger.warning(message)

    def toggle_voice(self, icon=None, item=None):
        if self.voice is None:
            self.notify_error("Entrada por voz indisponível. Verifique o runtime de transcrição.")
            return
        if self.voice.is_enabled():
            self.task_runner.start(self._disable_voice, name="voice-disable")
        else:
            self.gui.submit(self._confirm_and_enable_voice)

    def _show_manager_window(self, root):
        if self.manager_window is not None and self.manager_window.winfo_exists():
            self.manager_window.deiconify()
            self.manager_window.lift()
            return
        ui = ui_theme.bind(root)
        window = tk.Toplevel(root)
        self.manager_window = window
        window.title(APP_DISPLAY_NAME)
        geometry, min_width, min_height = ui.manager_window_size
        window.geometry(geometry)
        window.minsize(min_width, min_height)
        window.configure(bg=ui.surface)
        self._set_window_icon(window)
        style = ttk.Style(window)
        ui_theme.apply_ttk_theme(style)
        ui_theme.configure_manager_styles(style, ui)

        header = tk.Frame(window, bg=ui.surface, padx=ui.space_xl, pady=ui.space_lg)
        header.pack(fill=tk.X)
        identity = tk.Frame(header, bg=ui.surface)
        identity.pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Label(
            identity,
            text=APP_DISPLAY_NAME,
            font=ui.font(16, "bold"),
            bg=ui.surface,
            fg=ui.text_strong,
        ).pack(anchor="w")
        tk.Label(
            identity,
            text="Ditado, gravações, transcrições e resumos no seu computador",
            font=ui.font(9),
            bg=ui.surface,
            fg=ui.text_muted,
        ).pack(anchor="w", pady=(ui.space_xs, 0))
        privacy = tk.Frame(
            header, padx=ui.space_md, pady=ui.space_sm, **ui.card_options()
        )
        privacy.pack(side=tk.RIGHT, padx=(ui.space_lg, 0))
        tk.Label(
            privacy,
            text="Processamento local",
            font=ui.font(9, "bold"),
            bg=ui.card,
            fg=ui.success,
        ).pack()
        tk.Frame(window, bg=ui.divider, height=1).pack(fill=tk.X)

        notebook = ttk.Notebook(window, style="Manager.TNotebook")
        self._manager_notebook = notebook
        notebook.pack(
            fill=tk.BOTH,
            expand=True,
            padx=ui.space_xl,
            pady=(ui.space_md, ui.space_lg),
        )
        tab = tk.Frame(notebook, bg=ui.surface)
        self._manager_voice_tab = tab
        if self.voice is None:
            notebook.add(tab, text="Diagnóstico")
            tk.Label(tab, text="Entrada por voz indisponível. Verifique o runtime de transcrição.",
                     bg=ui.surface, fg=ui.text, font=ui.font(11), wraplength=640).pack(padx=24, pady=24)
        else:
            notebook.add(tab, text="Voz")
            self._create_voice_tab(tab, window)
        from meeting_gui import add_meeting_tabs
        meeting_view = add_meeting_tabs(
            root, window, notebook, self.meetings,
            lambda: dict(self.settings), self._persist_voice_settings,
            on_settings_changed=lambda: self.task_runner.start(
                self._rebuild_meeting_monitor, name="meeting-hotkey"),
        )
        self._manager_meeting_view = meeting_view
        self._manager_recording_tab = meeting_view.recording_tab
        self._manager_library_tab = meeting_view.library_tab
        window.protocol("WM_DELETE_WINDOW", self._close_settings_window)
        window_width, window_height = (int(value) for value in geometry.split("x"))
        screen_width = window.winfo_screenwidth()
        screen_height = window.winfo_screenheight()
        window_width = min(window_width, screen_width)
        window_height = min(window_height, screen_height)
        x = max(0, (screen_width - window_width) // 2)
        y = max(0, (screen_height - window_height) // 2)
        window.geometry(f"{window_width}x{window_height}+{x}+{y}")

    def _close_settings_window(self, force=False):
        meeting_view = self._manager_meeting_view
        if meeting_view is not None and not meeting_view.closed:
            if force:
                meeting_view.close_without_prompt(destroy=False)
            else:
                meeting_view.close(destroy=False, after_close=self._destroy_manager_window)
                return
        self._destroy_manager_window()

    def _destroy_manager_window(self):
        self._manager_voice_refresher = None
        self._manager_voice_tk_vars = []
        self._manager_voice_tab = None
        self._manager_notebook = None
        self._manager_meeting_view = None
        self._manager_recording_tab = None
        self._manager_library_tab = None
        window, self.manager_window = self.manager_window, None
        if window is not None:
            window.destroy()
        gc.collect()

    def _set_window_icon(self, window):
        if platform_support.IS_WINDOWS:
            path = os.path.join(get_runtime_resource_dir(), "snipvoice.ico")
            try:
                window.iconbitmap(path)
            except tk.TclError:
                self.logger.warning("Could not load the window icon")

    def on_tray_ready(self, icon):
        icon.visible = True
        self.task_runner.start(self._resolve_startup, name="startup-checks")

    def _resolve_startup(self):
        self._autostart_state = platform_support.autostart_state()
        if platform_support.IS_MAC:
            status = macos_permissions.check_permissions()
            if macos_permissions.needs_onboarding(status):
                self.notify_error("Conceda Monitoramento de Entrada e Acessibilidade ao Snipvoice e reinicie.")
        if self.voice.is_enabled():
            self.voice.enable()
        self.refresh_tray_menu()

    def toggle_autostart(self, icon=None, item=None):
        self.task_runner.start(self._toggle_autostart, name="autostart-toggle")

    def _toggle_autostart(self):
        try:
            if self._autostart_state == platform_support.AUTOSTART_CURRENT:
                changed = platform_support.remove_autostart()
            else:
                changed = platform_support.install_autostart()
            if not changed:
                self.notify_error("Não foi possível alterar a inicialização automática do Snipvoice.")
        except OSError:
            self.notify_error("Não foi possível alterar a inicialização automática do Snipvoice.")
        self._autostart_state = platform_support.autostart_state()
        self.refresh_tray_menu()

    def quit_app(self, icon, item):
        if self._quitting.is_set():
            return
        self._quitting.set()
        # Joins never block a Tk/Cocoa callback; shutdown then schedules teardown.
        self.task_runner.start(self._shutdown, name="voice-shutdown")

    def _shutdown(self):
        with self._settings_lock:
            if self._meeting_monitor is not None:
                self._meeting_monitor.stop()
                self._meeting_monitor = None
        self.meetings.shutdown()
        self.voice.shutdown()
        self.gui.submit(lambda root: self._close_settings_window(force=True))
        self.gui.stop()
        if self.icon is not None:
            self.icon.stop()

    def run(self, *, show_settings=False):
        if platform_support.tk_runs_on_main_thread():
            started = self.gui.adopt_main_thread()
        else:
            started = self.gui.ensure_started()
        if not started:
            raise RuntimeError("Could not start the shared GUI root")
        if platform_support.tk_runs_on_main_thread():
            platform_support.hide_dock_icon()
        menu = pystray.Menu(
            pystray.MenuItem("Gravações e reuniões…", self.open_meetings),
            pystray.MenuItem(self._voice_menu_label, self.toggle_voice, checked=self._voice_menu_checked),
            pystray.MenuItem("Configurar voz…", self.open_voice_settings, default=True),
            pystray.MenuItem("Recarregar comandos", self.reload_commands),
            pystray.MenuItem("Iniciar com o sistema", self.toggle_autostart,
                             checked=lambda item: self._autostart_state == platform_support.AUTOSTART_CURRENT),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(APP_DISPLAY_NAME, lambda icon, item: None, enabled=False),
            pystray.MenuItem("Sair", self.quit_app),
        )
        with Image.open(os.path.join(get_runtime_resource_dir(), "snipvoice.ico")) as image:
            tray_image = image.copy()
        self.icon = pystray.Icon("snipvoice", tray_image, APP_DISPLAY_NAME, menu,
                                 **platform_support.tray_icon_options())
        self.task_runner.start(self._rebuild_meeting_monitor, name="meeting-hotkey")
        if show_settings:
            self.open_voice_settings()
        if platform_support.tk_runs_on_main_thread():
            self.icon.run_detached(setup=self.on_tray_ready)
            self.gui.run_mainloop()
        else:
            self.icon.run(setup=self.on_tray_ready)

    def _capture_voice_target(self):
        if VoiceTarget is None:
            return None
        handle = platform_support.capture_text_target()
        if handle is None:
            return VoiceTarget("self")
        return VoiceTarget("window", handle)


    def _restore_voice_target(self, target):
        if target is None:
            return False
        if target.kind == "form":
            return True
        if target.kind != "window":
            return False
        return platform_support.restore_text_target(target.handle)


    def _insert_voice_text(self, text):
        return bool(self.text_inserter.insert_text(text))


    def _persist_voice_settings(self, payload):
        with self._settings_lock:
            current = load_settings(self.settings_file)
            if not isinstance(current, dict):
                current = {}
            current.update(payload)
            validate_hotkey_conflicts(current)
            if save_settings(self.settings_file, current):
                self.settings.update(payload)
                return True
            return False


    def _voice_menu_label(self, _text=None):
        if self.voice is None:
            return "Entrada por voz"
        return self.voice.status_label()


    def _voice_status_changed(self):
        """Refresh voice UI without touching Tk from a voice worker."""
        voice = self.voice
        if voice is None:
            return
        snapshot = voice.status_snapshot()
        try:
            self.gui.submit(
                lambda root, value=snapshot: self._render_voice_status(root, value)
            )
        except Exception as exc:
            self.logger.warning(f"Não foi possível atualizar o indicador de voz: {exc}")
        if snapshot["state"] not in {"recording", "transcribing", "routing"}:
            self.refresh_tray_menu()


    def _render_voice_status(self, root, snapshot):
        if self.voice_status_indicator is None:
            self.voice_status_indicator = VoiceStatusIndicator(root)
        self.voice_status_indicator.update(snapshot["state"], snapshot["mode"])
        self._refresh_manager_voice_tab()


    def _refresh_manager_voice_tab(self):
        """Update manager voice widgets. GUI thread only; no-op after close."""
        refresher = self._manager_voice_refresher
        if refresher is None:
            return
        try:
            refresher()
        except Exception as exc:
            self.logger.warning(f"Falha ao atualizar a aba de voz: {exc}")


    def _voice_menu_checked(self, _item=None):
        return bool(self.voice is not None and self.voice.is_enabled())


    def _voice_menu_visible(self, _item=None):
        return self.voice is not None


    def _disable_voice(self):
        """Join capture workers off the Tk/Cocoa callback that requested disable."""
        if self.voice is None:
            return
        self.voice.disable()
        self.refresh_tray_menu()


    def _confirm_and_enable_voice(self, _root=None):
        from voice_catalog import catalog_entry, format_size

        if macos_permissions.check_microphone() == macos_permissions.DENIED:
            self.notify_error(
                "O macOS bloqueou o microfone. Conceda Microfone em Privacidade "
                "e reinicie o Snipvoice.",
                key="voice-mic",
            )
            macos_permissions.open_settings_pane(macos_permissions.MICROPHONE)
            self._refresh_manager_voice_tab()
            return
        entry = catalog_entry(self.voice.settings.profile)
        if entry is not None and not self.voice.model_installed():
            size = format_size(entry["size_bytes"])
            message = (
                f"Baixar o modelo {entry['id']} ({size})?\n\n"
                f"{entry['purpose']}\n\n"
                f"Licença: {entry['license_id']}\n"
                f"{entry['attribution']}\n\n"
                "O arquivo fica num cache local, não na pasta de snippets."
            )
            if not messagebox.askyesno("Entrada por voz", message):
                self._refresh_manager_voice_tab()
                return
        self.voice.enable()
        self.refresh_tray_menu()
        self._refresh_manager_voice_tab()


    def open_voice_settings(self, icon=None, item=None):
        """Open the voice profile/license controls on the GUI thread."""
        try:
            self.gui.submit(self._show_voice_settings)
        except Exception as exc:
            self.logger.error(f"Erro ao abrir configurações de voz: {exc}")
            self.notify_error(
                f"Erro ao abrir configurações de voz: {exc}",
                key="voice-settings-open",
                cooldown_seconds=5,
            )


    def _show_voice_settings(self, root):
        """Open the manager on the voice tab. Tray shortcut; no extra dialog."""
        self._show_manager_window(root)
        notebook = self._manager_notebook
        tab = self._manager_voice_tab
        if notebook is None or tab is None:
            return
        try:
            notebook.select(tab)
        except tk.TclError:
            pass


    def _show_voice_third_party_notices(self, owner, notices):
        """Show model licenses in a bounded, scrollable child window."""
        ui = ui_theme.theme()
        dialog = tk.Toplevel(owner)
        dialog.title("Licenças e atribuições")
        dialog.geometry("700x420")
        dialog.minsize(520, 300)
        dialog.configure(bg=ui.surface)
        dialog.transient(owner)
        self._set_window_icon(dialog)

        container = tk.Frame(dialog, bg=ui.surface, padx=16, pady=16)
        container.pack(fill=tk.BOTH, expand=True)
        container.grid_columnconfigure(0, weight=1)
        container.grid_rowconfigure(1, weight=1)

        tk.Label(
            container,
            text="Licenças e atribuições",
            font=ui.font(11, "bold"),
            bg=ui.surface,
            fg=ui.text,
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        notice_text = tk.Text(
            container,
            wrap=tk.WORD,
            font=ui.font(9),
            relief=tk.FLAT,
            highlightthickness=1,
            highlightbackground=ui.border,
            padx=10,
            pady=8,
            **ui.text_colors(),
        )
        scrollbar = ttk.Scrollbar(
            container,
            orient=tk.VERTICAL,
            command=notice_text.yview,
        )
        notice_text.configure(yscrollcommand=scrollbar.set)
        notice_text.grid(row=1, column=0, sticky="nsew")
        scrollbar.grid(row=1, column=1, sticky="ns")
        notice_text.insert("1.0", "\n\n".join(notices))
        notice_text.configure(state=tk.DISABLED)

        tk.Button(
            container,
            text="Fechar",
            width=ui.button_width(10),
            command=dialog.destroy,
            **ui.button_colors(),
        ).grid(row=2, column=0, columnspan=2, sticky="e", pady=(12, 0))

        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        center_on_screen(dialog)
        dialog.lift()
        dialog.focus_force()
        return dialog


    def _show_voice_replacements(self, owner):
        """Edit optional transcript corrections without creating another Tk root."""
        from voice_text_support import validate_replacements

        ui = ui_theme.theme()
        dialog = tk.Toplevel(owner)
        dialog.title("Correções da transcrição")
        dialog.transient(owner)
        dialog.resizable(True, True)
        dialog.minsize(480, 300)
        container = tk.Frame(dialog, bg=ui.surface, padx=16, pady=16)
        container.pack(fill=tk.BOTH, expand=True)
        tk.Label(
            container,
            text="Corrija termos recorrentes reconhecidos incorretamente.",
            bg=ui.surface,
            fg=ui.text,
            font=ui.font(9),
            wraplength=560,
            justify="left",
        ).pack(anchor="w", pady=(0, 10))
        list_frame = tk.Frame(container, bg=ui.surface)
        list_frame.pack(fill=tk.BOTH, expand=True)
        listbox = tk.Listbox(
            list_frame,
            height=8,
            font=ui.mono_font(9),
            **ui.listbox_colors(),
        )
        scrollbar = tk.Scrollbar(list_frame, orient=tk.VERTICAL, command=listbox.yview)
        listbox.configure(yscrollcommand=scrollbar.set)
        listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        replacements = dict(
            getattr(self.voice.settings, "voice_replacements", {}) or {}
        )

        def redraw():
            listbox.delete(0, tk.END)
            for source, replacement in replacements.items():
                listbox.insert(tk.END, f"{source}  →  {replacement}")

        def edit_selected():
            selection = listbox.curselection()
            if not selection:
                return
            source = list(replacements)[selection[0]]
            replacement = replacements[source]
            new_value = simpledialog.askstring(
                "Substituição", f"Texto para substituir: {source}",
                initialvalue=replacement, parent=dialog,
            )
            if new_value is None:
                return
            checked = dict(replacements)
            checked[source] = new_value
            if not validate_replacements(checked):
                messagebox.showerror(
                    "Correção inválida",
                    "A substituição não pode ser vazia ou muito longa.",
                    parent=dialog,
                )
                return
            replacements[source] = new_value
            redraw()

        def add_entry():
            source = simpledialog.askstring(
                "Nova correção",
                "Texto reconhecido incorretamente:",
                parent=dialog,
            )
            if source is None:
                return
            replacement = simpledialog.askstring(
                "Nova correção",
                "Substituir por:",
                parent=dialog,
            )
            if replacement is None:
                return
            checked = dict(replacements)
            checked[source] = replacement
            if not validate_replacements(checked):
                messagebox.showerror(
                    "Correção inválida",
                    "O texto não pode ser vazio ou muito longo.",
                    parent=dialog,
                )
                return
            replacements.clear()
            replacements.update(checked)
            redraw()

        def remove_entry():
            selection = listbox.curselection()
            if selection:
                replacements.pop(list(replacements)[selection[0]], None)
                redraw()

        actions = tk.Frame(container, bg=ui.surface)
        actions.pack(fill=tk.X, pady=(10, 0))
        for label, command in (
            ("Adicionar", add_entry),
            ("Editar", edit_selected),
            ("Remover", remove_entry),
        ):
            tk.Button(
                actions,
                text=label,
                command=command,
                **ui.button_colors(),
            ).pack(side=tk.LEFT, padx=(0, 6))

        def save_and_close():
            checked = validate_replacements(replacements)
            if checked != replacements:
                messagebox.showerror(
                    "Correções inválidas",
                    "Revise os termos informados.",
                    parent=dialog,
                )
                return
            if not self._persist_voice_settings({"voice_replacements": checked}):
                messagebox.showerror(
                    "Falha ao salvar",
                    "Não foi possível salvar as correções da transcrição.",
                    parent=dialog,
                )
                return
            self.voice.settings.voice_replacements = checked
            dialog.destroy()

        tk.Button(
            actions,
            text="Salvar",
            command=save_and_close,
            **ui.button_colors(accent=True),
        ).pack(side=tk.RIGHT)
        tk.Button(
            actions,
            text="Cancelar",
            command=dialog.destroy,
            **ui.button_colors(),
        ).pack(side=tk.RIGHT, padx=(0, 6))
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        redraw()
        center_on_screen(dialog)
        dialog.lift()
        dialog.focus_force()
        return dialog


    def _build_voice_settings_controls(self, parent, owner):
        """Build profile, language, shortcut, and model controls."""
        from voice_catalog import (
            LANGUAGES,
            available_languages,
            default_language_for_profile,
            format_size,
            selectable_catalog,
            third_party_notices,
        )
        from voice_hotkey import parse_chord
        import voice_models

        ui = ui_theme.theme()
        wrap = 640
        visible = selectable_catalog()
        selected = tk.StringVar(master=owner, value=self.voice.settings.profile)
        language = tk.StringVar(master=owner, value=self.voice.settings.language)
        hotkey = tk.StringVar(master=owner, value=self.voice.settings.hotkey)
        command_hotkey = tk.StringVar(
            master=owner, value=self.voice.settings.command_hotkey
        )
        self._manager_voice_tk_vars = [
            selected,
            language,
            hotkey,
            command_hotkey,
        ]
        profile_buttons = []
        download_buttons = []

        models_card = tk.Frame(
            parent,
            padx=ui.space_lg,
            pady=ui.space_md,
            **ui.card_options(),
        )
        models_card.pack(fill=tk.X, pady=(0, ui.space_md))
        tk.Label(
            models_card,
            text="Modelo e idioma",
            font=ui.font(11, "bold"),
            bg=ui.card,
            fg=ui.text_strong,
        ).pack(anchor="w")
        tk.Label(
            models_card,
            text="Baixe somente os modelos que quiser usar. O áudio permanece neste computador.",
            font=ui.font(9),
            bg=ui.card,
            fg=ui.text_muted,
            wraplength=wrap,
            justify="left",
        ).pack(anchor="w", pady=(ui.space_xs, ui.space_sm))

        def profile_label(entry):
            installed = voice_models.model_is_installed(entry, self.voice.cache_dir)
            status = "instalado" if installed else "não baixado"
            return (
                f"{entry['purpose']}\n"
                f"Download {format_size(entry['size_bytes'])} · "
                f"{entry['license_id']} · {status}"
            )

        def refresh_profile_labels():
            for button, entry in profile_buttons:
                try:
                    if not button.winfo_exists():
                        continue
                except tk.TclError:
                    continue
                button.configure(text=profile_label(entry))
            for button, entry in download_buttons:
                try:
                    if not button.winfo_exists():
                        continue
                except tk.TclError:
                    continue
                installed = voice_models.model_is_installed(
                    entry, self.voice.cache_dir
                )
                downloading = self.voice.model_download_in_progress(
                    entry["profile"]
                )
                if installed:
                    button.configure(text="Baixado", state=tk.DISABLED)
                elif downloading:
                    button.configure(text="Baixando…", state=tk.DISABLED)
                else:
                    button.configure(text="Baixar", state=tk.NORMAL)

        for entry in visible:
            row = tk.Frame(models_card, bg=ui.card)
            row.pack(fill=tk.X, anchor="w", pady=ui.space_xs)
            button = tk.Radiobutton(
                row,
                text=profile_label(entry),
                variable=selected,
                value=entry["profile"],
                anchor="w",
                justify="left",
                wraplength=wrap - 110,
                **ui.checkbutton_colors(ui.card),
            )
            button.pack(side=tk.LEFT, fill=tk.X, expand=True, anchor="w")
            profile_buttons.append((button, entry))
            download_button = tk.Button(
                row,
                text="Baixar",
                width=ui.button_width(10),
                command=lambda item=entry: download_model(item),
                **ui.button_colors(),
            )
            download_button.pack(side=tk.RIGHT, padx=(8, 0))
            download_buttons.append((download_button, entry))

        lang_row = tk.Frame(models_card, bg=ui.card)
        lang_row.pack(anchor="w", pady=(ui.space_sm, 0))
        language_label = tk.Label(
            lang_row,
            text="Idioma:",
            bg=ui.card,
            fg=ui.text,
            font=ui.font(9),
        )
        language_label.pack(side=tk.LEFT)
        language_labels = {
            "auto": "detecção automática",
            "pt-BR": "pt-BR",
            "en-US": "en-US",
        }
        language_buttons = {}
        for lang in LANGUAGES:
            button = tk.Radiobutton(
                lang_row,
                text=language_labels.get(lang, lang),
                variable=language,
                value=lang,
                **ui.checkbutton_colors(ui.card),
            )
            button.pack(side=tk.LEFT, padx=4)
            language_buttons[lang] = button

        def update_language_options(*_args):
            profile = selected.get()
            allowed = set(available_languages(profile))
            if language.get() not in allowed:
                language.set(default_language_for_profile(profile, language.get()))
            for lang, button in language_buttons.items():
                button.configure(
                    state=tk.NORMAL if lang in allowed else tk.DISABLED
                )
            if allowed == {"auto"}:
                language_label.configure(text="Idioma: detecção automática (Qwen)")
            else:
                language_label.configure(text="Idioma:")

        for button, _entry in profile_buttons:
            button.configure(command=update_language_options)
        selected.trace_add("write", update_language_options)

        shortcut_frame = tk.Frame(
            parent,
            padx=ui.space_lg,
            pady=ui.space_md,
            **ui.card_options(),
        )
        shortcut_frame.pack(fill=tk.X, pady=(0, ui.space_md))
        shortcut_frame.grid_columnconfigure(1, weight=1)
        tk.Label(
            shortcut_frame,
            text="Atalhos",
            bg=ui.card,
            fg=ui.text_strong,
            font=ui.font(11, "bold"),
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, ui.space_sm))
        tk.Label(
            shortcut_frame,
            text="Ditado (segure para falar):",
            bg=ui.card,
            fg=ui.text,
            font=ui.font(9),
        ).grid(row=1, column=0, sticky="w", padx=(0, 12), pady=4)
        tk.Entry(
            shortcut_frame,
            textvariable=hotkey,
            font=ui.font(9),
            width=28,
            **ui.entry_colors(),
        ).grid(row=1, column=1, sticky="ew", pady=4)
        tk.Label(
            shortcut_frame,
            text="Comando por voz:",
            bg=ui.card,
            fg=ui.text,
            font=ui.font(9),
        ).grid(row=2, column=0, sticky="w", padx=(0, 12), pady=4)
        tk.Entry(
            shortcut_frame,
            textvariable=command_hotkey,
            font=ui.font(9),
            width=28,
            **ui.entry_colors(),
        ).grid(row=2, column=1, sticky="ew", pady=4)
        tk.Label(
            shortcut_frame,
            text="Formato: ctrl+alt+space, ctrl+shift+f8, etc.",
            bg=ui.card,
            fg=ui.text_muted,
            font=ui.font(8),
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))

        def refresh_form():
            refresh_profile_labels()
            voice = self.voice
            if voice is None:
                return
            selected.set(voice.settings.profile)
            language.set(voice.settings.language)
            hotkey.set(voice.settings.hotkey)
            command_hotkey.set(voice.settings.command_hotkey)
            update_language_options()

        def apply_voice_settings():
            profile = selected.get()
            try:
                dictation_chord = parse_chord(hotkey.get())
            except ValueError:
                messagebox.showerror(
                    "Atalho inválido",
                    "O atalho de ditado precisa de um modificador e uma tecla.\n\n"
                    "Exemplo: ctrl+alt+space",
                    parent=owner,
                )
                return
            try:
                command_chord = parse_chord(command_hotkey.get())
            except ValueError:
                messagebox.showerror(
                    "Atalho inválido",
                    "O atalho de comando precisa de um modificador e uma tecla.\n\n"
                    "Exemplo: ctrl+alt+shift+space",
                    parent=owner,
                )
                return
            if command_chord == dictation_chord:
                messagebox.showerror(
                    "Atalhos em conflito",
                    "Escolha atalhos diferentes para ditado e comando por voz.",
                    parent=owner,
                )
                return
            entry = next(
                (item for item in visible if item["profile"] == profile),
                None,
            )
            if entry is not None and not voice_models.model_is_installed(
                entry, self.voice.cache_dir
            ):
                warning = (
                    f"Isso vai baixar {format_size(entry['size_bytes'])} "
                    f"({entry['license_id']}).\n\n{entry['attribution']}"
                )
                if not messagebox.askokcancel(
                    "Baixar modelo de voz", warning, parent=owner
                ):
                    return
            with self._settings_lock:
                candidate = dict(self.settings, voice_hotkey=dictation_chord.spec,
                                 voice_command_hotkey=command_chord.spec)
                try:
                    validate_hotkey_conflicts(candidate)
                except ValueError as exc:
                    messagebox.showerror("Atalhos em conflito", str(exc), parent=owner)
                    return
                self.voice.apply_options(
                    profile=profile,
                    language=language.get(),
                    hotkey=dictation_chord.spec,
                    command_hotkey=command_chord.spec,
                )
            if not self.voice.is_enabled():
                self.voice.enable()
            self.refresh_tray_menu()
            self._refresh_manager_voice_tab()

        def download_model(entry):
            if voice_models.model_is_installed(entry, self.voice.cache_dir):
                refresh_profile_labels()
                return
            warning = (
                f"Baixar {entry['purpose']} "
                f"({format_size(entry['size_bytes'])})?\n\n"
                f"Licença: {entry['license_id']}\n{entry['attribution']}\n\n"
                "O arquivo fica no cache local de modelos. A entrada por voz "
                "não será ativada automaticamente."
            )
            if not messagebox.askokcancel(
                "Baixar modelo de voz", warning, parent=owner
            ):
                return
            if not self.voice.download_profile(entry["profile"]):
                refresh_profile_labels()
                return
            refresh_profile_labels()

        def remove_model():
            if not messagebox.askokcancel(
                "Remover modelo",
                "A entrada por voz será desligada e só o modelo deste perfil será apagado.",
                parent=owner,
            ):
                return
            self.voice.delete_active_model()
            self.refresh_tray_menu()
            self._refresh_manager_voice_tab()

        buttons = tk.Frame(parent, bg=ui.surface)
        buttons.pack(fill=tk.X)
        tk.Button(
            buttons,
            text="Salvar e usar",
            command=apply_voice_settings,
            **ui.button_colors(accent=True),
        ).pack(side=tk.LEFT)
        tk.Button(
            buttons,
            text="Remover modelo",
            command=remove_model,
            **ui.button_colors(),
        ).pack(side=tk.LEFT, padx=(8, 0))
        tk.Button(
            buttons,
            text="Licenças e atribuições…",
            command=lambda: self._show_voice_third_party_notices(
                owner, third_party_notices()
            ),
            **ui.button_colors(),
        ).pack(side=tk.RIGHT)
        tk.Button(
            buttons,
            text="Histórico de voz…",
            command=lambda: self._open_voice_history(owner),
            **ui.button_colors(),
        ).pack(side=tk.RIGHT, padx=(0, 8))
        tk.Button(
            buttons,
            text="Correções…",
            command=lambda: self._show_voice_replacements(owner),
            **ui.button_colors(),
        ).pack(side=tk.RIGHT, padx=(0, 8))

        refresh_form()
        return refresh_form


    def _create_voice_tab(self, parent, root):
        """Build manager voice controls backed by the existing tray actions."""
        ui = ui_theme.theme()
        main = tk.Frame(parent, bg=ui.surface, padx=ui.space_lg, pady=ui.space_lg)
        main.pack(fill=tk.BOTH, expand=True)

        tk.Label(
            main,
            text="Entrada por voz",
            font=ui.font(16, "bold"),
            bg=ui.surface,
            fg=ui.text_strong,
        ).pack(anchor="w")
        tk.Label(
            main,
            text="Ative a entrada por voz e escolha o modelo, o idioma e os atalhos.",
            font=ui.font(9),
            bg=ui.surface,
            fg=ui.text_muted,
            wraplength=640,
            justify="left",
        ).pack(anchor="w", pady=(ui.space_xs, ui.space_lg))

        enabled = bool(self.voice is not None and self.voice.is_enabled())
        status_text = (
            self.voice.status_label() if self.voice is not None else "Entrada por voz"
        )

        def refresh():
            voice = self.voice
            if voice is None:
                return
            try:
                if not checkbox.winfo_exists() or not status_label.winfo_exists():
                    return
            except tk.TclError:
                return
            if voice.is_enabled():
                checkbox.select()
            else:
                checkbox.deselect()
            status_label.configure(text=voice.status_label())
            refresh_form()

        def on_toggle():
            was_enabled = bool(self.voice is not None and self.voice.is_enabled())
            self.toggle_voice()
            # Disable is asynchronous; controller callbacks own that state.
            # A cancelled enable still needs an immediate checkbox reset.
            if not was_enabled:
                refresh()

        status_card = tk.Frame(
            main,
            padx=ui.space_lg,
            pady=ui.space_md,
            **ui.card_options(),
        )
        status_card.pack(fill=tk.X, pady=(0, ui.space_md))
        checkbox = tk.Checkbutton(
            status_card,
            text="Ativar entrada por voz",
            command=on_toggle,
            font=ui.font(10, "bold"),
            **ui.checkbutton_colors(ui.card),
        )
        checkbox.pack(anchor="w")
        if enabled:
            checkbox.select()
        else:
            checkbox.deselect()

        status_label = tk.Label(
            status_card,
            text=status_text,
            font=ui.font(9),
            bg=ui.card,
            fg=ui.text_muted,
            wraplength=640,
            justify="left",
        )
        status_label.pack(anchor="w", pady=(ui.space_xs, 0))

        refresh_form = self._build_voice_settings_controls(main, root)
        self._manager_voice_refresher = refresh
        refresh()


    def _open_voice_history(self, root):
        """Show recoverable recordings without replaying them into stale targets."""
        ui = ui_theme.theme()
        history_window = tk.Toplevel(root)
        history_window.title("Histórico de Voz")
        history_window.geometry("760x400")
        history_window.minsize(620, 300)
        history_window.configure(bg=ui.surface)
        history_window.transient(root)
        self._set_window_icon(history_window)

        outer = tk.Frame(history_window, bg=ui.surface, padx=14, pady=14)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.grid_columnconfigure(0, weight=1)
        outer.grid_rowconfigure(1, weight=1)

        tk.Label(
            outer,
            text="Gravações recuperáveis",
            font=ui.font(11, "bold"),
            bg=ui.surface,
            fg=ui.text,
        ).grid(row=0, column=0, sticky="w")

        frame = tk.Frame(
            outer,
            bg=ui.card,
            highlightbackground=ui.border,
            highlightthickness=1,
        )
        frame.grid(row=1, column=0, sticky="nsew", pady=(10, 8))
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(0, weight=1)

        columns = ("time", "status", "provider", "transcript")
        tree = ttk.Treeview(frame, columns=columns, show="headings", height=12)
        tree.heading("time", text="Data")
        tree.heading("status", text="Estado")
        tree.heading("provider", text="Provedor")
        tree.heading("transcript", text="Transcrição / erro")
        tree.column("time", width=145, anchor="center", stretch=False)
        tree.column("status", width=95, anchor="center", stretch=False)
        tree.column("provider", width=80, anchor="center", stretch=False)
        tree.column("transcript", width=400, anchor="w")

        scrollbar = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        def refresh_rows():
            tree.delete(*tree.get_children())
            voice = self.voice
            entries = voice.history_entries() if voice is not None else []
            if not entries:
                tree.insert("", tk.END, values=("—", "vazio", "—", "Nenhuma gravação."))
                return
            for entry in entries:
                summary = entry.get("transcript") or entry.get("error") or ""
                tree.insert(
                    "",
                    tk.END,
                    iid=entry["id"],
                    values=(
                        entry.get("created_at", "—"),
                        entry.get("status", "—"),
                        entry.get("provider", "—"),
                        summary,
                    ),
                )

        def selected_id():
            selection = tree.selection()
            return selection[0] if selection and self.voice is not None else None

        def retry_selected():
            record_id = selected_id()
            if record_id and self.voice.retry_history(record_id):
                history_window.after(500, refresh_rows)

        def copy_selected():
            record_id = selected_id()
            if record_id and not self.voice.copy_history_transcript(record_id):
                self.notify_error(
                    "Esta gravação ainda não tem uma transcrição para copiar.",
                    key="voice-history-copy",
                )

        actions = tk.Frame(outer, bg=ui.surface)
        actions.grid(row=2, column=0, sticky="w")
        tk.Button(
            actions,
            text="Tentar novamente",
            command=retry_selected,
            **ui.button_colors(accent=True),
        ).pack(side=tk.LEFT)
        tk.Button(
            actions,
            text="Copiar transcrição",
            command=copy_selected,
            **ui.button_colors(),
        ).pack(side=tk.LEFT, padx=(8, 0))

        refresh_rows()
        self._bind_mousewheel(tree, tree)
        center_dialog(history_window, root)


    def _bind_mousewheel(self, widget, target=None):
        scroll_target = target or widget

        def on_mousewheel(event):
            if getattr(event, "delta", 0):
                steps = -1 if event.delta > 0 else 1
            elif getattr(event, "num", None) == 4:
                steps = -1
            elif getattr(event, "num", None) == 5:
                steps = 1
            else:
                return None
            scroll_target.yview_scroll(steps, "units")
            return "break"

        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            widget.bind(sequence, on_mousewheel, add="+")


    def refresh_tray_menu(self):
        """Re-render the tray menu so check states reflect a background change.

        Callers are worker threads (autostart/permission resolves). On macOS
        pystray's ``update_menu`` mutates AppKit (NSMenu/``setMenu_``) on the
        calling thread, and AppKit may only be touched from the main thread —
        so the update is routed through the GuiThread pump, which runs it on the
        main thread from inside the Tk loop. That is AppKit-from-Tk-pump, the
        opposite of the forbidden Tcl-from-Cocoa direction (issue #53), so it is
        safe. ``submit`` queues rather than running inline even if the caller is
        already the GUI thread, so this never deadlocks. Windows/Linux post an
        internal message and call it directly, unchanged.
        """
        if not self.icon:
            return
        if platform_support.tray_menu_updates_on_gui_thread():
            self.gui.submit(self._update_tray_menu)
            return
        self._update_tray_menu()


    def _update_tray_menu(self, _root=None):
        """Rebuild the tray menu. ``_root`` is the arg the GuiThread pump passes."""
        try:
            self.icon.update_menu()
        except Exception as e:
            self.logger.warning(f"Falha ao atualizar o menu da bandeja: {e}")


def main():
    lock_path = None
    if platform_support.IS_WINDOWS:
        acquired = acquire_single_instance_mutex()
    else:
        lock_path = os.path.join(ensure_data_dir(), "snipvoice.lock")
        acquired = platform_support.acquire_lockfile(lock_path)
    if not acquired:
        return
    try:
        Snipvoice().run(show_settings="--show-settings" in sys.argv[1:])
    finally:
        if lock_path is not None:
            platform_support.release_lockfile(lock_path)


if __name__ == "__main__":
    main()
