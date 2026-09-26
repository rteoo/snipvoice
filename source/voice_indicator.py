"""Non-activating on-screen status for push-to-talk voice input."""

import ctypes
import tkinter as tk

from i18n import tr
import ui_theme
from macos_voice_overlay import MacVoiceStatusPanel
from platform_support import current_os


VISIBLE_STATES = frozenset({"recording", "transcribing", "routing"})

_OVERLAY_BG = "#18191C"
_OVERLAY_BORDER = "#34363C"
_OVERLAY_TEXT = "#F7F7F8"
_OVERLAY_MUTED = "#A7A9AF"
_WAVE_HEIGHTS = (
    (6, 12, 20, 12, 7),
    (10, 20, 12, 18, 9),
    (16, 9, 22, 13, 17),
    (8, 17, 11, 21, 12),
)

_GWL_EXSTYLE = -20
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_NOACTIVATE = 0x08000000
_HWND_TOPMOST = -1
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOACTIVATE = 0x0010
_SW_SHOWNOACTIVATE = 4


def _windows_user32():
    """Bind the pointer-sized Win32 signatures used by the overlay."""
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.GetParent.argtypes = [wintypes.HWND]
    user32.GetParent.restype = wintypes.HWND
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = ctypes.c_long
    user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
    user32.SetWindowLongW.restype = ctypes.c_long
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    user32.SetWindowPos.restype = wintypes.BOOL
    return user32


def indicator_content(state, mode=None):
    """Return the visible copy and accent token for a voice state."""
    if state == "recording":
        if mode == "command":
            return tr("Ouvindo comando"), "warning"
        return tr("Ouvindo"), "warning"
    if state == "transcribing":
        return tr("Transcrevendo"), "accent"
    if state == "routing":
        return tr("Inserindo texto"), "success"
    return None


def indicator_subtitle(state, mode=None):
    """Return concise guidance without making the overlay interactive."""
    if state == "recording" and mode == "command":
        return tr("Solte para executar · Esc cancela")
    if state == "recording":
        return tr("Solte para transcrever · Esc cancela")
    if state == "transcribing":
        return tr("Processando localmente")
    if state == "routing":
        return tr("Enviando para o campo ativo")
    return ""


class VoiceStatusIndicator:
    """Small bottom-center overlay owned by the shared Tk root."""

    def __init__(self, root):
        self.root = root
        self.window = None
        self.title_label = None
        self.subtitle_label = None
        self.waveform = None
        self.waveform_bars = []
        self._native_hwnd = None
        self._mac_panel = None
        self._animation_after_id = None
        self._animation_frame = 0
        self._state = "idle"

    def update(self, state, mode=None):
        content = indicator_content(state, mode)
        if content is None:
            self.hide()
            return
        title, accent_name = content
        if current_os() == "darwin":
            if self._mac_panel is None:
                self._mac_panel = MacVoiceStatusPanel()
            self._mac_panel.update(title, accent_name)
            return
        if self.window is None or not self._window_exists():
            self._build()
        ui = ui_theme.theme()
        accent = getattr(ui, accent_name)
        self.title_label.configure(text=title)
        self.subtitle_label.configure(text=indicator_subtitle(state, mode))
        for item in self.waveform_bars:
            self.waveform.itemconfigure(item, fill=accent)
        self._state = state
        self._start_animation()
        self._show_without_activation()

    def hide(self):
        if self._mac_panel is not None:
            self._mac_panel.hide()
            return
        self._stop_animation()
        self._state = "idle"
        if self.window is not None and self._window_exists():
            self.window.withdraw()

    def destroy(self):
        if self._mac_panel is not None:
            self._mac_panel.destroy()
            self._mac_panel = None
        self._stop_animation()
        if self.window is not None and self._window_exists():
            self.window.destroy()
        self.window = None
        self._native_hwnd = None

    def _window_exists(self):
        try:
            return bool(self.window.winfo_exists())
        except Exception:
            return False

    def _build(self):
        ui = ui_theme.theme()
        window = tk.Toplevel(self.root)
        self.window = window
        window.withdraw()
        window.overrideredirect(True)
        window.attributes("-topmost", True)
        try:
            window.attributes("-alpha", 0.96)
        except tk.TclError:
            pass
        window.configure(bg=_OVERLAY_BG)

        width = 286
        height = 58
        x = max(12, (window.winfo_screenwidth() - width) // 2)
        y = max(12, window.winfo_screenheight() - height - 76)
        window.geometry(f"{width}x{height}+{x}+{y}")

        container = tk.Frame(
            window,
            bg=_OVERLAY_BG,
            padx=14,
            pady=8,
            highlightbackground=_OVERLAY_BORDER,
            highlightthickness=1,
            bd=0,
        )
        container.pack(fill=tk.BOTH, expand=True)

        self.waveform = tk.Canvas(
            container,
            width=48,
            height=30,
            bg=_OVERLAY_BG,
            highlightthickness=0,
            bd=0,
        )
        self.waveform.pack(side=tk.LEFT, padx=(0, 12))
        self.waveform_bars = []
        for index, height_value in enumerate(_WAVE_HEIGHTS[0]):
            x_position = 6 + index * 9
            item = self.waveform.create_line(
                x_position,
                15 - height_value / 2,
                x_position,
                15 + height_value / 2,
                fill=ui.warning,
                width=3,
                capstyle=tk.ROUND,
            )
            self.waveform_bars.append(item)

        copy = tk.Frame(container, bg=_OVERLAY_BG)
        copy.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.title_label = tk.Label(
            copy,
            text="",
            bg=_OVERLAY_BG,
            fg=_OVERLAY_TEXT,
            font=ui.font(10, "bold"),
            anchor="w",
        )
        self.title_label.pack(fill=tk.X, anchor="w")
        self.subtitle_label = tk.Label(
            copy,
            text="",
            bg=_OVERLAY_BG,
            fg=_OVERLAY_MUTED,
            font=ui.font(8),
            anchor="w",
        )
        self.subtitle_label.pack(fill=tk.X, anchor="w", pady=(1, 0))

        if current_os() == "windows":
            try:
                window.update_idletasks()
                user32 = _windows_user32()
                widget_hwnd = window.winfo_id()
                hwnd = user32.GetParent(widget_hwnd) or widget_hwnd
                style = user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
                user32.SetWindowLongW(
                    hwnd,
                    _GWL_EXSTYLE,
                    style | _WS_EX_TOOLWINDOW | _WS_EX_NOACTIVATE,
                )
                self._native_hwnd = hwnd
                rounded = ctypes.c_int(2)
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, 33, ctypes.byref(rounded), ctypes.sizeof(rounded),
                )
            except Exception:
                self._native_hwnd = None

    def _start_animation(self):
        if self._animation_after_id is None and self.window is not None:
            self._animation_after_id = self.window.after(110, self._animate_waveform)

    def _stop_animation(self):
        if self._animation_after_id is None or self.window is None:
            self._animation_after_id = None
            return
        try:
            self.window.after_cancel(self._animation_after_id)
        except tk.TclError:
            pass
        self._animation_after_id = None

    def _animate_waveform(self):
        self._animation_after_id = None
        if self._state not in VISIBLE_STATES or not self._window_exists():
            return
        self._animation_frame = (self._animation_frame + 1) % len(_WAVE_HEIGHTS)
        heights = _WAVE_HEIGHTS[self._animation_frame]
        if self._state != "recording":
            heights = tuple(max(5, value // 2) for value in heights)
        for index, (item, height_value) in enumerate(zip(self.waveform_bars, heights)):
            x_position = 6 + index * 9
            self.waveform.coords(
                item,
                x_position,
                15 - height_value / 2,
                x_position,
                15 + height_value / 2,
            )
        self._start_animation()

    def _show_without_activation(self):
        """Reveal without moving keyboard focus away from the dictation target."""
        if current_os() != "windows":
            self.window.deiconify()
            return
        try:
            user32 = _windows_user32()
            hwnd = self._native_hwnd
            if not hwnd:
                raise RuntimeError("native overlay handle unavailable")
            self.window.deiconify()
            user32.ShowWindow(hwnd, _SW_SHOWNOACTIVATE)
            user32.SetWindowPos(
                hwnd,
                ctypes.c_void_p(_HWND_TOPMOST),
                0,
                0,
                0,
                0,
                _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE,
            )
        except Exception:
            self.window.deiconify()
