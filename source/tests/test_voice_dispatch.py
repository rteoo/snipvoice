import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from trigger_index import compile_trigger_index
from voice_dispatch import (
    MODE_COMMAND,
    MODE_DICTATION,
    OUTCOME_EMPTY,
    OUTCOME_EXPANDED,
    OUTCOME_FAILED,
    OUTCOME_FORM,
    OUTCOME_INSERTED,
    OUTCOME_NO_MATCH,
    OUTCOME_SECURE_INPUT,
    OUTCOME_TARGET_LOST,
    VoiceDispatchResult,
    VoiceTarget,
    dispatch_voice_result,
    match_voice_command,
)


def _index(snippets):
    return compile_trigger_index(snippets, set())


class MatchCommandTests(unittest.TestCase):
    def setUp(self):
        self.snippets = {
            "xadds": "hello",
            "_cpf_numbers": {"__prefix__": "cpf", "fulano": "123"},
        }
        self.index = _index(self.snippets)

    def test_exact_direct_trigger(self):
        self.assertEqual(match_voice_command("XAdds", self.snippets, self.index), "xadds")

    def test_exact_mapping_trigger(self):
        self.assertEqual(
            match_voice_command("cpffulano", self.snippets, self.index),
            "cpffulano",
        )

    def test_substring_does_not_fire(self):
        self.assertIsNone(
            match_voice_command("please expand xadds now", self.snippets, self.index)
        )

    def test_empty_does_not_match(self):
        self.assertIsNone(match_voice_command("   ", self.snippets, self.index))


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.snippets = {"xadds": "hello"}
        self.index = _index(self.snippets)
        self.inserted = []
        self.expanded = []
        self.forms = []
        self.clip = []

    def _dispatch(self, text, mode=MODE_DICTATION, form=False, restore=True, secure=False):
        return dispatch_voice_result(
            text,
            mode,
            VoiceTarget("form" if form else "window"),
            snippets=self.snippets,
            trigger_index=self.index,
            insert_text=lambda value: self.inserted.append(value) or True,
            expand_trigger=lambda trigger: self.expanded.append(trigger) or True,
            apply_form=(lambda value: self.forms.append(value)) if form else None,
            restore_target=lambda target: restore,
            secure_input_blocks=lambda: secure,
            leave_on_clipboard=lambda value: self.clip.append(value) or True,
        )

    def test_empty_is_ignored(self):
        self.assertEqual(self._dispatch("  ").outcome, OUTCOME_EMPTY)

    def test_dictation_is_literal(self):
        self.assertEqual(self._dispatch("xadds").outcome, OUTCOME_INSERTED)
        self.assertEqual(self.inserted, ["xadds"])
        self.assertEqual(self.expanded, [])

    def test_command_expands_exact_trigger(self):
        self.assertEqual(self._dispatch("xadds", mode=MODE_COMMAND).outcome, OUTCOME_EXPANDED)
        self.assertEqual(self.expanded, ["xadds"])

    def test_unknown_command(self):
        self.assertEqual(self._dispatch("nope", mode=MODE_COMMAND).outcome, OUTCOME_NO_MATCH)

    def test_form_updates_widget(self):
        self.assertEqual(self._dispatch("João", form=True).outcome, OUTCOME_FORM)
        self.assertEqual(self.forms, ["João"])
        self.assertEqual(self.inserted, [])

    def test_secure_input_uses_clipboard(self):
        self.assertEqual(
            self._dispatch("hi", secure=True),
            VoiceDispatchResult(OUTCOME_SECURE_INPUT, clipboard_saved=True),
        )
        self.assertEqual(self.clip, ["hi"])

    def test_lost_target_uses_clipboard(self):
        self.assertEqual(
            self._dispatch("hi", restore=False),
            VoiceDispatchResult(OUTCOME_TARGET_LOST, clipboard_saved=True),
        )
        self.assertEqual(self.clip, ["hi"])

    def test_secure_input_reports_clipboard_failure(self):
        result = dispatch_voice_result(
            "hi",
            MODE_DICTATION,
            VoiceTarget("window"),
            snippets=self.snippets,
            trigger_index=self.index,
            insert_text=lambda value: True,
            expand_trigger=lambda trigger: True,
            apply_form=None,
            restore_target=lambda target: True,
            secure_input_blocks=lambda: True,
            leave_on_clipboard=lambda value: False,
        )

        self.assertEqual(
            result,
            VoiceDispatchResult(OUTCOME_SECURE_INPUT, clipboard_saved=False),
        )

    def test_failed_insertion_reports_successful_clipboard_recovery(self):
        result = dispatch_voice_result(
            "hi",
            MODE_DICTATION,
            VoiceTarget("window"),
            snippets=self.snippets,
            trigger_index=self.index,
            insert_text=lambda value: False,
            expand_trigger=lambda trigger: True,
            apply_form=None,
            restore_target=lambda target: True,
            secure_input_blocks=lambda: False,
            leave_on_clipboard=lambda value: True,
        )

        self.assertEqual(
            result,
            VoiceDispatchResult(OUTCOME_FAILED, clipboard_saved=True),
        )


if __name__ == "__main__":
    unittest.main()
