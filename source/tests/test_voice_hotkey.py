import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from voice_hotkey import (
    DEFAULT_COMMAND_HOTKEY,
    DEFAULT_DICTATION_HOTKEY,
    MODE_COMMAND,
    MODE_DICTATION,
    VoiceHotkeyMonitor,
    chord_is_held,
    parse_chord,
)
from voice_settings import resolve_voice_settings


class ParseChordTests(unittest.TestCase):
    def test_default_chords_parse(self):
        dictation = parse_chord(DEFAULT_DICTATION_HOTKEY)
        command = parse_chord(DEFAULT_COMMAND_HOTKEY)
        self.assertEqual(dictation.modifiers, frozenset({"ctrl", "alt"}))
        self.assertEqual(dictation.key, "space")
        self.assertEqual(command.modifiers, frozenset({"ctrl", "alt", "shift"}))

    def test_rejects_empty_and_modifier_only(self):
        for spec in ("", "space", "ctrl", "ctrl+alt"):
            with self.subTest(spec=spec):
                with self.assertRaises(ValueError):
                    parse_chord(spec)

    def test_unknown_modifier_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_chord("hyper+space")

    def test_function_key_range_matches_windows_virtual_keys(self):
        self.assertEqual(parse_chord("ctrl+f24").key, "f24")
        for key in ("f0", "f25"):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    parse_chord(f"ctrl+{key}")

    def test_rejects_single_keys_the_windows_filter_cannot_suppress(self):
        with mock.patch("voice_hotkey.current_os", return_value="windows"):
            for key in ("é", "!", "@"):
                with self.subTest(key=key):
                    with self.assertRaises(ValueError):
                        parse_chord(f"ctrl+{key}")

    def test_macos_and_linux_accept_unicode_single_keys(self):
        for system in ("darwin", "linux"):
            with self.subTest(system=system), mock.patch(
                "voice_hotkey.current_os", return_value=system
            ):
                self.assertEqual(parse_chord("ctrl+é").key, "é")

    def test_escape_alias_is_canonicalized(self):
        self.assertEqual(parse_chord("ctrl+escape").spec, "ctrl+esc")


class ChordMatchTests(unittest.TestCase):
    def test_requires_every_modifier(self):
        chord = parse_chord("ctrl+alt+space")
        self.assertFalse(chord_is_held(chord, {"ctrl"}, "space"))
        self.assertTrue(chord_is_held(chord, {"ctrl", "alt"}, "space"))
        self.assertFalse(chord_is_held(chord, {"ctrl", "alt"}, "enter"))


class MonitorTests(unittest.TestCase):
    def test_press_and_release_report_the_matching_mode(self):
        events = []
        monitor = VoiceHotkeyMonitor(
            parse_chord("ctrl+alt+space"),
            parse_chord("ctrl+alt+shift+space"),
            on_press=lambda mode: events.append(("press", mode)),
            on_release=lambda mode: events.append(("release", mode)),
        )
        from pynput.keyboard import Key

        monitor._handle_press(Key.ctrl)
        monitor._handle_press(Key.alt)
        monitor._handle_press(Key.space)
        monitor._handle_release(Key.space)
        self.assertEqual(
            events,
            [("press", MODE_DICTATION), ("release", MODE_DICTATION)],
        )

    def test_configured_escape_chord_starts_voice_instead_of_cancelling(self):
        events = []
        monitor = VoiceHotkeyMonitor(
            parse_chord("ctrl+alt+esc"),
            parse_chord("ctrl+alt+shift+space"),
            on_press=lambda mode: events.append(("press", mode)),
            on_release=lambda mode: events.append(("release", mode)),
            on_escape=lambda: events.append(("cancel", None)),
        )
        from pynput.keyboard import Key

        monitor._handle_press(Key.ctrl)
        monitor._handle_press(Key.alt)
        monitor._handle_press(Key.esc)
        monitor._handle_release(Key.esc)

        self.assertEqual(
            events,
            [("press", MODE_DICTATION), ("release", MODE_DICTATION)],
        )

    def test_command_chord_wins_over_the_shorter_dictation_chord(self):
        events = []
        monitor = VoiceHotkeyMonitor(
            parse_chord("ctrl+alt+space"),
            parse_chord("ctrl+alt+shift+space"),
            on_press=lambda mode: events.append(mode),
            on_release=lambda mode: None,
        )
        from pynput.keyboard import Key

        monitor._handle_press(Key.ctrl)
        monitor._handle_press(Key.alt)
        monitor._handle_press(Key.shift)
        monitor._handle_press(Key.space)
        self.assertEqual(events, [MODE_COMMAND])

    def test_dictation_chord_can_be_more_specific_than_command(self):
        events = []
        monitor = VoiceHotkeyMonitor(
            parse_chord("ctrl+alt+shift+space"),
            parse_chord("ctrl+alt+space"),
            on_press=lambda mode: events.append(mode),
            on_release=lambda mode: None,
        )
        from pynput.keyboard import Key

        monitor._handle_press(Key.ctrl)
        monitor._handle_press(Key.alt)
        monitor._handle_press(Key.shift)
        monitor._handle_press(Key.space)
        self.assertEqual(events, [MODE_DICTATION])

    def test_auto_repeat_does_not_rearm_while_held(self):
        events = []
        monitor = VoiceHotkeyMonitor(
            parse_chord("ctrl+alt+space"),
            parse_chord("ctrl+alt+shift+space"),
            on_press=lambda mode: events.append("press"),
            on_release=lambda mode: events.append("release"),
        )
        from pynput.keyboard import Key

        monitor._handle_press(Key.ctrl)
        monitor._handle_press(Key.alt)
        monitor._handle_press(Key.space)
        monitor._handle_press(Key.space)
        self.assertEqual(events, ["press"])

    def test_releasing_required_modifier_ends_hold(self):
        events = []
        monitor = VoiceHotkeyMonitor(
            parse_chord("ctrl+alt+space"),
            parse_chord("ctrl+alt+shift+space"),
            on_press=lambda mode: events.append(("press", mode)),
            on_release=lambda mode: events.append(("release", mode)),
        )
        from pynput.keyboard import Key

        monitor._handle_press(Key.ctrl)
        monitor._handle_press(Key.alt)
        monitor._handle_press(Key.space)
        monitor._handle_release(Key.alt)
        monitor._handle_release(Key.alt)
        self.assertEqual(
            events,
            [("press", MODE_DICTATION), ("release", MODE_DICTATION)],
        )

    def test_final_key_release_then_modifier_release_is_once(self):
        events = []
        monitor = VoiceHotkeyMonitor(
            parse_chord("ctrl+alt+space"),
            parse_chord("ctrl+alt+shift+space"),
            on_press=lambda mode: events.append(("press", mode)),
            on_release=lambda mode: events.append(("release", mode)),
        )
        from pynput.keyboard import Key

        monitor._handle_press(Key.ctrl)
        monitor._handle_press(Key.alt)
        monitor._handle_press(Key.space)
        monitor._handle_release(Key.space)
        monitor._handle_release(Key.ctrl)
        self.assertEqual(
            events,
            [("press", MODE_DICTATION), ("release", MODE_DICTATION)],
        )

    def test_unrelated_extra_modifier_does_not_end_hold(self):
        events = []
        monitor = VoiceHotkeyMonitor(
            parse_chord("ctrl+alt+space"),
            parse_chord("ctrl+alt+shift+enter"),
            on_press=lambda mode: events.append(("press", mode)),
            on_release=lambda mode: events.append(("release", mode)),
        )
        from pynput.keyboard import Key

        monitor._handle_press(Key.ctrl)
        monitor._handle_press(Key.alt)
        monitor._handle_press(Key.shift)
        monitor._handle_press(Key.space)
        monitor._handle_release(Key.shift)
        self.assertEqual(events, [("press", MODE_DICTATION)])
        monitor._handle_release(Key.space)
        self.assertEqual(
            events,
            [("press", MODE_DICTATION), ("release", MODE_DICTATION)],
        )

    def test_overlapping_command_chord_ends_when_its_extra_modifier_releases(self):
        events = []
        monitor = VoiceHotkeyMonitor(
            parse_chord("ctrl+alt+space"),
            parse_chord("ctrl+alt+shift+space"),
            on_press=lambda mode: events.append(("press", mode)),
            on_release=lambda mode: events.append(("release", mode)),
        )
        from pynput.keyboard import Key

        monitor._handle_press(Key.ctrl)
        monitor._handle_press(Key.alt)
        monitor._handle_press(Key.shift)
        monitor._handle_press(Key.space)
        monitor._handle_release(Key.shift)
        monitor._handle_release(Key.space)
        self.assertEqual(
            events,
            [("press", MODE_COMMAND), ("release", MODE_COMMAND)],
        )


class ListenerFilterTests(unittest.TestCase):
    def _monitor(self):
        return VoiceHotkeyMonitor(
            parse_chord("ctrl+alt+space"),
            parse_chord("ctrl+alt+shift+space"),
            on_press=lambda mode: None,
            on_release=lambda mode: None,
        )

    def test_darwin_listener_uses_selective_intercept(self):
        monitor = self._monitor()
        fake_listener = mock.Mock()
        with mock.patch("voice_hotkey.current_os", return_value="darwin"), mock.patch(
            "pynput.keyboard.Listener", return_value=fake_listener
        ) as listener:
            monitor.start()

        kwargs = listener.call_args.kwargs
        self.assertIn("darwin_intercept", kwargs)
        self.assertNotIn("win32_event_filter", kwargs)
        self.assertIs(kwargs["darwin_intercept"].__self__, monitor)

    def test_linux_listener_has_no_global_suppression_filter(self):
        monitor = self._monitor()
        fake_listener = mock.Mock()
        with mock.patch("voice_hotkey.current_os", return_value="linux"), mock.patch(
            "pynput.keyboard.Listener", return_value=fake_listener
        ) as listener:
            monitor.start()

        kwargs = listener.call_args.kwargs
        self.assertNotIn("darwin_intercept", kwargs)
        self.assertNotIn("win32_event_filter", kwargs)

    def test_windows_listener_keeps_win32_filter(self):
        monitor = self._monitor()
        fake_listener = mock.Mock()
        with mock.patch("voice_hotkey.current_os", return_value="windows"), mock.patch(
            "pynput.keyboard.Listener", return_value=fake_listener
        ) as listener:
            monitor.start()

        kwargs = listener.call_args.kwargs
        self.assertIs(kwargs["win32_event_filter"].__self__, monitor)
        self.assertNotIn("darwin_intercept", kwargs)

    def test_windows_filter_suppresses_all_parseable_final_key_events(self):
        final_keys = (
            ("main-row 1", "1", 0x31),
            ("numpad 1", "1", 0x61),
            ("numpad subtract", "-", 0x6D),
            ("numpad decimal", ".", 0x6E),
            ("numpad divide", "/", 0x6F),
            ("function key", "f5", 0x74),
            ("enter", "enter", 0x0D),
            ("tab", "tab", 0x09),
            ("escape", "esc", 0x1B),
            ("punctuation", ";", 0xBA),
        )
        for case, final_key, vk in final_keys:
            with self.subTest(case=case):
                monitor = VoiceHotkeyMonitor(
                    parse_chord(f"ctrl+alt+{final_key}"),
                    parse_chord("ctrl+alt+shift+space"),
                    on_press=lambda mode: None,
                    on_release=lambda mode: None,
                )
                monitor._listener = mock.Mock()
                monitor._held.update({"ctrl", "alt"})
                data = mock.Mock(vkCode=vk)

                monitor._win32_filter(0x100, data)
                monitor._win32_filter(0x101, data)

                self.assertEqual(monitor._listener.suppress_event.call_count, 2)

    def test_stop_clears_pending_windows_suppression(self):
        monitor = self._monitor()
        monitor._listener = mock.Mock()
        monitor._win32_suppressed_key = "space"

        monitor.stop()

        self.assertIsNone(monitor._win32_suppressed_key)

    def test_darwin_intercept_suppresses_configured_final_key_down_and_up(self):
        monitor = self._monitor()
        from pynput.keyboard import Key

        fake_quartz = mock.Mock(
            kCGEventKeyDown=10,
            kCGEventKeyUp=11,
            kCGKeyboardEventKeycode=999,
        )
        fake_quartz.CGEventGetIntegerValueField.return_value = 49
        fake_event = object()
        with mock.patch.dict(sys.modules, {"Quartz": fake_quartz}):
            self.assertIs(monitor._darwin_intercept(10, fake_event), fake_event)
            monitor._handle_press(Key.ctrl)
            monitor._handle_press(Key.alt)
            monitor._handle_press(Key.space)
            self.assertIsNone(monitor._darwin_intercept(10, fake_event))
            monitor._handle_release(Key.space)
            self.assertIsNone(monitor._darwin_intercept(11, fake_event))


class SettingsTests(unittest.TestCase):
    def test_defaults_are_off_and_balanced(self):
        resolved = resolve_voice_settings({})
        self.assertFalse(resolved.enabled)
        self.assertEqual(resolved.profile, "balanced")
        self.assertEqual(resolved.hotkey, DEFAULT_DICTATION_HOTKEY)

    def test_bad_profile_and_hotkey_fall_back(self):
        warnings = []
        resolved = resolve_voice_settings(
            {"voice_profile": "turbo", "voice_hotkey": "space"},
            warnings,
        )
        self.assertEqual(resolved.profile, "balanced")
        self.assertEqual(resolved.hotkey, DEFAULT_DICTATION_HOTKEY)
        self.assertEqual(len(warnings), 2)

    def test_colliding_hotkeys_are_separated(self):
        warnings = []
        resolved = resolve_voice_settings(
            {
                "voice_hotkey": "ctrl+alt+space",
                "voice_command_hotkey": "ctrl+alt+space",
            },
            warnings,
        )
        self.assertNotEqual(resolved.hotkey, resolved.command_hotkey)
        self.assertTrue(warnings)


if __name__ == "__main__":
    unittest.main()
