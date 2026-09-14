"""Per-OS palette and font resolution for the Tkinter windows.

The manager GUI was written Windows-first with a hardcoded light palette and
``Segoe UI`` everywhere. On macOS that produced unreadable windows: Aqua themes
the widgets the app leaves uncolored according to the *system* appearance, so a
dark-mode ``tk.Entry`` renders black-on-black inside a ``#F4F6FA`` frame, and
``Segoe UI`` silently substitutes to a different font with different metrics.

This module is the seam. Call sites ask for a semantic token
(``theme().surface``, ``theme().text``) and a size (``ui_font(9, "bold")``)
instead of naming a color or a font family:

* **Windows** resolves to the app's Fluent-inspired opaque palette. It keeps
  the native selection behavior of controls the app does not paint itself.
* **macOS** resolves to Aqua's dynamic system colors (``systemTextColor`` and
  friends), which follow the light/dark appearance natively, plus a small set
  of derived grays for the tokens Aqua only exposes with an alpha channel --
  Tk drops the alpha and hands back pure white, which would be invisible.
* **Linux** reuses the same literals: Aqua's color names do not exist on X11,
  and the GUI is untested there anyway.

Resolution needs a live widget on macOS (the system colors are queried through
Tk), so :func:`bind` is called by each top-level window builder and the result
is cached until the next ``bind``. Re-binding per window is deliberate: it is
what makes reopening the manager pick up an appearance the user changed while
it was closed. A live switch with the window open is not handled -- the tokens
mapped to system color *names* repaint themselves, the derived grays do not.
"""

from tkinter import font as tkfont

from platform_support import current_os


# The GUI's body size, in the Windows point scale every ``font=`` call uses.
# Other platforms shift their sizes by the distance between this and their own
# system default, so 9 stays "body text" rather than "unreadably small".
BODY_FONT_SIZE = 9

_WINDOWS_FAMILY = "Segoe UI"
_WINDOWS_EMOJI_FAMILY = "Segoe UI Emoji"
# Not in ``font.families()`` -- it is the hidden system font Tk resolves
# ``TkDefaultFont`` to -- but Tk accepts it by name in a font spec.
_MAC_FAMILY = ".AppleSystemUIFont"
_MAC_EMOJI_FAMILY = "Apple Color Emoji"
_LINUX_FAMILY = "TkDefaultFont"

# Monospace, for the trigger columns.
_MONO_FAMILIES = {
    "windows": "Consolas",
    "darwin": "Menlo",
    "linux": "TkFixedFont",
}

# The toolbar's glyph buttons. Windows needs the dedicated symbol face; the
# macOS system font already carries the glyphs.
_SYMBOL_FAMILIES = {
    "windows": "Segoe UI Symbol",
    "darwin": _MAC_FAMILY,
    "linux": _LINUX_FAMILY,
}


class Theme:
    """A resolved palette plus the font family/size shift for this platform."""

    __slots__ = (
        "kind", "system", "family", "emoji_family", "mono_family", "symbol_family",
        "size_delta",
        "surface", "surface_alt", "surface_alt_active", "surface_hover",
        "card", "field", "field_hover",
        "text", "text_strong", "text_muted", "text_on_accent",
        "border", "divider",
        "accent", "accent_active", "danger", "danger_active", "focus_ring",
        "link", "warning", "success",
        "select_bg", "select_fg", "text_native", "tab_unselected_fg",
        "space_xs", "space_sm", "space_md", "space_lg", "space_xl",
        "tree_row_height",
    )

    def __init__(self, kind, system, family, emoji_family, mono_family,
                 symbol_family, size_delta, colors):
        self.kind = kind
        self.system = system
        self.family = family
        self.emoji_family = emoji_family
        self.mono_family = mono_family
        self.symbol_family = symbol_family
        self.size_delta = size_delta
        for name, value in colors.items():
            setattr(self, name, value)
        self.space_xs = 4
        self.space_sm = 8
        self.space_md = 12
        self.space_lg = 16
        self.space_xl = 24
        self.tree_row_height = 30

    def font(self, size=BODY_FONT_SIZE, weight=None):
        """Return a font spec tuple for the GUI's shared family."""
        return _spec(self.family, size + self.size_delta, weight)

    def emoji_font(self, size=BODY_FONT_SIZE, weight=None):
        return _spec(self.emoji_family, size + self.size_delta, weight)

    def mono_font(self, size=BODY_FONT_SIZE, weight=None):
        return _spec(self.mono_family, size + self.size_delta, weight)

    @property
    def is_dark(self):
        return self.kind == "dark"

    def entry_colors(self):
        """Colors every text-entry widget needs so Aqua cannot theme it blind.

        A ``tk.Entry``/``tk.Text`` that sets neither ``bg`` nor ``fg`` inherits
        the system appearance while its parent frame carries an explicit color;
        that mismatch is what renders as a black box in dark mode.

        Empty off macOS, and that is the point: Win32's own defaults for these
        widgets are already right (system window background, system window
        text, the user's highlight color). Pinning them there would swap the
        user's selection color for the app's blue -- a Windows regression for
        no gain.
        """
        if self.system != "darwin":
            return {}
        return {
            "bg": self.field,
            "fg": self.text,
            "insertbackground": self.text,
            "selectbackground": self.select_bg,
            "selectforeground": self.select_fg,
            "disabledbackground": self.surface_alt,
            "disabledforeground": self.text_muted,
        }

    def text_colors(self):
        """:meth:`entry_colors` for ``tk.Text``, which spells 'disabled' differently."""
        colors = self.entry_colors()
        if not colors:
            return colors
        colors.pop("disabledbackground", None)
        colors.pop("disabledforeground", None)
        colors["inactiveselectbackground"] = self.select_bg
        return colors

    def listbox_colors(self):
        """See :meth:`entry_colors` -- macOS only, for the same reason."""
        if self.system != "darwin":
            return {}
        return {
            "bg": self.field,
            "fg": self.text,
            "selectbackground": self.select_bg,
            "selectforeground": self.select_fg,
        }

    # Buttons and checkboxes are the widgets Aqua draws *itself*, and it draws
    # them from the appearance rather than from what the app asks for. It
    # ignores ``-background`` outright and keeps its own light bezel, but it
    # does honour ``-foreground`` -- so a well-meant `fg=systemTextColor` puts
    # white text on that light bezel and the button renders as a blank box
    # (seen on macOS 15 / Tk 9.0). Every button color helper therefore answers
    # nothing on macOS: the native control is already correct in both
    # appearances, and the only way to break it is to paint on it.

    def checkbutton_colors(self, bg):
        """Colors for a checkbox sitting on ``bg``. Native on macOS."""
        if self.system == "darwin":
            return {}
        return {
            "bg": bg,
            "fg": self.text_native,
            "activebackground": bg,
            "activeforeground": self.text_native,
        }

    def toolbar_frame_colors(self):
        """``bg`` for the formatting-toolbar frame (and its stacked status row).

        The toolbar belongs to the editor surface, so it always uses ``card``.
        Toolbar buttons receive their own foreground and interaction colors,
        avoiding the old Win32 white-on-white regression.
        """
        return {"bg": self.card}

    def status_label_options(self):
        """Font and foreground for the format-status label.

        The status is secondary UI, so it shares the app's body family and
        muted semantic color on every platform. The label's ``bg`` is passed
        by the caller (the toolbar's own background).
        """
        return {"font": self.font(8), "fg": self.text_muted}

    def toolbar_button_colors(self, bg):
        """Colors for the flat glyph buttons in the formatting toolbar."""
        if self.system == "darwin":
            return {}
        return {
            "bg": bg,
            "fg": self.text_native,
            "activebackground": self.surface_hover,
            "activeforeground": self.text_native,
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

        macOS needs a wider default: Aqua's native buttons have a minimum
        width the flat Win32 ones do not, so the editor pane that fits its
        formatting toolbar in 433px on Windows needs ~490px here.
        """
        if self.system == "darwin":
            return ("1140x760", 980, 600)
        return ("1080x720", 900, 580)

    @property
    def stacked_toolbar_status(self):
        """True where the format status must sit below the toolbar, not beside it.

        Nine native buttons already fill the editor pane on macOS; leaving the
        status label on the same row pushes the last button off the edge.
        """
        return self.system == "darwin"

    def button_chrome(self, compact=False):
        """Platform-safe geometry and focus treatment for manager buttons."""
        if self.system == "darwin":
            return {}
        return {
            "relief": "flat",
            "bd": 0,
            "padx": 8 if compact else 12,
            "pady": 4 if compact else 6,
            "highlightthickness": 1,
            "highlightbackground": self.border,
            "highlightcolor": self.focus_ring,
            "cursor": "hand2",
        }

    def button_colors(self, accent=False, danger=False):
        """Colors for the app's tinted buttons. Native on macOS."""
        if self.system == "darwin":
            return {}
        if danger:
            return {
                "bg": self.danger,
                "fg": self.text_on_accent,
                "activebackground": self.danger_active,
                "activeforeground": self.text_on_accent,
            }
        if accent:
            return {
                "bg": self.accent,
                "fg": self.text_on_accent,
                "activebackground": self.accent_active,
                "activeforeground": self.text_on_accent,
            }
        return {
            "bg": self.surface_alt,
            "fg": self.text_native,
            "activebackground": self.surface_alt_active,
            "activeforeground": self.text_native,
        }


def _spec(family, size, weight=None):
    return (family, size) if weight is None else (family, size, weight)


# ---------------------------------------------------------------------------
# Palettes
# ---------------------------------------------------------------------------

# Fluent-inspired opaque surfaces. Tk cannot reproduce Mica or Acrylic
# reliably across platforms, so hierarchy comes from restrained contrast,
# spacing and selection states instead.
_LIGHT = {
    "surface": "#F3F3F3",
    "surface_alt": "#FAFAFA",
    "surface_alt_active": "#EDEDED",
    "surface_hover": "#EBEBEB",
    "card": "#FFFFFF",
    "field": "#FFFFFF",
    "field_hover": "#F5F9FD",
    "text": "#1B1B1B",
    "text_strong": "#242424",
    "text_muted": "#616161",
    "text_on_accent": "#FFFFFF",
    "border": "#E1E1E1",
    "divider": "#D6D6D6",
    "accent": "#0067C0",
    "accent_active": "#005A9E",
    "danger": "#C42B1C",
    "danger_active": "#A4262C",
    "focus_ring": "#005FB8",
    "link": "#005FB8",
    "warning": "#8A4B00",
    "success": "#0F7B0F",
    "select_bg": "#DCEEFF",
    "select_fg": "#1B1B1B",
    # Foreground for widgets the pre-change GUI left uncolored. Resolved to
    # each platform's own default so filling it in changes nothing there.
    "text_native": "#1B1B1B",
    # Unselected notebook-tab label. The selected tab uses the accent token.
    "tab_unselected_fg": "#4A4A4A",
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
    # The light-mode blues lose contrast against a dark surface.
    "link": "#6BA0FF",
    "warning": "#E0A458",
    "success": "#4ADE80",
    "danger": "#FF6961",
    "danger_active": "#FF453A",
    "focus_ring": "#6BA0FF",
}


def palette(kind, system=None):
    """Return the color token map for ``kind`` in {'windows', 'light', 'dark'}.

    Pure: no Tk, no platform probing. ``'windows'`` returns the opaque Fluent
    palette; it is also what Linux gets, since Aqua's color names do not
    resolve there. ``system`` only selects the platform's native defaults for
    the ``*_native`` tokens.
    """
    colors = dict(_LIGHT)
    if kind == "windows":
        if system == "windows":
            colors.update(_WINDOWS_NATIVE)
        return colors
    colors.update(_MAC_SYSTEM)
    colors.update(_MAC_NATIVE)
    colors.update(_MAC_DARK_OVERRIDES if kind == "dark" else _MAC_LIGHT_OVERRIDES)
    return colors


def font_family(system=None):
    system = system or current_os()
    if system == "windows":
        return _WINDOWS_FAMILY
    if system == "darwin":
        return _MAC_FAMILY
    return _LINUX_FAMILY


def emoji_family(system=None):
    system = system or current_os()
    if system == "windows":
        return _WINDOWS_EMOJI_FAMILY
    if system == "darwin":
        return _MAC_EMOJI_FAMILY
    return _LINUX_FAMILY


def mono_family(system=None):
    return _MONO_FAMILIES.get(system or current_os(), _MONO_FAMILIES["linux"])


def symbol_family(system=None):
    return _SYMBOL_FAMILIES.get(system or current_os(), _SYMBOL_FAMILIES["linux"])


def size_delta(system=None, default_size=None):
    """Shift between the Windows point scale and this platform's system size.

    Windows is the reference (0). Elsewhere the GUI's body size is pinned to
    the platform's own ``TkDefaultFont`` size so a 9 pt Windows label does not
    render two points below every native control around it.
    """
    if (system or current_os()) == "windows" or not default_size:
        return 0
    return int(default_size) - BODY_FONT_SIZE


def ttk_theme_preference(system=None):
    """ttk themes to try, best first. The last is Tk's built-in fallback."""
    system = system or current_os()
    if system == "windows":
        return ("vista", "winnative", "default")
    if system == "darwin":
        return ("aqua", "clam", "default")
    return ("clam", "default")


def apply_ttk_theme(style, system=None):
    """Select the best available ttk theme. Returns the theme actually in use."""
    try:
        available = set(style.theme_names())
    except Exception:
        return None
    for name in ttk_theme_preference(system):
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


def _probe_kind(widget, system):
    if system != "darwin":
        # Linux takes the literal palette too: Aqua's system color names do not
        # exist on X11 and Tk raises on an unknown color.
        return "windows"
    try:
        return appearance_kind(
            _relative_luminance(widget.winfo_rgb("systemWindowBackgroundColor"))
        )
    except Exception:
        # An appearance we cannot read is not a reason to ship an unreadable
        # window: the light palette is the historical behavior.
        return "light"


def _probe_default_size(widget):
    try:
        return int(tkfont.nametofont("TkDefaultFont", root=widget).actual("size"))
    except Exception:
        return None


def build_theme(kind, system=None, default_size=None):
    """Assemble a :class:`Theme`. Pure given ``kind``/``system``/``default_size``."""
    system = system or current_os()
    return Theme(
        kind=kind,
        system=system,
        family=font_family(system),
        emoji_family=emoji_family(system),
        mono_family=mono_family(system),
        symbol_family=symbol_family(system),
        size_delta=size_delta(system, default_size),
        colors=palette(kind, system),
    )


_current = None


def bind(widget=None, system=None):
    """Resolve the theme against ``widget`` and cache it. Returns the theme.

    Called by every top-level window builder, so a window opened after the user
    switched appearance is built from the new palette.
    """
    global _current
    system = system or current_os()
    if widget is None:
        # No widget means no appearance probe; never guess dark, because
        # guessing wrong is exactly the unreadable case being fixed.
        _current = build_theme("light" if system == "darwin" else "windows", system)
        return _current
    _current = build_theme(
        _probe_kind(widget, system), system, _probe_default_size(widget)
    )
    return _current


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
