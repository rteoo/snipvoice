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

    def test_previous_builtin_catalog_ids_migrate_to_current_families(self):
        expected = {
            "qwen3-1.7b-q4": "qwen3.5-2b-q4",
            "granite-3.3-2b-q4": "lfm2.5-2.6b-q4",
            "granite-4.0-1b-q4": "lfm2.5-2.6b-q4",
            "granite-4.2-3b-q4": "lfm2.5-2.6b-q4",
            "gemma-3-1b-q4": "gemma-4-e2b-q4",
        }
        for old, new in expected.items():
            with self.subTest(old=old):
                settings = resolve_meeting_settings({"meeting_summary_model": old})
                self.assertEqual(settings.summary_model, new)

    def test_new_settings_default_to_explicit_recording_and_opt_in_automation(self):
        settings = resolve_meeting_settings({})
        self.assertEqual(settings.destination, "")
        self.assertTrue(settings.input_enabled)
        self.assertTrue(settings.output_enabled)
        self.assertFalse(settings.auto_transcribe)
        self.assertFalse(settings.auto_summary)
        self.assertFalse(settings.voice_boost)

    def test_destination_and_switches_round_trip_without_touching_destination(self):
        destination = os.path.join(os.path.dirname(__file__), "missing-recordings")
        self.assertFalse(os.path.exists(destination))
        settings = resolve_meeting_settings({
            "meeting_destination": destination,
            "meeting_input_enabled": True,
            "meeting_output_enabled": False,
            "meeting_auto_transcribe": True,
            "meeting_auto_summary": True,
            "meeting_voice_boost": True,
        })
        self.assertEqual(resolve_meeting_settings(settings.payload()), settings)
        self.assertEqual(settings.sources, "microphone")
        self.assertFalse(os.path.exists(destination))

    def test_source_enum_migrates_to_input_output_switches(self):
        settings = resolve_meeting_settings({"meeting_sources": "system"})
        self.assertFalse(settings.input_enabled)
        self.assertTrue(settings.output_enabled)
        self.assertEqual(settings.sources, "system")

    def test_malformed_new_values_fail_without_fallback(self):
        values = (
            {"meeting_destination": 3},
            {"meeting_destination": None},
            {"meeting_destination": "relative/path"},
            {"meeting_input_enabled": "false"},
            {"meeting_input_enabled": None},
            {"meeting_output_enabled": 0},
            {"meeting_auto_transcribe": "yes"},
            {"meeting_auto_summary": 1},
            {"meeting_voice_boost": []},
            {"meeting_input_enabled": False, "meeting_output_enabled": False},
            {"meeting_auto_summary": True},
        )
        for value in values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                resolve_meeting_settings(value)

    def test_unknown_keys_are_not_persisted(self):
        payload = resolve_meeting_settings({"future_setting": "ignore"}).payload()
        self.assertNotIn("future_setting", payload)
