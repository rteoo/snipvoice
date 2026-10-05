import os
import tempfile
import unittest
from unittest import mock

from snippet_utils import write_json_atomic
from trigger_index import compile_trigger_index
from voice_commands import (
    CommandConflictError,
    load_commands,
    save_commands,
    validate_command,
    validate_commands,
)
from voice_dispatch import match_voice_command


class VoiceCommandsTests(unittest.TestCase):
    def setUp(self):
        directory = os.path.join(os.path.dirname(__file__), "tmp")
        os.makedirs(directory, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=directory)
        self.addCleanup(temporary.cleanup)
        self.path = os.path.join(temporary.name, "commands.json")

    def test_missing_library_is_empty_and_can_be_saved(self):
        self.assertEqual(load_commands(self.path), {})
        save_commands(self.path, {"greeting": "Hello!\nHow can I help?"}, {})
        self.assertEqual(load_commands(self.path), {"greeting": "Hello!\nHow can I help?"})

    def test_editor_normalizes_phrase_and_preserves_literal_text(self):
        text = "  Hello!\n{literal} <plain text>  "
        phrase, value = validate_command("  Quick   Greeting  ", text, {})
        self.assertEqual(phrase, "quick greeting")
        self.assertEqual(value, text)
        library = {phrase: value}
        self.assertEqual(match_voice_command("Quick greeting", library,
                                           compile_trigger_index(library)), phrase)

    def test_duplicate_phrases_are_rejected_and_the_original_can_be_renamed(self):
        library = {"Quick Greeting": "Hello!"}
        with self.assertRaises(ValueError):
            validate_command("quick greeting", "Other text", library)
        self.assertEqual(validate_command("updated greeting", "Hello!", library,
                                          "Quick Greeting"), ("updated greeting", "Hello!"))

    def test_invalid_editor_input_is_rejected(self):
        for phrase, text in (("", "value"), ("_hidden", "value"), ("x" * 161, "value"),
                             ("greeting", ""), ("greeting", " \n"),
                             ("greeting", "x" * 100_001)):
            with self.subTest(phrase_length=len(phrase), text_length=len(text)):
                with self.assertRaises(ValueError):
                    validate_command(phrase, text, {})

    def test_legacy_library_shape_is_preserved(self):
        library = {"Existing Phrase": "", "other": "first\nsecond"}
        self.assertEqual(validate_commands(library), library)
        for invalid in ([], {"": "value"}, {"_hidden": "value"}, {"greeting": 1}):
            with self.subTest(shape=type(invalid).__name__):
                with self.assertRaises(ValueError):
                    validate_commands(invalid)

    def test_external_changes_are_preserved(self):
        baseline = {"greeting": "Hello!"}
        changed = {"greeting": "Updated elsewhere"}
        write_json_atomic(self.path, changed)
        with self.assertRaises(CommandConflictError):
            save_commands(self.path, {"greeting": "Draft"}, baseline)
        self.assertEqual(load_commands(self.path), changed)

    def test_corrupt_existing_library_is_not_overwritten(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{invalid")
        with self.assertRaises(ValueError):
            save_commands(self.path, {"greeting": "Draft"}, {})
        with open(self.path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "{invalid")

    def test_failed_atomic_replace_preserves_the_library_and_cleans_temp_files(self):
        baseline = {"greeting": "Hello!"}
        write_json_atomic(self.path, baseline)
        with mock.patch("snippet_utils.os.replace", side_effect=OSError("synthetic failure")):
            with self.assertRaises(OSError):
                save_commands(self.path, {"greeting": "Draft"}, baseline)
        self.assertEqual(load_commands(self.path), baseline)
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ["commands.json"])


if __name__ == "__main__":
    unittest.main()
