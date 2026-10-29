"""Ditado settings for the Tauri manager (``manager_dictation``).

The voice controller is a fake that records calls; shortcut, command and
correction rules run through the real parsers and validators.
"""

import os
import sys
import types
import unittest
from unittest import mock

SOURCE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, SOURCE_DIR)

import i18n
from manager_bridge import RpcFailure
from manager_dictation import DictationActions
from voice_catalog import available_languages, selectable_catalog
from voice_commands import CommandConflictError

# No catalog model lacks language hints today; the tests stand one in.
ONE_LANGUAGE = "whisper-turbo"


def one_language(profile):
    return ("auto",) if profile == ONE_LANGUAGE else available_languages(profile)


MANY_LANGUAGES = next(entry["profile"] for entry in selectable_catalog() if len(available_languages(entry["profile"])) > 1)


class FakeVoice:
    def __init__(self):
        self.cache_dir = os.path.join(os.path.dirname(__file__), "tmp", "no-voice-cache")
        self.settings = types.SimpleNamespace(profile=MANY_LANGUAGES, language="pt-BR", hotkey="ctrl+alt+space",
                                              command_hotkey="ctrl+alt+shift+space", activation_mode="hold",
                                              voice_replacements={"snip voice": "SnipVoice"})
        self.applied = []
        self.accept = True
        self.edits = []
        self.removed = 0

    def apply_options(self, **options):
        self.applied.append(options)
        return self.accept

    def begin_hotkey_edit(self, token):
        self.edits.append(("begin", token))
        return True

    def end_hotkey_edit(self, token):
        self.edits.append(("end", token))

    def delete_active_model(self):
        self.removed += 1
        return True


class DictationTestCase(unittest.TestCase):
    def setUp(self):
        self.voice = FakeVoice()
        self.saved = {"meeting_hotkey": "ctrl+alt+r"}
        self.persisted = []
        self.commands = {"meu email": "nome@example.com", "Hand Edited": ""}
        self.command_saves = []
        self.changed = []
        self.reloads = []

        def save_commands(commands, expected):
            if expected != self.commands:
                raise CommandConflictError("changed outside")
            self.command_saves.append(commands)
            self.commands = dict(commands)

        self.actions = DictationActions(
            voice=self.voice, settings_getter=lambda: dict(self.saved),
            persist_settings=lambda payload: self.persisted.append(payload) or True,
            commands_getter=lambda: dict(self.commands), save_commands=save_commands,
            reload_commands=lambda: self.reloads.append(True) or True, on_changed=lambda: self.changed.append(True),
        )

    def tearDown(self):
        i18n.set_language("pt-BR")

    def values(self, **overrides):
        values = dict(self.actions.settings({})["values"])
        values.update(overrides)
        return values


class FormTests(DictationTestCase):
    @mock.patch("manager_dictation.available_languages", one_language)
    def test_the_form_reads_the_controller(self):
        view = self.actions.settings({})
        self.assertEqual(view["values"]["hotkey"], "ctrl+alt+space")
        self.assertEqual((view["commands"], view["corrections"]), (2, 1))
        self.assertEqual([item["id"] for item in view["languages"][ONE_LANGUAGE]], ["auto"])

    @mock.patch("manager_dictation.available_languages", one_language)
    def test_bad_shortcuts_are_refused_before_anything_is_applied(self):
        cases = [
            {"hotkey": "space"},
            {"commandHotkey": "ctrl"},
            {"commandHotkey": "ctrl+alt+space"},
            {"hotkey": "ctrl+alt+r"},
            {"profile": ONE_LANGUAGE, "language": "pt-BR"},
            {"activationMode": "sometimes"},
        ]
        for overrides in cases:
            with self.subTest(overrides=overrides), self.assertRaises(RpcFailure):
                self.actions.save({"values": self.values(**overrides), "confirmed": True})
        self.assertEqual(self.voice.applied, [])

    def test_a_model_not_downloaded_is_confirmed_first(self):
        first = self.actions.save({"values": self.values(hotkey="Ctrl+Alt+D")})
        self.assertEqual(first["status"], "confirm")
        self.assertEqual(self.voice.applied, [])
        saved = self.actions.save({"values": self.values(hotkey="Ctrl+Alt+D", activationMode="toggle"), "confirmed": True})
        self.assertEqual(saved["status"], "saved")
        self.assertEqual(self.voice.applied[0]["hotkey"], "ctrl+alt+d")
        self.assertEqual(self.voice.applied[0]["activation_mode"], "toggle")
        self.assertEqual(self.changed, [True])

    def test_a_refused_apply_says_so(self):
        self.voice.accept = False
        with self.assertRaises(RpcFailure):
            self.actions.save({"values": self.values(), "confirmed": True})
        self.assertEqual(self.changed, [])

    def test_shortcut_capture_pauses_and_resumes_the_same_token(self):
        self.assertTrue(self.actions.begin_shortcut_edit({})["ready"])
        self.actions.begin_shortcut_edit({})
        self.assertEqual(self.actions.end_shortcut_edit({}), {"ended": True})
        self.assertEqual(self.actions.end_shortcut_edit({}), {"ended": False})
        begins = [token for kind, token in self.voice.edits if kind == "begin"]
        ends = [token for kind, token in self.voice.edits if kind == "end"]
        self.assertEqual(ends, begins)
        self.assertFalse(any(token.is_set() for token in ends))


class CommandTests(DictationTestCase):
    def test_new_commands_are_normalized_and_hand_edited_ones_kept(self):
        baseline = self.actions.commands({})["commands"]
        commands = [*baseline, {"phrase": "  Meu   Telefone ", "text": "+00 0000"}]
        self.actions.save_command_list({"commands": commands, "baseline": baseline})
        self.assertEqual(self.command_saves[0], {"meu email": "nome@example.com", "Hand Edited": "", "meu telefone": "+00 0000"})

    def test_duplicates_and_empty_text_are_refused(self):
        baseline = self.actions.commands({})["commands"]
        for extra in ({"phrase": "MEU EMAIL", "text": "x"}, {"phrase": "nova", "text": "  "}):
            with self.subTest(extra=extra), self.assertRaises(RpcFailure):
                self.actions.save_command_list({"commands": [*baseline, extra], "baseline": baseline})
        self.assertEqual(self.command_saves, [])

    def test_a_library_changed_elsewhere_is_not_overwritten(self):
        baseline = self.actions.commands({})["commands"]
        self.commands["outra"] = "editada fora"
        with self.assertRaises(RpcFailure) as caught:
            self.actions.save_command_list({"commands": baseline, "baseline": baseline})
        self.assertIn("changed outside", caught.exception.message)

    def test_reload_reads_the_file_again(self):
        self.assertEqual(self.actions.reload({}), {"commands": 2})
        self.actions.reload_commands = lambda: False
        with self.assertRaises(RpcFailure):
            self.actions.reload({})


class CorrectionAndModelTests(DictationTestCase):
    def test_corrections_save_to_settings_and_the_controller(self):
        result = self.actions.save_corrections({"items": [{"source": "snip voice", "replacement": "SnipVoice"},
                                                          {"source": "teo", "replacement": "Teô"}]})
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(self.persisted[0]["voice_replacements"]["teo"], "Teô")
        self.assertEqual(self.voice.settings.voice_replacements["teo"], "Teô")

    def test_invalid_corrections_change_nothing(self):
        for items in ([{"source": "", "replacement": "x"}], [{"source": "a", "replacement": "b"}, {"source": "A", "replacement": "c"}]):
            with self.subTest(items=items), self.assertRaises(RpcFailure):
                self.actions.save_corrections({"items": items})
        self.assertEqual(self.persisted, [])

    def test_model_removal_asks_first(self):
        self.assertEqual(self.actions.remove_model({})["status"], "confirm")
        self.assertEqual(self.voice.removed, 0)
        self.assertEqual(self.actions.remove_model({"confirmed": True}), {"status": "removed"})
        self.assertEqual((self.voice.removed, self.changed), (1, [True]))


if __name__ == "__main__":
    unittest.main()
