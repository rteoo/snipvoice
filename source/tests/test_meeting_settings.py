import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from meeting_settings import resolve_meeting_settings, resolve_selection
from summary_catalog import DEFAULT_SUMMARY_MODEL


class MeetingSettingsTests(unittest.TestCase):
    def test_default_sources_are_independent_of_dictation(self):
        settings = resolve_meeting_settings({"voice_enabled": False, "unknown": 7})
        self.assertEqual(settings.sources, "both")
        self.assertEqual(settings.microphone.argument(), "default:multimedia")
        self.assertEqual(settings.hotkey, "")
        self.assertEqual(settings.summary_model, DEFAULT_SUMMARY_MODEL)
        self.assertNotIn("voice_enabled", settings.payload())

    def test_manual_id_and_role_round_trip(self):
        settings = resolve_meeting_settings({"meeting_sources": "system",
            "meeting_system": {"mode": "manual", "endpoint_id": "opaque-uid"},
            "meeting_microphone": {"mode": "default", "default_role": "communications"},
            "meeting_hotkey": "ctrl+shift+r"})
        self.assertEqual(resolve_meeting_settings(settings.payload()), settings)
        self.assertEqual(settings.system.argument(), "opaque-uid")
        self.assertEqual(settings.microphone.argument(), "default:communications")

    def test_invalid_manual_device_never_falls_back(self):
        for endpoint in (None, "", "\n", "x" * 1025, 123):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                resolve_selection({"mode": "manual", "endpoint_id": endpoint})

    def test_malformed_settings_fail_actionably(self):
        for values in ({"meeting_sources": "other"}, {"meeting_hotkey": 3},
                       {"meeting_profile": "streaming"}, {"meeting_language": "xx"}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                resolve_meeting_settings(values)

    def test_legacy_ollama_value_migrates_to_builtin_default(self):
        settings = resolve_meeting_settings({"meeting_summary_model": "qwen:latest"})
        self.assertEqual(settings.summary_model, DEFAULT_SUMMARY_MODEL)
