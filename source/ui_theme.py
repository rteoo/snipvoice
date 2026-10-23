"""Per-OS palette, appearance preference and font resolution for Tk windows.

The manager GUI was written Windows-first with a hardcoded light palette and
``Segoe UI`` everywhere. On macOS that produced unreadable windows: Aqua themes
the widgets the app leaves uncolored according to the *system* appearance, so a
dark-mode ``tk.Entry`` renders black-on-black inside a light frame, and
``Segoe UI`` silently substitutes to a different font with different metrics.

This module is the seam. Call sites ask for a semantic token
(``theme().surface``, ``theme().text``), a type role (``theme().body()``) or a
themed control (:func:`button`) instead of naming a color or a font family:

* **Windows** resolves to the Teô Design System 2.7.1 Windows family (Ink &
  Copper on the shared neutral surfaces), Dark by default, with the 12/14/18/28
  pixel type ramp and Compact 32px controls.
* **macOS** resolves to Aqua's dynamic system colors (``systemTextColor`` and
  friends), which follow the light/dark appearance natively, plus a small set
  of derived grays for the tokens Aqua only exposes with an alpha channel --
  Tk drops the alpha and hands back pure white, which would be invisible. Only
  the accent, link and focus roles take the Ink & Copper values there.
* **Linux** reuses the same literals: Aqua's color names do not exist on X11,
  and the GUI is untested there anyway.

Resolution needs a live widget (macOS queries system colors, and Windows asks
Tk which font faces GDI enumerates), so :func:`bind` is called by each
top-level window builder and the result is cached until the next ``bind``.
Re-binding per window is deliberate: it is what makes reopening the manager
pick up an appearance the user changed while it was closed. A live switch with
the window open is not handled -- the tokens mapped to system color *names*
repaint themselves, the derived grays do not.
"""

import ctypes
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

from i18n import N_, tr
from platform_support import current_os


# The legacy size scale every ``font(n)`` call site uses, in points. macOS and
# Linux shift it by the distance between this and their own system default, so
# 9 stays "body text" rather than "unreadably small". Windows maps the same
# sizes onto the pixel type ramp below instead.
BODY_FONT_SIZE = 9

# Windows family type ramp (DS 2.7.1), in pixels at 96 DPI.
_WINDOWS_ROLE_PX = {"caption": 12, "body": 14, "heading": 18, "title": 28}
_WINDOWS_LEGACY_ROLES = {8: "caption", 9: "body", 10: "body", 11: "heading", 12: "heading",
                         14: "title"}
# Headings and titles are semibold by definition; captions and body text only
# when the caller asks for weight.
_STRONG_ROLES = frozenset({"heading", "title"})
# Segoe UI Variable switches optical size at the display sizes.
_DISPLAY_MIN_PX = 18

# Tk resolves only the face names GDI enumerates. The bare "Segoe UI Variable"
# is not one of them and silently falls back to Arial; GDI also truncates face
# names to 31 characters, which is why the Display semibold face is cut short.
# The last candidate is the face every supported Windows release ships.
_WINDOWS_FACES = {
    ("text", False): ("Segoe UI Variable Text", "Segoe UI"),
    ("display", False): ("Segoe UI Variable Display", "Segoe UI"),
    ("text", True): ("Segoe UI Variable Text Semibold", "Segoe UI Semibold"),
    ("display", True): ("Segoe UI Variable Display Semib", "Segoe UI Semibold"),
}
_WINDOWS_EMOJI_FAMILY = "Segoe UI Emoji"
# Not in ``font.families()`` -- it is the hidden system font Tk resolves
# ``TkDefaultFont`` to -- but Tk accepts it by name in a font spec.
_MAC_FAMILY = ".AppleSystemUIFont"
_MAC_EMOJI_FAMILY = "Apple Color Emoji"
_LINUX_FAMILY = "TkDefaultFont"

# Monospace, for literal code and command text only.
_MONO_FAMILIES = {
    "windows": ("Cascadia Mono", "Consolas"),
    "darwin": ("Menlo",),
    "linux": ("TkFixedFont",),
}

# The toolbar's glyph buttons. Windows needs the dedicated symbol face; the
# macOS system font already carries the glyphs.
_SYMBOL_FAMILIES = {
    "windows": "Segoe UI Symbol",
    "darwin": _MAC_FAMILY,
    "linux": _LINUX_FAMILY,
}

# Fluent/MDL2 share these semantic codepoints. Other platforms keep readable
# Unicode symbols rather than private-use characters from an unrelated font.
_ICONS = {
    "record": ("\ue8f1", "●"), "library": ("\ue8b7", "▤"),
    "dictation": ("\ue8f1", "🎙"), "settings": ("\ue713", "⚙"),
    "down": ("\ue70d", "▾"), "up": ("\ue70e", "▴"),
}


def icon_family(system=None, families=None):
    if (system or current_os()) == "windows":
        available = set(families or ())
        for face in ("Segoe Fluent Icons", "Segoe MDL2 Assets"):
            if face in available:
                return face
    return None


def windows_high_contrast():
    """Resolve Win32 colors to RGB literals; never persist this OS override."""
    if current_os() != "windows":
        return None
    class HighContrast(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwFlags", ctypes.c_uint),
                    ("lpszDefaultScheme", ctypes.c_wchar_p)]
    state = HighContrast()
    state.cbSize = ctypes.sizeof(state)
    user32 = ctypes.windll.user32
    if not user32.SystemParametersInfoW(0x0042, state.cbSize, ctypes.byref(state), 0) or not state.dwFlags & 1:
        return None
    def color(index):
        value = user32.GetSysColor(index)
        return f"#{value & 255:02x}{(value >> 8) & 255:02x}{(value >> 16) & 255:02x}"
    return {"background": color(5), "text": color(8), "selection": color(13),
            "selected_text": color(14), "disabled": color(17), "link": color(26)}


def client_area_animation_enabled():
    if current_os() != "windows":
        return True
    enabled = ctypes.c_int(1)
    if not ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(enabled), 0):
        return True
    return bool(enabled.value)

BUTTON_VARIANTS = ("neutral", "primary", "quiet", "danger")


class Theme:
    """A resolved palette plus the fonts and metrics for this platform."""

    __slots__ = (
        "kind", "system", "preference", "family", "display_family", "strong_family",
        "display_strong_family", "emoji_family", "mono_family", "symbol_family",
        "size_delta", "px_scale", "icon_family", "high_contrast",
        "surface", "surface_alt", "surface_alt_active", "surface_hover",
        "card", "field", "field_hover",
        "control", "control_active", "control_border", "field_border",
        "text", "text_strong", "text_muted", "text_on_accent", "disabled_fg",
        "border", "divider",
        "accent", "accent_active", "accent_mark", "danger", "danger_active", "focus_ring",
        "link", "warning", "success", "info",
        "danger_bg", "warning_bg", "success_bg", "info_bg",
        "chart_primary", "chart_secondary", "chart_neutral",
        "overlay_bg", "overlay_border", "overlay_text", "overlay_muted",
        "overlay_live", "overlay_busy", "overlay_ok", "overlay_trouble",
        "select_bg", "select_fg", "text_native", "tab_unselected_fg",
        "space_xs", "space_sm", "space_md", "space_lg", "space_xl",
        "tree_row_height",
    )

    def __init__(self, kind, system, preference, faces, emoji_family, mono_family,
                 symbol_family, size_delta, colors, px_scale=1.0):
        self.kind = kind
        self.system = system
        self.preference = preference
        self.family = faces[("text", False)]
        self.display_family = faces[("display", False)]
        self.strong_family = faces[("text", True)]
        self.display_strong_family = faces[("display", True)]
        self.emoji_family = emoji_family
        self.mono_family = mono_family
        self.symbol_family = symbol_family
        self.icon_family = None
        self.high_contrast = False
        self.size_delta = size_delta
        self.px_scale = px_scale
        for name, value in colors.items():
            setattr(self, name, value)
        self.space_xs = 4
        self.space_sm = 8
        self.space_md = 12
        self.space_lg = 16
        self.space_xl = 24
        # Compact density: 32px rows.
        self.tree_row_height = 32

    def px(self, value):
        return round(value * self.px_scale)

    def font(self, size=BODY_FONT_SIZE, weight=None):
        """Return a font spec for a legacy size; Windows maps it onto the type ramp."""
        if self.system != "windows":
            return _spec(self.family, size + self.size_delta, weight)
        return self._role_spec(_windows_role(size), weight == "bold")

    def caption(self):
        return self.font(8)

    def body(self):
        return self.font(9)

    def body_strong(self):
        return self.font(9, "bold")

    def heading(self):
        return self.font(12, "bold")

    def title(self):
        return self.font(14, "bold")

    def mono(self):
        return self.mono_font(9)

    def icon(self, name):
        return _ICONS[name][0 if self.icon_family else 1]

    def icon_font(self):
        return (self.icon_family, self._pixels("body")) if self.icon_family else self.body()

    def emoji_font(self, size=BODY_FONT_SIZE, weight=None):
        return _spec(self.emoji_family, size + self.size_delta, weight)

    def mono_font(self, size=BODY_FONT_SIZE, weight=None):
        if self.system != "windows":
            return _spec(self.mono_family, size + self.size_delta, weight)
        return _spec(self.mono_family, self._pixels(_windows_role(size)), weight)

    def _pixels(self, role):
        # Negative sizes are pixels in Tk. Scaling them by Tk's own DPI factor
        # keeps the ramp the size the point scale had under DPI awareness.
        return -round(_WINDOWS_ROLE_PX[role] * self.px_scale)

    def _role_spec(self, role, strong):
        pixels = self._pixels(role)
        display = _WINDOWS_ROLE_PX[role] >= _DISPLAY_MIN_PX
        if not (strong or role in _STRONG_ROLES):
            return (self.display_family if display else self.family, pixels)
        semibold = self.display_strong_family if display else self.strong_family
        if semibold:
            return (semibold, pixels)
        return (self.display_family if display else self.family, pixels, "bold")

    @property
    def is_dark(self):
        return self.kind == "dark"

    def entry_colors(self):
        """Colors every text-entry widget needs so Aqua cannot theme it blind.

        A ``tk.Entry``/``tk.Text`` that sets neither ``bg`` nor ``fg`` inherits
        the system appearance while its parent frame carries an explicit color;
        that mismatch is what renders as a black box in dark mode.

        Empty for the light Windows/Linux palette, and that is the point:
        native defaults are already right there, including the system text
        selection color. Opaque dark themes need explicit fields and selection
        colors because the OS otherwise leaves these classic Tk widgets light.
        """
        if self.system != "darwin" and not self.is_dark and not self.high_contrast:
            return {}
        return {
            "bg": self.field,
            "fg": self.text,
            "insertbackground": self.text,
            "selectbackground": self.select_bg,
            "selectforeground": self.select_fg,
            "disabledbackground": self.surface_alt,
            "disabledforeground": self.disabled_fg,
            # Read-only entries otherwise keep Win32's light button face under
            # the light dark-mode text, which makes their value invisible.
            "readonlybackground": self.surface_alt,
        }

    def entry_chrome(self, readonly=False):
        """Flat field border for ``tk.Entry``, ``tk.Text`` and ``tk.Listbox``. Native on macOS.

        Win32's sunken relief draws its light bevel edges in white, so on a
        white card only the top shadow line survives and the field reads as a
        stray rule. The border width doubles as inner padding in ``bg`` and
        brings a one-line entry to the Compact 32px height; :func:`focus_ring`
        with ``shrink=("bd",)`` trades one pixel of it for the 2px focus ring.
        A ``readonly`` text pane is content rather than a control, so its idle
        edge is the decorative line instead of the control boundary.
        """
        if self.system == "darwin":
            return {}
        return {
            "relief": "flat",
            "bd": 5,
            "highlightthickness": 1,
            "highlightbackground": self.border if readonly else self.field_border,
            "highlightcolor": self.focus_ring,
        }

    def text_colors(self):
        """:meth:`entry_colors` for ``tk.Text``, which spells 'disabled' differently."""
        colors = self.entry_colors()
        if not colors:
            return colors
        colors.pop("disabledbackground", None)
        colors.pop("disabledforeground", None)
        colors.pop("readonlybackground", None)
        colors["inactiveselectbackground"] = self.select_bg
        return colors

    def listbox_colors(self):
        """See :meth:`entry_colors` for the same native-versus-painted seam.

        Off macOS the boundary and focus ring belong to :meth:`entry_chrome`.
        """
        if self.system != "darwin" and not self.is_dark:
            return {}
        colors = {
            "bg": self.field,
            "fg": self.text,
            "selectbackground": self.select_bg,
            "selectforeground": self.select_fg,
        }
        if self.system == "darwin":
            # Tk's default highlight ring is near-white on the dark surfaces.
            colors["highlightbackground"] = self.field_border
            colors["highlightcolor"] = self.focus_ring
        return colors

    # Buttons and checkboxes are the widgets Aqua draws *itself*, and it draws
    # them from the appearance rather than from what the app asks for. It
    # ignores ``-background`` outright and keeps its own light bezel, but it
    # does honour ``-foreground`` -- so a well-meant `fg=systemTextColor` puts
    # white text on that light bezel and the button renders as a blank box
    # (seen on macOS 15 / Tk 9.0). Every button color helper therefore answers
    # nothing on macOS: the native control is already correct in both
    # appearances, and the only way to break it is to paint on it.

    def checkbutton_colors(self, bg):
        """Colors for a checkbox or radio button sitting on ``bg``. Native on macOS."""
        if self.system == "darwin":
            return {}
        colors = {
            "bg": bg,
            "fg": self.text_native,
            "activebackground": bg,
            "activeforeground": self.text_native,
            # Win32 marks a focused checkbox with its own dotted rectangle.
            "disabledforeground": self.disabled_fg,
        }
        if self.is_dark:
            # Win32 paints ``selectcolor`` behind the indicator in *both*
            # states and draws the check mark in ``fg``; an accent fill made
            # every box look checked and hid the light mark.
            colors["selectcolor"] = self.field
        return colors

    def nav_button_colors(self, bg, selected=False):
        """Colors for a navigation button on ``bg``. Native on macOS.

        A selected item sits on the selection plane; its marker and weight are
        drawn by the caller. The label never turns copper.
        """
        if self.system == "darwin":
            return {}
        plane = self.select_bg if selected else bg
        return {
            "bg": plane,
            "fg": self.select_fg if selected else self.text_native,
            "activebackground": self.surface_hover,
            "activeforeground": self.text_native,
            "highlightbackground": plane,
        }

    def glyph_button_colors(self, bg):
        """Colors for a small icon button sitting on ``bg`` (the ✎ rename)."""
        if self.system == "darwin":
            return {}
        # No activeforeground: the shipped button did not set one, and adding
        # it would change the pressed state on Windows.
        return {
            "bg": bg,
            "fg": self.text_muted,
            "activebackground": self.field_hover,
        }

    def button_width(self, chars):
        """Fixed button width in characters, or 0 to let the button size itself.

        The widths in the GUI were picked against flat Win32 buttons. Aqua's
        native bezel is wider than the text it wraps, so the same numbers
        overflow their pane there and clip the last button in a row (measured:
        the five-button editor row needs 630px in a 433px pane). Natural
        sizing costs Aqua nothing -- its minimum width is already generous --
        and Windows keeps the tuned numbers.
        """
        return 0 if self.system == "darwin" else chars

    @property
    def manager_window_size(self):
        """``(geometry, min_width, min_height)`` for the manager window.

        macOS needs a wider default because Aqua's native buttons and controls
        have larger minimum metrics than their Win32 counterparts. The minimum
        width includes the navigation sidebar, which trades horizontal room
        for the height a header and tab strip used to take.
        """
        if self.system == "darwin":
            return ("1160x840", 1100, 700)
        return ("1120x820", 1040, 700)

    @property
    def stacked_toolbar_status(self):
        """True where the format status must sit below the toolbar, not beside it.

        Nine native buttons already fill the editor pane on macOS; leaving the
        status label on the same row pushes the last button off the edge.
        """
        return self.system == "darwin"

    def button_chrome(self, compact=False, ring=True):
        """Platform-safe geometry and focus treatment for manager buttons.

        Compact buttons measure 33px with the 14px body face (Win32 adds a
        fixed 5px around a push button's text). A ``tk.Menubutton`` gets no
        such inset, so the regular padding brings it to the same 33px.
        ``ring=False`` drops the idle ring for buttons that sit flush on a
        navigation surface; padding keeps the same footprint either way.
        """
        if self.system == "darwin":
            return {}
        inset = 1 if ring else 0
        return {
            "relief": "flat",
            "bd": 0,
            "padx": (8 if compact else 12) + 1 - inset,
            "pady": (3 if compact else 6) + 1 - inset,
            # The idle ring color belongs to button_colors: a neutral button
            # needs a visible edge, a filled one must not wear a grey ring.
            "highlightthickness": inset,
            "highlightcolor": self.focus_ring,
            "cursor": "hand2",
        }

    def card_options(self):
        """Tk frame options for WinUI-style settings cards.

        Tk has no reliable cross-platform rounded-corner primitive. A quiet
        one-pixel border and the card surface preserve the hierarchy without
        drawing faux rounded controls that would clash with native widgets.
        """
        return {
            "bg": self.card,
            "highlightbackground": self.border,
            "highlightthickness": 1,
            "bd": 0,
        }

    def button_colors(self, variant="neutral", *, surface=None, disabled=False):
        """Colors for a button ``variant`` sitting on ``surface``. Native on macOS.

        Filled and quiet variants blend their idle ring into ``surface``, so
        only keyboard focus draws one; the neutral variant keeps a visible
        control boundary. A disabled button shows no copper and no danger
        fill, whatever its variant.
        """
        if variant not in BUTTON_VARIANTS:
            raise ValueError(f"unknown button variant: {variant!r}")
        if self.system == "darwin":
            return {}
        if surface is None and variant != "neutral":
            raise ValueError(f"a {variant} button needs the surface it sits on")
        # The focus color itself is chrome (button_chrome); these are the
        # colors that change with variant and state.
        colors = {"disabledforeground": self.disabled_fg}
        if disabled:
            quiet = variant == "quiet"
            colors.update({
                "bg": surface if quiet else self.control,
                "fg": self.disabled_fg,
                "activebackground": surface if quiet else self.control,
                "activeforeground": self.disabled_fg,
                "highlightbackground": surface if quiet else self.border,
            })
            return colors
        if variant == "primary":
            colors.update({
                "bg": self.accent,
                "fg": self.text_on_accent,
                "activebackground": self.accent_active,
                "activeforeground": self.text_on_accent,
                "highlightbackground": surface,
            })
        elif variant == "danger":
            colors.update({
                "bg": self.danger_bg,
                "fg": self.danger,
                "activebackground": self.danger_active,
                "activeforeground": self.danger_bg,
                "highlightbackground": self.danger,
            })
        elif variant == "quiet":
            colors.update({
                "bg": surface,
                "fg": self.link,
                "activebackground": self.surface_hover,
                "activeforeground": self.link,
                "highlightbackground": surface,
            })
        else:
            # A neutral button sits on cards as often as on the page surface,
            # so its edge carries the boundary on both.
            colors.update({
                "bg": self.control,
                "fg": self.text_native,
                "activebackground": self.control_active,
                "activeforeground": self.text_native,
                "highlightbackground": self.control_border,
            })
        return colors


def _spec(family, size, weight=None):
    return (family, size) if weight is None else (family, size, weight)


def _windows_role(size):
    try:
        return _WINDOWS_LEGACY_ROLES[size]
    except KeyError:
        raise ValueError(f"no Windows type role for font size {size!r}") from None


# ---------------------------------------------------------------------------
# Palettes
# ---------------------------------------------------------------------------

# The HUD stays dark in both appearances: it floats over arbitrary
# application content, so it carries its own fixed surface and tones.
_OVERLAY = {
    "overlay_bg": "#1c1c1e",
    "overlay_border": "#454545",
    "overlay_text": "#F2EFE9",
    "overlay_muted": "#bcbcbc",
    "overlay_live": "#E6A05A",
    "overlay_busy": "#8CA8CF",
    "overlay_ok": "#73E0A1",
    "overlay_trouble": "#FF8A85",
}

# Teô DS 2.7.1 Windows family, light appearance. Tk cannot reproduce Mica or
# Acrylic reliably, so hierarchy comes from the opaque surface steps
# (#e9e8e7 navigation, #f6f5f4 page, #ffffff content) and restrained borders.
_LIGHT = {
    "surface": "#f6f5f4",
    "surface_alt": "#e9e8e7",
    "surface_alt_active": "#e8e6e3",
    "surface_hover": "#e8e6e3",
    "card": "#ffffff",
    "field": "#ffffff",
    "field_hover": "#e8e6e3",
    "control": "#ffffff",
    "control_active": "#e8e6e3",
    "control_border": "#7b8088",
    "field_border": "#7b8088",
    "text": "#202630",
    "text_strong": "#202630",
    "text_muted": "#606060",
    "text_on_accent": "#15100A",
    # Text blended 45% over the control face: Tk has no opacity.
    "disabled_fg": "#9B9DA2",
    "border": "#d8d8d8",
    "divider": "#d8d8d8",
    "accent": "#E6A05A",
    # Win32 shows a button's active colors only while it is pressed.
    "accent_active": "#BE7836",
    # Raw copper fails 3:1 on light surfaces; marks use the dark copper.
    "accent_mark": "#A0520A",
    "danger": "#9B2C2C",
    "danger_active": "#9B2C2C",
    "focus_ring": "#263C56",
    "link": "#A0520A",
    "warning": "#7A4A00",
    "success": "#16643B",
    "info": "#263C56",
    "danger_bg": "#FCE8E8",
    "warning_bg": "#FFF1D6",
    "success_bg": "#E6F6EC",
    "info_bg": "#E8EEF5",
    "chart_primary": "#A0520A",
    "chart_secondary": "#263C56",
    "chart_neutral": "#7B8492",
    "select_bg": "#e8e6e3",
    "select_fg": "#202630",
    # Foreground for widgets the pre-change GUI left uncolored. Resolved to
    # each platform's own default so filling it in changes nothing there.
    "text_native": "#202630",
    "tab_unselected_fg": "#606060",
    **_OVERLAY,
}

# Teô DS 2.7.1 Windows family, dark appearance (the default).
_DARK = {
    "surface": "#232323",
    "surface_alt": "#292929",
    "surface_alt_active": "#484848",
    "surface_hover": "#484848",
    "card": "#1c1c1e",
    "field": "#1c1c1e",
    "field_hover": "#484848",
    "control": "#1c1c1e",
    "control_active": "#484848",
    "control_border": "#999ca3",
    "field_border": "#999ca3",
    "text": "#F2EFE9",
    "text_strong": "#F2EFE9",
    "text_muted": "#bcbcbc",
    "text_on_accent": "#15100A",
    "disabled_fg": "#7C7B79",
    "border": "#454545",
    "divider": "#454545",
    "accent": "#E6A05A",
    "accent_active": "#CE8845",
    "accent_mark": "#E6A05A",
    "danger": "#FF8A85",
    "danger_active": "#FF8A85",
    "focus_ring": "#F0C293",
    "link": "#EDB47C",
    "warning": "#FFD27A",
    "success": "#73E0A1",
    "info": "#ADC1DC",
    "danger_bg": "#351718",
    "warning_bg": "#342A17",
    "success_bg": "#13271D",
    "info_bg": "#202E40",
    "chart_primary": "#E6A05A",
    "chart_secondary": "#8CA8CF",
    "chart_neutral": "#788697",
    "select_bg": "#484848",
    "select_fg": "#F2EFE9",
    "text_native": "#F2EFE9",
    "tab_unselected_fg": "#bcbcbc",
    **_OVERLAY,
}

# Win32's defaults for the widgets this GUI leaves uncolored (tkWinDefault.h).
# Naming them explicitly is a no-op on Windows and keeps the seam honest.
_WINDOWS_NATIVE = {
    "text_native": "SystemButtonText",
}

# Aqua exposes most of these as dynamic system colors that repaint themselves
# when the appearance changes -- but only the *opaque* ones survive the trip
# through Tk. ``systemSecondaryLabelColor``, ``systemSeparatorColor`` and
# ``systemPlaceholderTextColor`` all carry an alpha channel that Tk discards,
# yielding pure white in dark mode; those tokens are fixed grays instead.
_MAC_SYSTEM = {
    "surface": "systemWindowBackgroundColor",
    "card": "systemTextBackgroundColor",
    "field": "systemTextBackgroundColor",
    "text": "systemTextColor",
    "text_strong": "systemTextColor",
    "tab_unselected_fg": "systemTextColor",
    "select_bg": "systemSelectedTextBackgroundColor",
    "select_fg": "systemTextColor",
}

_MAC_NATIVE = {
    "text_native": "systemTextColor",
}

_MAC_LIGHT_OVERRIDES = {
    # Apple's neutral gray: legible against both the light and dark surfaces.
    "text_muted": "#6E6E73",
}

_MAC_DARK_OVERRIDES = {
    "surface_alt": "#2C2C2E",
    "surface_alt_active": "#3A3A3C",
    "surface_hover": "#3A3A3C",
    "field_hover": "#2C2C2E",
    "text_muted": "#98989D",
    "border": "#48484A",
    "divider": "#48484A",
    "warning": "#E0A458",
    "success": "#4ADE80",
    "danger": "#FF6961",
    "danger_active": "#FF453A",
}


def palette(kind, system=None):
    """Return the color token map for ``kind`` in {'windows', 'light', 'dark'}.

    Pure: no Tk, no platform probing. ``'windows'`` returns the opaque light
    Windows-family palette; it is also what Linux gets, since Aqua's color
    names do not resolve there. ``system`` only selects the platform's native
    defaults for the ``*_native`` tokens.
    """
    if system is None:
        # Preserve the public palette helper's historical contract: ``windows``
        # is the literal light map, while light/dark exercise Aqua tokens.
        system = "darwin" if kind in {"light", "dark"} else None
    colors = dict(_DARK if kind == "dark" else _LIGHT)
    if system != "darwin":
        if kind in {"windows", "light"} and system == "windows":
            colors.update(_WINDOWS_NATIVE)
        return colors
    if kind == "windows":
        if system == "windows":
            colors.update(_WINDOWS_NATIVE)
        return colors
    colors.update(_MAC_SYSTEM)
    colors.update(_MAC_NATIVE)
    colors.update(_MAC_DARK_OVERRIDES if kind == "dark" else _MAC_LIGHT_OVERRIDES)
    return colors


def rgb(color):
    """``#RRGGBB`` as sRGB channels in 0..1, for native APIs that take floats."""
    if not (isinstance(color, str) and len(color) == 7 and color.startswith("#")):
        raise ValueError(f"not a #RRGGBB color: {color!r}")
    return tuple(int(color[index:index + 2], 16) / 255 for index in (1, 3, 5))


def _first_available(candidates, families):
    """The first candidate Tk can resolve; the guaranteed last one if unknown."""
    if families is not None:
        for name in candidates:
            if name in families:
                return name
    return candidates[-1]


def font_faces(system=None, families=None):
    """``{(optical, strong): family}`` for this platform.

    ``families`` is what ``tkfont.families()`` reports, or ``None`` when no Tk
    is available yet (the guaranteed faces are assumed then). A missing
    semibold face is ``None``: the caller synthesizes bold instead.
    """
    system = system or current_os()
    if system == "windows":
        faces = {}
        for key, candidates in _WINDOWS_FACES.items():
            face = _first_available(candidates, families)
            if key[1] and families is not None and face not in families:
                face = None
            faces[key] = face
        return faces
    family = _MAC_FAMILY if system == "darwin" else _LINUX_FAMILY
    return {(optical, strong): family if not strong else None
            for optical in ("text", "display") for strong in (False, True)}


def font_family(system=None, families=None):
    return font_faces(system, families)[("text", False)]


def emoji_family(system=None):
    system = system or current_os()
    if system == "windows":
        return _WINDOWS_EMOJI_FAMILY
    if system == "darwin":
        return _MAC_EMOJI_FAMILY
    return _LINUX_FAMILY


def mono_family(system=None, families=None):
    candidates = _MONO_FAMILIES.get(system or current_os(), _MONO_FAMILIES["linux"])
    return _first_available(candidates, families)


def symbol_family(system=None):
    return _SYMBOL_FAMILIES.get(system or current_os(), _SYMBOL_FAMILIES["linux"])


def size_delta(system=None, default_size=None):
    """Shift between the legacy point scale and this platform's system size.

    Windows maps the legacy sizes onto its pixel ramp instead (0). Elsewhere
    the GUI's body size is pinned to the platform's own ``TkDefaultFont`` size
    so a 9 pt label does not render two points below every native control.
    """
    if (system or current_os()) == "windows" or not default_size:
        return 0
    return int(default_size) - BODY_FONT_SIZE


def ttk_theme_preference(system=None):
    """ttk themes to try, best first. The last is Tk's built-in fallback.

    Windows uses clam in both appearances: Vista draws its own control faces,
    selection and meters and ignores the palette. Clam is also the portable
    theme that honors the palette elsewhere; macOS keeps native Aqua.
    """
    if (system or current_os()) == "darwin":
        return ("aqua", "clam", "default")
    return ("clam", "default")


def apply_ttk_theme(style, system=None, resolved=None):
    """Select the best available ttk theme. Returns the theme actually in use."""
    try:
        available = set(style.theme_names())
    except Exception:
        return None
    ui = resolved or theme()
    for name in ttk_theme_preference(system or ui.system):
        if name not in available:
            continue
        try:
            style.theme_use(name)
            return name
        except Exception:
            continue
    try:
        return style.theme_use()
    except Exception:
        return None


def configure_combobox_popdown(combo, resolved=None, *, fit_values=False):
    """Theme a ttk combobox's transient listbox on non-native platforms.

    ttk creates the popdown lazily, so this is intended for the combobox's
    ``postcommand`` callback.  Aqua owns the native macOS list's colors and
    font, so there the listbox is left untouched and ``None`` is returned;
    only an opted-in ``fit_values`` width applies, through the style's post
    offset, which ttk honors on every platform.
    """
    ui = resolved or theme()
    listbox = None
    if ui.system != "darwin":
        popdown = combo.tk.call("ttk::combobox::PopdownWindow", str(combo))
        listbox = f"{popdown}.f.l"
        combo.tk.call(
            listbox,
            "configure",
            "-background", ui.field,
            "-foreground", ui.text,
            "-selectbackground", ui.select_bg,
            "-selectforeground", ui.select_fg,
            "-font", ui.body(),
            "-relief", "flat",
            "-borderwidth", ui.space_sm,
            "-highlightthickness", 0,
            "-selectborderwidth", 0,
            "-activestyle", "none",
        )
    if fit_values:
        # Callers opt in with their own style so unrelated comboboxes retain
        # their popup geometry. Fit long endpoint names inside the window.
        font = combobox_font(combo, ui)
        widest = max((font.measure(str(value)) for value in combo.cget("values")), default=0)
        window = combo.winfo_toplevel()
        width = max(combo.winfo_width(), min(widest + 2 * ui.space_sm + 24,
                                            window.winfo_width() - 2 * ui.space_md))
        right = window.winfo_rootx() + window.winfo_width() - ui.space_md
        shift = min(0, right - combo.winfo_rootx() - width)
        combo.tk.call("ttk::style", "configure", combo.cget("style"), "-postoffset",
                      (shift, 0, width - combo.winfo_width(), 0))
    return listbox


def combobox_font(combo, resolved=None):
    ui = resolved or theme()
    if ui.system == "darwin":
        popup = combo.tk.call("ttk::combobox::PopdownWindow", str(combo))
        # Tk 8.6 uses a Listbox; Tk 9 uses a native Menu on Aqua. Read only
        # the actual widget's configured font, leaving native painting intact.
        native = next((name for name in (f"{popup}.menu", f"{popup}.f.l")
                       if combo.tk.getboolean(combo.tk.call("winfo", "exists", name))), None)
        if native is None:
            raise tk.TclError("combobox popup has no native Menu or Listbox")
        native_font = combo.tk.call(native, "cget", "-font")
        return tkfont.Font(root=combo, font=native_font)
    return tkfont.Font(root=combo, font=ui.body())


def _focus_edges(ui):
    """Clam field edges: a 1px boundary at rest, a 2px focus ring on focus.

    Clam draws a field as an outer border plus an inner bevel, so coloring
    both in the focus color doubles the ring without moving the content.
    """
    return {
        "bordercolor": [("focus", ui.focus_ring)],
        "lightcolor": [("focus", ui.focus_ring)],
        "darkcolor": [("focus", ui.focus_ring)],
    }


def configure_manager_styles(style, resolved=None):
    """Apply the shared Windows-family shell styles to a live ttk style object."""
    ui = resolved or theme()
    # Clam's defaults are near-white bevels around every tab.
    style.configure(
        "Manager.TNotebook",
        background=ui.surface,
        borderwidth=0,
        tabmargins=(0, 0, 0, 0),
        bordercolor=ui.border,
        lightcolor=ui.surface,
        darkcolor=ui.surface,
    )
    style.configure(
        "Manager.TNotebook.Tab",
        padding=(18, 10),
        font=ui.body_strong(),
        foreground=ui.tab_unselected_fg,
        background=ui.surface,
        borderwidth=0,
        bordercolor=ui.border,
        lightcolor=ui.surface,
        darkcolor=ui.surface,
    )
    style.map(
        "Manager.TNotebook.Tab",
        foreground=[("selected", ui.text_strong), ("active", ui.text_strong)],
        background=[("selected", ui.card), ("active", ui.surface_hover)],
        lightcolor=[("selected", ui.card)],
        # Built-in themes may map selected tabs to narrower horizontal padding.
        # Keep the selected tab's geometry stable while changing its colors.
        padding=[("selected", (18, 10))],
        expand=[("selected", (0, 0, 0, 0))],
    )
    # Page container for a shell that navigates from its own sidebar: the
    # same frame, with the tab strip removed so it costs no vertical space.
    style.configure(
        "Pages.TNotebook",
        background=ui.surface,
        borderwidth=0,
        tabmargins=(0, 0, 0, 0),
        bordercolor=ui.surface,
        lightcolor=ui.surface,
        darkcolor=ui.surface,
    )
    style.layout("Pages.TNotebook.Tab", [])
    # Progress and level meters are chart marks, not actions: the primary
    # series holds 3:1 against its track, the second source is the
    # comparison series.
    for name, mark in (("Horizontal.TProgressbar", ui.chart_primary),
                       ("Secondary.Horizontal.TProgressbar", ui.chart_secondary)):
        style.configure(
            name,
            troughcolor=ui.field,
            background=mark,
            bordercolor=ui.border,
            lightcolor=mark,
            darkcolor=mark,
        )
    style.configure(
        "Horizontal.TScale",
        background=ui.control,
        troughcolor=ui.surface_alt_active,
        bordercolor=ui.field_border,
        lightcolor=ui.control,
        darkcolor=ui.control,
    )
    style.map(
        "Horizontal.TScale",
        background=[("active", ui.control_active)],
        **_focus_edges(ui),
    )
    style.configure("TPanedwindow", background=ui.surface)
    style.configure(
        "Sash",
        background=ui.surface,
        bordercolor=ui.surface,
        lightcolor=ui.surface,
        darkcolor=ui.surface,
        gripcount=0,
    )
    style.configure("Manager.TFrame", background=ui.surface)
    style.configure("TFrame", background=ui.surface)
    style.configure("TLabel", background=ui.surface, foreground=ui.text)
    # One combobox contract; Device.TCombobox and its per-track children only
    # change the font, so every field shares its boundary, focus and density.
    style.configure(
        "TCombobox",
        fieldbackground=ui.field,
        background=ui.control,
        foreground=ui.text,
        arrowcolor=ui.text,
        bordercolor=ui.field_border,
        lightcolor=ui.field,
        darkcolor=ui.field,
        padding=(8, 5),
    )
    style.map(
        "TCombobox",
        fieldbackground=[("disabled", ui.surface_alt), ("readonly", ui.field)],
        foreground=[("disabled", ui.disabled_fg), ("readonly", ui.text)],
        background=[("disabled", ui.surface_alt), ("pressed", ui.control_active),
                    ("active", ui.field_hover)],
        arrowcolor=[("disabled", ui.disabled_fg)],
        selectbackground=[("readonly", "focus", ui.select_bg), ("readonly", ui.field)],
        selectforeground=[("readonly", ui.text)],
        **_focus_edges(ui),
    )
    style.configure("Device.TCombobox", font=ui.body())
    for orientation in ("Vertical", "Horizontal"):
        style.configure(
            f"{orientation}.TScrollbar",
            background=ui.field_border,
            troughcolor=ui.surface,
            bordercolor=ui.surface,
            arrowcolor=ui.surface,
            lightcolor=ui.field_border,
            darkcolor=ui.field_border,
        )
        # Clam maps a near-white face onto idle states (e.g. an empty list's
        # full-length thumb), overriding the configured background.
        style.map(
            f"{orientation}.TScrollbar",
            background=[("pressed", ui.text_muted), ("active", ui.text_muted),
                        ("!active", ui.field_border)],
        )
    for name in ("Manager.Treeview", "Meeting.Treeview"):
        # A 2px field edge in the row color: invisible at rest, the focus
        # ring when the list has the keyboard.
        style.configure(
            name,
            background=ui.card,
            fieldbackground=ui.card,
            foreground=ui.text,
            rowheight=ui.tree_row_height,
            font=ui.body(),
            borderwidth=2,
            bordercolor=ui.card,
            lightcolor=ui.card,
            darkcolor=ui.card,
        )
        style.map(
            name,
            background=[("selected", ui.select_bg)],
            foreground=[("selected", ui.select_fg)],
            **_focus_edges(ui),
        )
        # Headings sit on the content plane with a line under them, so a
        # selected first row never merges into the header band. Clam's raised
        # border draws its dark edge bottom and right only; that right edge
        # doubles as the column separator.
        style.configure(
            f"{name}.Heading",
            background=ui.card,
            foreground=ui.text_strong,
            font=ui.body_strong(),
            padding=(8, 6),
            relief="raised",
            borderwidth=1,
            bordercolor=ui.card,
            lightcolor=ui.card,
            darkcolor=ui.border,
        )
        hover = [("active", ui.surface_hover)]
        style.map(f"{name}.Heading", background=hover, bordercolor=hover, lightcolor=hover)
    return style


# ---------------------------------------------------------------------------
# Controls
# ---------------------------------------------------------------------------

def _surface_of(widget):
    """The background ``widget`` paints, for a child that must blend into it."""
    try:
        color = str(widget.cget("background"))
    except tk.TclError:
        # ttk widgets take their background from the style, not an option.
        style = str(widget.cget("style")) or widget.winfo_class()
        color = str(ttk.Style(widget).lookup(style, "background"))
    if not color:
        raise ValueError(f"cannot resolve the background behind {widget}")
    return color


def focus_ring(widget, resolved=None, shrink=("padx", "pady")):
    """Draw a 2px focus ring while ``widget`` has keyboard focus. Native on macOS.

    The idle ring (1px, or none) grows to 2px on focus and ``shrink`` gives
    those pixels back from the padding, so the control never changes size.
    """
    ui = resolved or theme()
    if ui.system == "darwin":
        return widget
    rest_ring = int(widget.cget("highlightthickness"))
    rest = {option: int(widget.cget(option)) for option in shrink}
    focused = {option: max(0, value - (2 - rest_ring)) for option, value in rest.items()}
    # Win32 never draws the highlight ring of a push button. It draws the
    # "default" ring instead -- always, in -highlightcolor -- so that ring
    # carries both the idle edge and the focus ring.
    default_ring = ui.system == "windows" and widget.winfo_class() == "Button"
    if default_ring:
        tk.Misc.configure(widget, default="active",
                          highlightcolor=widget.cget("highlightbackground"))

    def show(focus):
        if bool(getattr(widget, "_focus_ring_shown", False)) == focus:
            return
        widget._focus_ring_shown = focus
        options = {**(focused if focus else rest), "highlightthickness": 2 if focus else rest_ring}
        if default_ring:
            options["highlightcolor"] = (ui.focus_ring if focus
                                         else widget.cget("highlightbackground"))
        tk.Misc.configure(widget, **options)

    widget.bind("<FocusIn>", lambda _event: show(True), add="+")
    widget.bind("<FocusOut>", lambda _event: show(False), add="+")
    return widget


class _ThemedButton(tk.Button):
    """A ``tk.Button`` whose colors follow its variant and its state.

    Tk buttons have no per-state background, so a disabled primary would keep
    its copper fill. Setting ``state`` through ``configure``/``config``/item
    assignment repaints the button from :meth:`Theme.button_colors`.
    """

    def __init__(self, master, ui, variant, surface, **options):
        self._ui, self._variant, self._surface = ui, variant, surface
        super().__init__(master, **options)
        self._paint()

    def configure(self, cnf=None, **kw):
        result = super().configure(cnf, **kw)
        if "state" in kw or (isinstance(cnf, dict) and "state" in cnf):
            self._paint()
        return result

    config = configure

    def _paint(self):
        colors = self._ui.button_colors(
            self._variant, surface=self._surface,
            disabled=str(self.cget("state")) == tk.DISABLED,
        )
        if self._ui.system == "windows" and not getattr(self, "_focus_ring_shown", False):
            # Win32 draws the idle edge in -highlightcolor (see focus_ring).
            colors["highlightcolor"] = colors["highlightbackground"]
        tk.Misc.configure(self, **colors)
        glyph = getattr(self, "_disclosure_glyph", None)
        if glyph is not None:
            glyph.configure(bg=colors["bg"], fg=colors["fg"])


def disclosure(widget, label, expanded=False, resolved=None):
    """Keep the written command in the body face and its icon in the icon face."""
    ui = resolved or theme()
    if not ui.icon_family:
        widget.configure(text=f"{label} {ui.icon('up' if expanded else 'down')}")
        return widget
    widget.configure(text=label)
    glyph = getattr(widget, "_disclosure_glyph", None)
    if glyph is None:
        glyph = tk.Label(widget, font=ui.icon_font(), bg=widget.cget("bg"), fg=widget.cget("fg"),
                         bd=0, padx=0, pady=0, takefocus=False)
        widget._disclosure_glyph = glyph
        glyph.place(relx=1, rely=0.5, x=-ui.space_xs, anchor="e")
        glyph.bind("<Button-1>", lambda _event: widget.event_generate("<Button-1>")
                   if widget.winfo_class() == "Menubutton" else widget.invoke())
        widget.configure(padx=int(widget.cget("padx")) + ui.space_sm)
    glyph.configure(text=ui.icon("up" if expanded else "down"))
    return widget


def button(parent, text, command, variant="neutral", *, resolved=None, **options):
    """A themed Compact command button: ``neutral``, ``primary``, ``quiet`` or ``danger``.

    Use one primary per decision area. Every button shares the 33px Compact
    height, so an action row never mixes heights. On macOS this is a native
    Aqua button with the shared font; nothing is painted on it.
    """
    ui = resolved or theme()
    if variant not in BUTTON_VARIANTS:
        raise ValueError(f"unknown button variant: {variant!r}")
    options.setdefault("font", ui.body())
    if ui.system == "darwin":
        widget = tk.Button(parent, text=text, command=command, **options)
    else:
        widget = _ThemedButton(parent, ui, variant, _surface_of(parent), text=text,
                               command=command, **{**ui.button_chrome(compact=True), **options})
    # Native insets differ between Win32, X11 and Aqua. Measure the real request
    # rather than relying on Win32's extra five pixels around the same text.
    missing = max(0, ui.px(32) - widget.winfo_reqheight())
    if missing:
        widget.configure(pady=int(widget.cget("pady")) + (missing + 1) // 2)
    if text.endswith((" ▾", " ▴")):
        disclosure(widget, text[:-2], text.endswith("▴"), ui)
    return focus_ring(widget, ui)


def prepare_dialog(window, owner, *, resolved=None, set_icon=None):
    """Apply base chrome only; callers own presentation, focus, grabs and waits."""
    ui = resolved or theme()
    window.configure(bg=ui.surface)
    parent = owner.winfo_toplevel()
    window.transient(parent)
    if ui.system != "windows" or window.winfo_ismapped():
        apply_window_chrome(window, ui)
    else:
        applied = False
        def mapped(event):
            nonlocal applied
            if event.widget is not window or applied:
                return
            applied = True
            window.unbind("<Map>", binding)
            apply_window_chrome(window, ui)
        # Windows chrome flushes idle layout. Wait until the caller presents
        # its complete dialog instead of mapping an empty Toplevel here.
        binding = window.bind("<Map>", mapped, add="+")
    set_icon = set_icon or getattr(parent, "_snipvoice_set_icon", None)
    if set_icon is not None:
        set_icon(window)
        window._snipvoice_set_icon = set_icon
    return window


def form_dialog(owner, title, fields, *, multiline=(), validate=None, set_icon=None):
    """One bounded themed form; invalid values remain editable in place."""
    ui = theme()
    dialog = tk.Toplevel(owner)
    prepare_dialog(dialog, owner, resolved=ui, set_icon=set_icon)
    dialog.title(title)
    dialog.minsize(420, 220)
    dialog.geometry("560x420" if multiline else "560x280")
    body = tk.Frame(dialog, bg=ui.surface, padx=ui.space_lg, pady=ui.space_lg)
    body.pack(fill="both", expand=True)
    body.columnconfigure(0, weight=1)
    controls, result = {}, [None]
    for row, (key, label, value) in enumerate(fields):
        tk.Label(body, text=label, bg=ui.surface, fg=ui.text, font=ui.body()).grid(
            row=row * 2, column=0, sticky="w", pady=(ui.space_sm, ui.space_xs))
        if key in multiline:
            control = tk.Text(body, height=5, wrap="word", font=ui.body(),
                              **ui.text_colors(), **ui.entry_chrome())
            control.insert("1.0", value)
            body.rowconfigure(row * 2 + 1, weight=1)
        else:
            control = tk.Entry(body, font=ui.body(), **ui.entry_colors(), **ui.entry_chrome())
            control.insert(0, value)
        focus_ring(control, ui, shrink=("bd",))
        control.grid(row=row * 2 + 1, column=0, sticky="nsew")
        controls[key] = control
    error = tk.Label(body, bg=ui.surface, fg=ui.danger, font=ui.caption(),
                     anchor="w", justify="left", width=1, wraplength=440)
    error.grid(row=len(fields) * 2, column=0, sticky="ew", pady=ui.space_sm)
    error.bind("<Configure>", lambda event: error.configure(wraplength=max(120, event.width)))
    actions = tk.Frame(body, bg=ui.surface)
    actions.grid(row=len(fields) * 2 + 1, column=0, sticky="e")

    def save():
        values = {key: (control.get("1.0", "end-1c") if key in multiline else control.get())
                  for key, control in controls.items()}
        try:
            if validate is not None:
                validate(values)
        except ValueError as exc:
            error.configure(text="⚠ " + str(exc))
            next(iter(controls.values())).focus_set()
            return
        result[0] = values
        dialog.destroy()

    button(actions, tr("Cancelar"), dialog.destroy, resolved=ui).pack(side="left")
    button(actions, tr("Salvar"), save, "primary", resolved=ui).pack(side="left", padx=(ui.space_sm, 0))
    dialog.bind("<Escape>", lambda _event: dialog.destroy())
    dialog.bind("<Control-s>", lambda _event: save())
    dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
    dialog.wait_visibility()
    dialog.grab_set()
    next(iter(controls.values())).focus_set()
    owner.wait_window(dialog)
    owner.focus_set()
    return result[0]


def ellipsize(text, font, width):
    if font.measure(text) <= width:
        return text
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if font.measure(text[:middle] + "…") <= width:
            low = middle
        else:
            high = middle - 1
    return text[:low] + "…"


def confirm_draft(owner):
    ui = theme()
    dialog = tk.Toplevel(owner)
    prepare_dialog(dialog, owner, resolved=ui)
    dialog.title(tr("Alterações não salvas"))
    result = [None]
    body = tk.Frame(dialog, bg=ui.surface, padx=ui.space_lg, pady=ui.space_lg)
    body.pack(fill="both", expand=True)
    tk.Label(body, text=tr("Salvar as alterações antes de sair desta seção?"),
             bg=ui.surface, fg=ui.text, font=ui.body(), wraplength=ui.px(380)).pack(fill="x")
    row = tk.Frame(body, bg=ui.surface)
    row.pack(fill="x", pady=(ui.space_md, 0))

    def choose(value):
        result[0] = value
        dialog.destroy()

    for label, value, variant in ((tr("Permanecer"), None, "neutral"),
                                  (tr("Descartar"), "discard", "neutral"),
                                  (tr("Salvar"), "save", "primary")):
        button(row, label, lambda item=value: choose(item), variant, resolved=ui).pack(
            side="left", padx=(0, ui.space_sm))
    dialog.bind("<Escape>", lambda _event: choose(None))
    dialog.protocol("WM_DELETE_WINDOW", lambda: choose(None))
    dialog.wait_visibility()
    dialog.grab_set()
    row.winfo_children()[0].focus_set()
    owner.wait_window(dialog)
    return result[0]


def tooltip(widget, text):
    """A focus/hover disclosure; no grab or activation is taken."""
    state = {"window": None, "timer": None}

    def hide(_event=None):
        if state["timer"] is not None:
            widget.after_cancel(state["timer"])
            state["timer"] = None
        if state["window"] is not None:
            state["window"].destroy()
            state["window"] = None

    def show():
        state["timer"] = None
        value = text() if callable(text) else text
        if not value:
            return
        ui = theme()
        window = tk.Toplevel(widget)
        prepare_dialog(window, widget, resolved=ui)
        window.overrideredirect(True)
        window.geometry(f"+{widget.winfo_rootx()}+{widget.winfo_rooty() + widget.winfo_height()}")
        tk.Label(window, text=value, bg=ui.card, fg=ui.text, font=ui.body(),
                 padx=ui.space_sm, pady=ui.space_xs, wraplength=ui.px(400), justify="left").pack()
        state["window"] = window

    def schedule(_event=None):
        hide()
        state["timer"] = widget.after(500, show)

    for event in ("<Enter>", "<FocusIn>"):
        widget.bind(event, schedule, add="+")
    for event in ("<Leave>", "<FocusOut>", "<Destroy>", "<ButtonPress>"):
        widget.bind(event, hide, add="+")


def model_table(parent, resolved=None):
    ui = resolved or theme()
    table = tk.Frame(parent, bg=ui.card)
    table.pack(fill="x", pady=ui.space_sm)
    progress_width = int(table.tk.call("font", "measure", ui.body(), tr("Baixando {percent}%", percent=100)))
    table.columnconfigure(4, minsize=progress_width + 2 * ui.space_xs)
    for column, (title, weight) in enumerate(zip(
            (tr("Modelo"), tr("Qualidade"), tr("Tamanho"), tr("Licença"), tr("Estado"), tr("Ação")),
            (3, 2, 0, 1, 1, 0))):
        table.columnconfigure(column, weight=weight)
        tk.Label(table, text=title, bg=ui.card, fg=ui.text_strong, font=ui.body_strong(),
                 anchor="e" if column == 2 else "w").grid(
                     row=0, column=column, sticky="ew", padx=ui.space_xs, pady=ui.space_sm)
    return table


def wrap_model_identifier(value, width, measure):
    """Wrap model IDs at their separators before splitting a long component."""
    value = str(value)
    if measure(value) <= width:
        return value
    parts, start = [], 0
    for index, char in enumerate(value):
        if char in "/_-":
            parts.append(value[start:index + 1])
            start = index + 1
    if start < len(value):
        parts.append(value[start:])
    lines, line = [], ""
    for part in parts:
        if line and measure(line + part) > width:
            lines.append(line)
            line = ""
        while len(part) > 1 and measure(part) > width:
            end = 1
            while end < len(part) and measure(part[:end + 1]) <= width:
                end += 1
            # Do not strand the final character of a long component.
            if len(part) - end == 1 and end > 1:
                end -= 1
            lines.append(part[:end])
            part = part[end:]
        line += part
    if line:
        lines.append(line)
    return "\n".join(lines)


def model_table_row(table, row, values, resolved=None):
    ui = resolved or theme()
    for column, value in enumerate(values):
        label = tk.Label(table, text=value, bg=ui.card, fg=ui.text, font=ui.body(),
                         anchor="e" if column == 2 else "w", justify="left",
                         width=8 if column == 2 else 1, wraplength=120)
        label.grid(row=row, column=column, sticky="nsew", padx=ui.space_xs, pady=ui.space_sm)
        if column == 0:
            face = tkfont.Font(root=table, font=ui.body())
            label.bind("<Configure>", lambda event, widget=label, text=str(value), font=face:
                       widget.configure(text=wrap_model_identifier(text, max(32, event.width), font.measure),
                                        wraplength=max(32, event.width)))
            tooltip(label, str(value))
        else:
            label.bind("<Configure>", lambda event, widget=label:
                       widget.configure(wraplength=max(32, event.width)))
    status = tk.Label(table, bg=ui.card, fg=ui.text_muted, font=ui.body(), anchor="w",
                      width=1, wraplength=100, justify="left")
    status.grid(row=row, column=4, sticky="nsew", padx=ui.space_xs)
    status.bind("<Configure>", lambda event: status.configure(wraplength=max(32, event.width)))
    actions = tk.Frame(table, bg=ui.card)
    actions.grid(row=row, column=5, sticky="e", padx=ui.space_xs, pady=ui.space_xs)
    return status, actions


INFO_BAR_TONES = ("danger", "warning", "info", "success")
# A shape per severity, so the bar never depends on its color alone. Tk falls
# back to a symbol face for glyphs the text face lacks.
_INFO_BAR_GLYPHS = {"danger": "⊗", "warning": "⚠", "info": "ⓘ", "success": "✓"}
# The severity written beside the time, for people who do not read tone or shape.
INFO_BAR_WORDS = {"danger": N_("Erro"), "warning": N_("Aviso"), "info": N_("Informação"),
                  "success": N_("Concluído")}


def local_time_text(moment):
    """``moment`` as the 24-hour local clock ("14:02")."""
    return moment.strftime("%H:%M")


def info_bar(parent, ui, tone, title, body, action=None, details=None, copy=None,
             when=None, dismiss=None):
    """A Windows-family InfoBar: tone pair, glyph, title, body, one action, Detalhes.

    ``title`` says what happened, ``body`` (optional) what it affects and what to do.
    ``action`` is one ``(label, command)`` pair. ``details`` is technical text
    kept behind a "Detalhes" disclosure and selectable. ``copy(text, done)``
    adds a Copiar button; it owns the clipboard write and calls ``done(ok)`` on
    the GUI thread, so the bar reports a copy that failed instead of assuming it.
    ``when`` (a local datetime) writes the severity and the time at the right of
    the title ("Erro às 14:02"); the title wraps to make room. ``dismiss`` adds a
    quiet "Fechar".
    """
    if tone not in INFO_BAR_TONES:
        raise ValueError(f"unknown info bar tone: {tone!r}")
    fg, bg = getattr(ui, tone), getattr(ui, f"{tone}_bg")
    # The tone fill is barely lighter than the page, so a 1px tone edge
    # carries the bar's boundary.
    bar = tk.Frame(parent, bg=bg, highlightthickness=1, highlightbackground=fg,
                   highlightcolor=fg, bd=0, padx=ui.space_md, pady=ui.space_sm)
    bar.columnconfigure(1, weight=1)
    tk.Label(bar, text=_INFO_BAR_GLYPHS[tone], font=ui.body_strong(), bg=bg, fg=fg).grid(
        row=0, column=0, sticky="nw", padx=(0, ui.space_sm))
    # width=1 keeps a wrapped title from requesting width back from the page.
    bar.title_label = tk.Label(bar, text=title, font=ui.body_strong(), bg=bg, fg=ui.text,
                               anchor="w", justify="left", width=1, wraplength=360)
    bar.title_label.bind("<Configure>", lambda event: bar.title_label.configure(
        wraplength=max(event.width, 120)))
    bar.title_label.grid(row=0, column=1, sticky="ew")
    if when is not None:
        tk.Label(bar, text=tr("{severity} às {time}", severity=tr(INFO_BAR_WORDS[tone]),
                              time=local_time_text(when)),
                 font=ui.caption(), bg=bg, fg=ui.text, anchor="e").grid(
            row=0, column=2, sticky="ne", padx=(ui.space_sm, 0))
    if body:
        message = tk.Label(bar, text=body, font=ui.body(), bg=bg, fg=ui.text, anchor="w",
                           justify="left", width=1, wraplength=360)
        message.bind("<Configure>",
                     lambda event: message.configure(wraplength=max(event.width, 120)))
        message.grid(row=1, column=1, columnspan=2, sticky="ew")
    if not (action or details or dismiss):
        return bar
    actions = tk.Frame(bar, bg=bg)
    actions.grid(row=2, column=1, columnspan=2, sticky="w", pady=(ui.space_sm, 0))
    if action:
        label, command = action
        button(actions, label, command, resolved=ui).pack(side="left", padx=(0, ui.space_sm))
    if details:
        panel = tk.Frame(bar, bg=bg)
        text = tk.Text(panel, height=3, wrap="word", font=ui.body(), **ui.text_colors(),
                       **ui.entry_chrome(readonly=True))
        focus_ring(text, ui, shrink=("bd",))
        text.insert("1.0", details)
        # Disabled still allows selecting and Ctrl+C; it only blocks edits.
        text.configure(state="disabled")
        text.pack(fill="x")

        if copy:
            def copied(ok):
                if not copy_button.winfo_exists():
                    return
                copy_button.configure(text=tr("Copiado") if ok else tr("Copiar"))
                copy_hint.configure(text="" if ok else tr("Selecione o texto e pressione Ctrl+C"))

            copy_row = tk.Frame(panel, bg=bg)
            copy_row.pack(anchor="w", pady=(ui.space_xs, 0))
            copy_button = button(copy_row, tr("Copiar"), lambda: copy(details, copied), resolved=ui)
            copy_button.pack(side="left")
            copy_hint = tk.Label(copy_row, text="", font=ui.caption(), bg=bg, fg=ui.text)
            copy_hint.pack(side="left", padx=(ui.space_sm, 0))

        def toggle():
            if panel.winfo_manager():
                panel.grid_remove()
                disclosure(toggle_button, tr("Detalhes"), False, ui)
            else:
                panel.grid(row=3, column=1, columnspan=2, sticky="ew", pady=(ui.space_sm, 0))
                disclosure(toggle_button, tr("Detalhes"), True, ui)

        toggle_button = button(actions, f"{tr('Detalhes')} ▾", toggle, "quiet", resolved=ui)
        toggle_button.pack(side="left", padx=(0, ui.space_sm))
    if dismiss:
        button(actions, tr("Fechar"), dismiss, "quiet", resolved=ui).pack(side="left")
    return bar


class MessageBar(tk.Frame):
    """One page's feedback slot: trouble as an InfoBar, everything else as one quiet line.

    The slot has no height until the page has something to say, and the next
    message replaces the last, so a page never stacks a history. ``attach``
    records where the slot sits (pack or grid options); :meth:`clear` takes it
    out again.
    """

    def __init__(self, parent, ui, *, copy=None):
        super().__init__(parent, bg=_surface_of(parent), bd=0, highlightthickness=0)
        self._ui, self._copy = ui, copy
        self._placer, self._slot = None, None
        self._title = None
        self.kind = None

    def attach(self, *, grid=False, **options):
        """Remember how the slot is placed whenever it shows (``grid=True`` for a grid cell)."""
        self._placer, self._slot = ("grid" if grid else "pack"), options

    def _show(self, kind, widget):
        if self._slot is None:
            raise ValueError("attach() the message bar before showing a message")
        for child in self.winfo_children():
            if child is not widget:
                child.destroy()
        self.kind = kind
        widget.pack(fill="x")
        if not self.winfo_manager():
            getattr(self, self._placer)(**self._slot)

    def problem(self, tone, title, body, *, action=None, details=None, when=None, dismiss=None):
        """Show trouble: tone, what happened, what it affects, one action, Detalhes."""
        bar = info_bar(self, self._ui, tone, title, body, action=action, details=details,
                       copy=self._copy, when=when, dismiss=dismiss)
        self._title = bar.title_label
        self._show("problem", bar)

    def retitle(self, title):
        """Rewrite the title of the trouble on show without rebuilding the bar."""
        if self.kind == "problem" and self._title is not None:
            self._title.configure(text=title)

    def receipt(self, text, when=None):
        """Show one quiet line for a routine outcome, with the local time."""
        line = text.rstrip(".") + (" · " + local_time_text(when) if when is not None else "")
        label = tk.Label(self, text=line, font=self._ui.caption(), bg=self.cget("bg"),
                         fg=self._ui.text_muted, anchor="w", justify="left", width=1,
                         wraplength=360)
        label.bind("<Configure>", lambda event: label.configure(wraplength=max(event.width, 120)))
        self._show("receipt", label)

    def clear(self):
        for child in self.winfo_children():
            child.destroy()
        self.kind, self._title = None, None
        if self.winfo_manager():
            (self.grid_remove if self._placer == "grid" else self.pack_forget)()


class StatusChip(tk.Frame):
    """A tone dot beside one written state ("● Gravando · 00:45:54").

    The dot is drawn, not typed, so it scales with the text and never depends on
    a glyph face; the written state carries the meaning, the dot only the tone.
    """

    def __init__(self, parent, ui, *, bg, font, wraplength=360):
        super().__init__(parent, bg=bg)
        self._ui = ui
        size = max(8, int(tkfont.Font(root=parent, font=font).metrics("linespace") * 0.5))
        self._dot = tk.Canvas(self, width=size, height=size, bg=bg, bd=0, highlightthickness=0)
        self._shape = self._dot.create_oval(1, 1, size - 1, size - 1, width=0)
        self._dot.pack(side="left", padx=(0, ui.space_sm))
        self.label = tk.Label(self, text="", font=font, bg=bg, fg=ui.text_strong, anchor="w",
                              justify="left", width=1, wraplength=wraplength)
        self.label.bind("<Configure>",
                        lambda event: self.label.configure(wraplength=max(event.width, 120)))
        self.label.pack(side="left", fill="x", expand=True)

    def set(self, text, tone):
        """Write ``text`` and color the dot with the ``Theme`` color named ``tone``."""
        self.label.configure(text=text)
        self._dot.itemconfigure(self._shape, fill=getattr(self._ui, tone))


# ---------------------------------------------------------------------------
# Runtime resolution
# ---------------------------------------------------------------------------

def appearance_kind(luminance):
    """Classify a window-background luminance (0..1) as 'light' or 'dark'."""
    return "dark" if luminance < 0.5 else "light"


def _relative_luminance(rgb16):
    # ``winfo_rgb`` answers in 16-bit channels; a plain average is enough to
    # tell Aqua's near-black window background from its near-white one.
    return sum(rgb16) / (3.0 * 65535.0)


def normalize_preference(value):
    """Return ``system``, ``light`` or ``dark`` for persisted input.

    Dark is the default appearance; an unset or unreadable value gets it, and
    an explicit stored ``system`` or ``light`` is kept.
    """
    return value if value in {"system", "light", "dark"} else "dark"


def _windows_apps_use_light_theme():
    """Read the Windows app-theme preference; return ``None`` if unavailable."""
    try:
        import winreg

        path = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            value, _kind = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return bool(value)
    except (ImportError, OSError, TypeError, ValueError):
        return None


def _probe_kind(widget, system):
    if system == "windows":
        light = _windows_apps_use_light_theme()
        return "windows" if light is not False else "dark"
    if system != "darwin":
        return "light"
    try:
        return appearance_kind(
            _relative_luminance(widget.winfo_rgb("systemWindowBackgroundColor"))
        )
    except Exception:
        # An appearance we cannot read is not a reason to ship an unreadable
        # window: the light palette is the historical behavior.
        return "light"


def system_appearance_changed(widget, resolved):
    if resolved.system == "windows":
        contrast = windows_high_contrast()
        if bool(contrast) != resolved.high_contrast:
            return True
        if contrast and (resolved.surface, resolved.text, resolved.select_bg, resolved.select_fg) != (
                contrast["background"], contrast["text"], contrast["selection"], contrast["selected_text"]):
            return True
    return resolved.preference == "system" and _probe_kind(widget, resolved.system) != resolved.kind


def _probe_default_size(widget):
    try:
        return int(tkfont.nametofont("TkDefaultFont", root=widget).actual("size"))
    except Exception:
        return None


def build_theme(kind, system=None, default_size=None, preference="system",
                families=None, px_scale=1.0, high_contrast=None):
    """Assemble a :class:`Theme` from resolved platform and appearance state.

    macOS system mode keeps Aqua's dynamic color names. A fixed light/dark
    preference must use the opaque palette instead; otherwise Aqua resolves
    those names from the OS appearance and silently overrides the user's
    explicit choice.
    """
    system = system or current_os()
    preference = normalize_preference(preference)
    colors = palette(kind, system)
    if system == "darwin" and preference != "system":
        colors = dict(_DARK if kind == "dark" else _LIGHT)
        colors.update(_MAC_NATIVE)
    if system == "windows" and high_contrast:
        colors = dict(colors)
        for token in colors:
            if token in ("select_bg", "accent", "accent_active", "accent_mark", "focus_ring"):
                colors[token] = high_contrast["selection"]
            elif token in ("select_fg", "text_on_accent"):
                colors[token] = high_contrast["selected_text"]
            elif token == "disabled_fg":
                colors[token] = high_contrast["disabled"]
            elif token == "link":
                colors[token] = high_contrast["link"]
            elif token.endswith("_bg") or token in (
                    "surface", "surface_alt", "surface_alt_active", "surface_hover", "card",
                    "field", "field_hover", "control", "control_active"):
                colors[token] = high_contrast["background"]
            else:
                colors[token] = high_contrast["text"]
    resolved = Theme(
        kind=kind,
        system=system,
        preference=preference,
        faces=font_faces(system, families),
        emoji_family=emoji_family(system),
        mono_family=mono_family(system, families),
        symbol_family=symbol_family(system),
        size_delta=size_delta(system, default_size),
        colors=colors,
        px_scale=px_scale,
    )
    resolved.icon_family = icon_family(system, families)
    resolved.high_contrast = bool(system == "windows" and high_contrast)
    return resolved


_current = None


def bind(widget=None, system=None, preference="system"):
    """Resolve the theme against ``widget`` and cache it. Returns the theme.

    Called by every top-level window builder, so a window opened after the user
    switched appearance is built from the new palette.
    """
    global _current
    system = system or current_os()
    preference = normalize_preference(preference)
    if preference == "system":
        if widget is None and system == "darwin":
            kind = "light"
        else:
            kind = _probe_kind(widget, system)
    else:
        kind = preference
    families = None
    px_scale = 1.0
    if widget is not None:
        families = frozenset(tkfont.families(widget))
        # Tk's pixels per point; 4/3 at 96 DPI, where the ramp is defined.
        px_scale = float(widget.tk.call("tk", "scaling")) * 0.75
    _current = build_theme(
        kind,
        system,
        _probe_default_size(widget) if widget else None,
        preference=preference,
        families=families,
        px_scale=px_scale,
        high_contrast=windows_high_contrast() if system == "windows" else None,
    )
    if widget is not None and system == "windows":
        _apply_default_fonts(widget, _current)
    return _current


def _apply_default_fonts(widget, ui):
    """Point Tk's default faces at the body role.

    Comboboxes, unstyled labels and entries take ``TkTextFont`` or
    ``TkDefaultFont`` rather than a font option, so without this they keep the
    9pt system face and sit below the Compact 32px height.
    """
    family, size = ui.body()[:2]
    for name in ("TkDefaultFont", "TkTextFont"):
        tkfont.nametofont(name, root=widget).configure(family=family, size=size, weight="normal")


def apply_window_chrome(window, resolved=None):
    """Ask Windows 11 for matching title-bar colors and rounded corners."""
    ui = resolved or theme()
    if ui.system != "windows":
        return False
    try:
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id()) or window.winfo_id()
        enabled = ctypes.c_int(1 if ui.is_dark else 0)
        rounded = ctypes.c_int(2)  # DWMWCP_ROUND
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, 20, ctypes.byref(enabled), ctypes.sizeof(enabled),
        )
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, 33, ctypes.byref(rounded), ctypes.sizeof(rounded),
        )
        return True
    except Exception:
        return False


def theme():
    """Return the cached theme, resolving a widget-free default on first use."""
    if _current is None:
        return bind(None)
    return _current


def reset():
    """Drop the cached theme (tests)."""
    global _current
    _current = None


def ui_font(size=BODY_FONT_SIZE, weight=None):
    return theme().font(size, weight)


def emoji_font(size=BODY_FONT_SIZE, weight=None):
    return theme().emoji_font(size, weight)


def mono_font(size=BODY_FONT_SIZE, weight=None):
    return theme().mono_font(size, weight)
