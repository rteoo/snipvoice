import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from voice_settings import resolve_voice_settings, voice_settings_payload
from voice_text_support import apply_voice_replacements, validate_replacements


class VoiceSettingsTests(unittest.TestCase):
    def test_missing_file_shape_is_off(self):
        settings = resolve_voice_settings({})
        self.assertFalse(settings.enabled)
        self.assertEqual(settings.profile, "balanced")
        self.assertEqual(settings.hotkey, "ctrl+alt+space")
        self.assertEqual(settings.command_hotkey, "ctrl+alt+shift+space")
        self.assertEqual(settings.voice_replacements, {})

    def test_warnings_for_bad_values(self):
        notes = []
        settings = resolve_voice_settings(
            {
                "voice_enabled": "yes",
                "voice_profile": "huge",
                "voice_language": "fr",
                "voice_hotkey": "space",
                "voice_command_hotkey": "space",
            },
            warnings=notes,
        )
        self.assertFalse(settings.enabled)
        self.assertEqual(settings.profile, "balanced")
        self.assertEqual(settings.language, "auto")
        self.assertGreaterEqual(len(notes), 3)

    def test_payload_roundtrip_keys(self):
        settings = resolve_voice_settings({"voice_enabled": True, "voice_profile": "balanced"})
        payload = voice_settings_payload(settings)
        self.assertEqual(
            set(payload),
            {
                "voice_enabled",
                "voice_profile",
                "voice_language",
                "voice_hotkey",
                "voice_command_hotkey",
                "voice_replacements",
            },
        )
        self.assertTrue(payload["voice_enabled"])
        self.assertEqual(payload["voice_profile"], "balanced")

    def test_replacements_are_normalized_and_round_trip(self):
        settings = resolve_voice_settings({"voice_replacements": {" Quen ": "Qwen"}})
        self.assertEqual(settings.voice_replacements, {"Quen": "Qwen"})
        self.assertEqual(
            voice_settings_payload(settings)["voice_replacements"],
            {"Quen": "Qwen"},
        )

    def test_malformed_replacements_are_disabled(self):
        notes = []
        settings = resolve_voice_settings({"voice_replacements": ["bad"]}, warnings=notes)
        self.assertEqual(settings.voice_replacements, {})
        self.assertTrue(any("voice_replacements" in note for note in notes))
        self.assertEqual(resolve_voice_settings({}, warnings=[]).voice_replacements, {})

    def test_replacements_are_unicode_case_insensitive_longest_first_and_non_cascading(self):
        replacements = {"Quen": "Qwen", "Quen model": "Qwen model", "ação": "ACAO"}
        self.assertEqual(
            apply_voice_replacements("The QUEN MODEL and ação; Quen." , replacements),
            "The Qwen model and ACAO; Qwen.",
        )
        self.assertEqual(
            apply_voice_replacements("um comum", {"um": "one", "one": "two"}),
            "one comum",
        )
        self.assertEqual(
            apply_voice_replacements("um teste com Qwen", {"queen": "Qwen"}),
            "um teste com Qwen",
        )
        self.assertEqual(
            apply_voice_replacements("STRASSE", {"straße": "road"}),
            "road",
        )

    def test_invalid_entries_reject_the_whole_mapping(self):
        self.assertEqual(validate_replacements({"": "x"}), {})
        self.assertEqual(validate_replacements({"x": 3}), {})

    def test_accuracy_profile_is_selectable_and_forces_auto_language(self):
        notes = []
        settings = resolve_voice_settings(
            {"voice_profile": "accuracy", "voice_language": "pt-BR"},
            warnings=notes,
        )
        self.assertEqual(settings.profile, "accuracy")
        self.assertEqual(settings.language, "auto")
        self.assertEqual(notes, [])

    def test_hidden_profiles_fall_back_to_balanced(self):
        notes = []
        settings = resolve_voice_settings({"voice_profile": "streaming"}, warnings=notes)
        self.assertEqual(settings.profile, "balanced")
        self.assertTrue(notes)

    def test_colliding_hotkeys_stay_distinct(self):
        notes = []
        settings = resolve_voice_settings(
            {
                "voice_hotkey": "ctrl+alt+shift+space",
                "voice_command_hotkey": "ctrl+alt+shift+space",
            },
            warnings=notes,
        )
        self.assertNotEqual(settings.hotkey, settings.command_hotkey)
        self.assertTrue(notes)

    def test_aliased_modifiers_still_count_as_a_collision(self):
        notes = []
        settings = resolve_voice_settings(
            {
                "voice_hotkey": "control+alt+shift+space",
                "voice_command_hotkey": "ctrl+alt+shift+space",
            },
            warnings=notes,
        )
        from voice_hotkey import parse_chord
        self.assertNotEqual(parse_chord(settings.hotkey), parse_chord(settings.command_hotkey))
        self.assertTrue(notes)


if __name__ == "__main__":
    unittest.main()
