import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from validation_support import validate_trigger


class ValidateTriggerTests(unittest.TestCase):
    def test_clean_trigger_has_no_warnings(self):
        self.assertEqual(validate_trigger("xhello", {"xother"}, set()), [])

    def test_whitespace_warns(self):
        warnings = validate_trigger("x hi", set(), set())
        self.assertTrue(any("espaços" in w for w in warnings))

    def test_terminator_char_warns(self):
        warnings = validate_trigger("x.hi", set(), set())
        self.assertTrue(any("pontuação" in w.lower() or "terminador" in w.lower() for w in warnings))

    def test_short_trigger_warns(self):
        self.assertTrue(any("curto" in w for w in validate_trigger("ab", set(), set())))

    def test_shadowed_by_dynamic_warns(self):
        warnings = validate_trigger("xhj", set(), {"xhj", "xdolar"})
        self.assertTrue(any("dinâmico" in w for w in warnings))

    def test_suffix_conflict_warns(self):
        # "ecban" is a suffix of the proposed "iecban"
        warnings = validate_trigger("iecban", {"ecban"}, set())
        self.assertTrue(any("ecban" in w for w in warnings))

    def test_existing_ends_with_new_warns(self):
        warnings = validate_trigger("ban", {"ecban"}, set())
        self.assertTrue(any("ecban" in w for w in warnings))


class ValidateTriggerEdgeTests(unittest.TestCase):
    def test_whitespace_suppresses_the_terminator_warning(self):
        # The whitespace branch is an ``elif`` before the terminator branch, so a
        # trigger with both a space and punctuation reports only the space.
        warnings = validate_trigger("a b.", set(), set())
        self.assertTrue(any("espaços" in w for w in warnings))
        self.assertFalse(any("pontuação" in w.lower() for w in warnings))

    def test_tab_carriage_return_and_newline_count_as_whitespace(self):
        for trigger in ("a\tb", "a\nb", "a\rb"):
            warnings = validate_trigger(trigger, set(), set())
            self.assertTrue(any("espaços" in w for w in warnings), repr(trigger))

    def test_short_length_boundary_is_two(self):
        self.assertTrue(any("curto" in w for w in validate_trigger("ab", set(), set())))
        self.assertFalse(any("curto" in w for w in validate_trigger("abc", set(), set())))

    def test_trigger_is_not_flagged_as_its_own_suffix(self):
        self.assertEqual([], validate_trigger("xhello", {"xhello"}, set()))

    def test_both_suffix_directions_reported_together(self):
        warnings = validate_trigger("logout", {"out", "relogout"}, set())
        self.assertTrue(any("'out'" in w for w in warnings))
        self.assertTrue(any("'relogout'" in w for w in warnings))

    def test_clean_unicode_trigger_has_no_warnings(self):
        self.assertEqual([], validate_trigger("café", {"outro"}, set()))

    def test_short_and_shadowed_warnings_accumulate(self):
        warnings = validate_trigger("ab", set(), {"ab"})
        self.assertTrue(any("curto" in w for w in warnings))
        self.assertTrue(any("dinâmico" in w for w in warnings))

    def test_astral_emoji_trigger_counts_as_short(self):
        # "x🎉" is two code points, so it trips the 1–2 character warning.
        self.assertTrue(any("curto" in w for w in validate_trigger("x🎉", set(), set())))


if __name__ == "__main__":
    unittest.main()
