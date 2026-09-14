"""Parse and observe push-to-talk chords without touching the expansion listener.

The expansion listener stays listen-only and must never swallow keys. Voice
uses a dedicated observer: OS filters suppress only the final key of a
configured chord so it does not type into the target. Press and release are
both required; auto-repeat is ignored by the controller, not here.
"""

from pynput.keyboard import Key, KeyCode

from platform_support import current_os


DEFAULT_DICTATION_HOTKEY = "ctrl+alt+space"
DEFAULT_COMMAND_HOTKEY = "ctrl+alt+shift+space"

MODE_DICTATION = "dictation"
MODE_COMMAND = "command"

_MODIFIER_ALIASES = {
    "ctrl": "ctrl",
    "control": "ctrl",
    "alt": "alt",
    "option": "alt",
    "shift": "shift",
    "win": "win",
    "super": "win",
    "meta": "win",
    "cmd": "cmd",
    "command": "cmd",
}

_MODIFIER_KEYS = {
    Key.ctrl: "ctrl",
    Key.ctrl_l: "ctrl",
    Key.ctrl_r: "ctrl",
    Key.alt: "alt",
    Key.alt_l: "alt",
    Key.alt_r: "alt",
    Key.alt_gr: "alt",
    Key.shift: "shift",
    Key.shift_l: "shift",
    Key.shift_r: "shift",
    Key.cmd: "cmd",
    Key.cmd_l: "cmd",
    Key.cmd_r: "cmd",
}

# macOS event taps expose physical key codes rather than pynput ``Key``
# objects. These are the stable ANSI keyboard codes for the keys accepted by
# ``parse_chord``. Unicode extraction below fills in keys on other layouts
# when Quartz can provide it.
_DARWIN_KEYCODES = {
    0: "a",
    1: "s",
    2: "d",
    3: "f",
    4: "h",
    5: "g",
    6: "z",
    7: "x",
    8: "c",
    9: "v",
    11: "b",
    12: "q",
    13: "w",
    14: "e",
    15: "r",
    16: "y",
    17: "t",
    18: "1",
    19: "2",
    20: "3",
    21: "4",
    22: "6",
    23: "5",
    24: "=",
    25: "9",
    26: "7",
    27: "-",
    28: "8",
    29: "0",
    30: "]",
    31: "o",
    32: "u",
    33: "[",
    34: "i",
    35: "p",
    36: "enter",
    37: "l",
    38: "j",
    39: "'",
    40: "k",
    41: ";",
    42: "\\",
    43: ",",
    44: "/",
    45: "n",
    46: "m",
    47: ".",
    48: "tab",
    49: "space",
    50: "`",
    53: "esc",
    64: "f17",
    79: "f18",
    80: "f19",
    90: "f20",
    96: "f5",
    97: "f6",
    98: "f7",
    99: "f3",
    100: "f8",
    101: "f9",
    103: "f11",
    105: "f13",
    106: "f16",
    107: "f14",
    109: "f10",
    111: "f12",
    113: "f15",
    118: "f4",
    120: "f2",
    122: "f1",
}

_WIN32_VK_NAMES = {
    0x09: "tab",
    0x0D: "enter",
    0x1B: "esc",
    0x20: "space",
    **{0x30 + index: str(index) for index in range(10)},
    **{0x41 + index: chr(0x61 + index) for index in range(26)},
    **{0x60 + index: str(index) for index in range(10)},
    0x6D: "-",
    0x6E: ".",
    0x6F: "/",
    **{0x70 + index: f"f{index + 1}" for index in range(24)},
    0xBA: ";",
    0xBB: "=",
    0xBC: ",",
    0xBD: "-",
    0xBE: ".",
    0xBF: "/",
    0xC0: "`",
    0xDB: "[",
    0xDC: "\\",
    0xDD: "]",
    0xDE: "'",
}

_WIN32_KEYUP_MESSAGES = frozenset({0x0101, 0x0105})
_SUPPORTED_SINGLE_KEYS = frozenset(
    name for name in _WIN32_VK_NAMES.values() if len(name) == 1
)

try:
    _MODIFIER_KEYS[Key.cmd] = "cmd"
except AttributeError:
    pass


class Chord:
    """A modifier set plus one non-modifier key."""

    __slots__ = ("modifiers", "key", "spec")

    def __init__(self, modifiers, key, spec):
        self.modifiers = frozenset(modifiers)
        self.key = key
        self.spec = spec

    def __eq__(self, other):
        return (
            isinstance(other, Chord)
            and self.modifiers == other.modifiers
            and self.key == other.key
        )

    def __hash__(self):
        return hash((self.modifiers, self.key))


def parse_chord(spec):
    """Parse ``ctrl+alt+space`` into a Chord. Raises ValueError if unusable."""
    if not isinstance(spec, str) or not spec.strip():
        raise ValueError("empty hotkey")
    parts = [part.strip().lower() for part in spec.split("+") if part.strip()]
    if len(parts) < 2:
        raise ValueError("hotkey needs a modifier and a key")
    key = parts[-1]
    if key == "escape":
        key = "esc"
    if key in _MODIFIER_ALIASES:
        raise ValueError("hotkey cannot end on a modifier")
    if (
        len(key) == 1
        and current_os() == "windows"
        and key not in _SUPPORTED_SINGLE_KEYS
    ):
        raise ValueError(f"unsupported hotkey key: {key}")
    if len(key) != 1 and key not in {"space", "enter", "tab", "esc", "escape"}:
        if (
            not key.startswith("f")
            or not key[1:].isdigit()
            or not 1 <= int(key[1:]) <= 24
        ):
            raise ValueError(f"unsupported hotkey key: {key}")
    modifiers = []
    for part in parts[:-1]:
        alias = _MODIFIER_ALIASES.get(part)
        if alias is None:
            raise ValueError(f"unsupported modifier: {part}")
        modifiers.append(alias)
    if not modifiers:
        raise ValueError("hotkey needs a modifier")
    return Chord(modifiers, key, "+".join([*modifiers, key]))


def _key_name(key):
    modifier = _MODIFIER_KEYS.get(key)
    if modifier is not None:
        return modifier
    if key in (Key.space,):
        return "space"
    if key in (Key.enter,):
        return "enter"
    if key in (Key.tab,):
        return "tab"
    if key in (Key.esc,):
        return "esc"
    char = getattr(key, "char", None)
    if isinstance(char, str) and char:
        if char == " ":
            return "space"
        return char.lower()
    vk = getattr(key, "vk", None)
    if vk == 0x20:
        return "space"
    name = getattr(key, "name", None)
    if isinstance(name, str) and name:
        lowered = name.lower()
        if lowered in _MODIFIER_ALIASES:
            return _MODIFIER_ALIASES[lowered]
        return lowered
    return None


def chord_is_held(chord, held_modifiers, key_name):
    """True when ``key_name`` completes ``chord`` given the held modifiers."""
    return key_name == chord.key and held_modifiers >= chord.modifiers


class VoiceHotkeyMonitor:
    """Dedicated pynput listener that reports press/release of configured chords.

    Listener callbacks only update Python state and invoke the supplied
    press/release handlers. They never open devices, load models, or touch Tk.
    """

    def __init__(self, dictation_chord, command_chord, on_press, on_release, on_escape=None):
        self.dictation_chord = dictation_chord
        self.command_chord = command_chord
        self._on_press = on_press
        self._on_release = on_release
        self._on_escape = on_escape
        self._held = set()
        self._active_mode = None
        self._darwin_suppressed_key = None
        self._win32_suppressed_key = None
        self._listener = None

    def start(self):
        from pynput import keyboard

        if self._listener is not None:
            return
        kwargs = {
            "on_press": self._handle_press,
            "on_release": self._handle_release,
        }
        system = current_os()
        if system == "windows":
            kwargs["win32_event_filter"] = self._win32_filter
        elif system == "darwin":
            kwargs["darwin_intercept"] = self._darwin_intercept
        # Linux deliberately stays listen-only: it has no selective global
        # filter in pynput, and suppressing the whole listener would swallow
        # ordinary typing.
        self._listener = keyboard.Listener(**kwargs)
        self._listener.start()

    def stop(self):
        listener = self._listener
        self._listener = None
        self._held.clear()
        self._active_mode = None
        self._darwin_suppressed_key = None
        self._win32_suppressed_key = None
        if listener is not None:
            try:
                listener.stop()
            except Exception:
                pass

    def _mode_for_key(self, key_name):
        if key_name is None:
            return None
        matches = []
        if chord_is_held(self.command_chord, self._held, key_name):
            matches.append((len(self.command_chord.modifiers), MODE_COMMAND))
        if chord_is_held(self.dictation_chord, self._held, key_name):
            matches.append((len(self.dictation_chord.modifiers), MODE_DICTATION))
        if not matches:
            return None
        # A user may assign the longer chord to either action. Prefer the most
        # specific match; keep command as the deterministic tie-breaker.
        return max(matches, key=lambda item: (item[0], item[1] == MODE_COMMAND))[1]

    def _handle_press(self, key):
        name = _key_name(key)
        if name in {"esc", "escape"}:
            if self._active_mode is None:
                mode = self._mode_for_key("esc")
                if mode is not None:
                    self._active_mode = mode
                    self._darwin_suppressed_key = "esc"
                    self._on_press(mode)
                    return
            if self._on_escape is not None:
                self._on_escape()
            return
        if name in _MODIFIER_ALIASES.values():
            self._held.add(name)
            return
        if self._active_mode is not None:
            return
        mode = self._mode_for_key(name)
        if mode is None:
            return
        self._active_mode = mode
        self._darwin_suppressed_key = self._chord_for_mode(mode).key
        self._on_press(mode)

    def _handle_release(self, key):
        name = _key_name(key)
        if name in _MODIFIER_ALIASES.values():
            self._held.discard(name)
            if (
                self._active_mode is not None
                and name in self._chord_for_mode(self._active_mode).modifiers
            ):
                # Releasing any required modifier ends the hold. An unrelated
                # modifier may be released while the chord remains active.
                mode = self._active_mode
                self._active_mode = None
                self._on_release(mode)
            return
        if self._active_mode is None:
            return
        expected = self._chord_for_mode(self._active_mode).key
        if name == expected:
            mode = self._active_mode
            self._active_mode = None
            self._on_release(mode)

    def _chord_for_mode(self, mode):
        return self.command_chord if mode == MODE_COMMAND else self.dictation_chord

    @staticmethod
    def _darwin_key_name(event):
        """Return the configured-key name represented by a Quartz event."""
        try:
            key_name = getattr(event, "key_name", None)
        except Exception:
            key_name = None
        if isinstance(key_name, str) and key_name:
            return key_name.lower()
        try:
            from Quartz import (
                CGEventGetIntegerValueField,
                CGEventKeyboardGetUnicodeString,
                kCGKeyboardEventKeycode,
            )

            keycode = int(
                CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode)
            )
        except (ImportError, AttributeError, TypeError, ValueError, OSError):
            return None
        name = _DARWIN_KEYCODES.get(keycode)
        if name is not None:
            return name
        try:
            result = CGEventKeyboardGetUnicodeString(event, 4, None, None)
            chars = result[1] if isinstance(result, tuple) else result
        except (AttributeError, TypeError, ValueError, OSError):
            return None
        if isinstance(chars, str) and chars:
            return _key_name(KeyCode.from_char(chars[0]))
        return None

    def _darwin_intercept(self, event_type, event):
        """Suppress only an armed chord's final key down/up on macOS."""
        try:
            from Quartz import kCGEventKeyDown, kCGEventKeyUp
        except (ImportError, AttributeError):
            return event
        if event_type not in (kCGEventKeyDown, kCGEventKeyUp):
            return event
        name = self._darwin_key_name(event)
        if name is None:
            return event
        if event_type == kCGEventKeyDown:
            if self._darwin_suppressed_key == name or self._mode_for_key(name):
                self._darwin_suppressed_key = name
                return None
            return event
        if self._darwin_suppressed_key == name:
            self._darwin_suppressed_key = None
            return None
        return event

    def _win32_filter(self, msg, data):
        # Swallow only the final key of an armed chord so it does not type.
        vk = getattr(data, "vkCode", None)
        name = _WIN32_VK_NAMES.get(vk) if isinstance(vk, int) else None
        listener = self._listener
        if listener is None or name is None:
            return True
        if self._win32_suppressed_key == name:
            try:
                listener.suppress_event()
            except Exception:
                pass
            if msg in _WIN32_KEYUP_MESSAGES:
                self._win32_suppressed_key = None
            return True
        if self._mode_for_key(name) is not None:
            self._win32_suppressed_key = name
            try:
                listener.suppress_event()
            except Exception:
                pass
        return True
