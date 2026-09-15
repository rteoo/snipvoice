import unittest

from trigger_index import compile_trigger_index, find_direct_trigger, find_dynamic_trigger


class TriggerIndexTests(unittest.TestCase):
    def setUp(self):
        self.snippets = {
            "abc": "first",
            "xbc": "second",
            "xname": "Alex",
            "_cpf_numbers": {
                "fulano": "123.456.789-00",
            },
            "_service_codes": {
                "__prefix__": "clw",
                "gtw": "service gateway restart",
            },
        }
        self.slow_triggers = {"xname"}
        self.index = compile_trigger_index(self.snippets, self.slow_triggers)

    def test_compile_trigger_index_preserves_direct_trigger_order(self):
        self.assertEqual(("abc", "xbc", "xname"), self.index["direct_triggers"])

    def test_compile_trigger_index_groups_by_last_character(self):
        self.assertEqual(("abc", "xbc"), self.index["direct_by_last_char"]["c"])
        self.assertEqual(("xname",), self.index["direct_by_last_char"]["e"])

    def test_find_direct_trigger_only_checks_matching_last_char_bucket(self):
        self.assertEqual("abc", find_direct_trigger("zzabc", self.index))
        self.assertIsNone(find_direct_trigger("zzab", self.index))

    def test_find_direct_trigger_preserves_original_precedence(self):
        self.assertEqual("abc", find_direct_trigger("prefixabc", self.index))

    def test_find_dynamic_trigger_resolves_builtin_mapping(self):
        trigger, value = find_dynamic_trigger(self.snippets, "xxcpffulano", self.index)

        self.assertEqual("cpffulano", trigger)
        self.assertEqual("123.456.789-00", value)

    def test_find_dynamic_trigger_resolves_custom_prefix(self):
        trigger, value = find_dynamic_trigger(self.snippets, "abcclwgtw", self.index)

        self.assertEqual("clwgtw", trigger)
        self.assertEqual("service gateway restart", value)

    def test_find_dynamic_trigger_returns_none_for_non_match(self):
        trigger, value = find_dynamic_trigger(self.snippets, "clwmissing", self.index)

        self.assertIsNone(trigger)
        self.assertIsNone(value)


class SuffixOrderingTests(unittest.TestCase):
    def test_longer_trigger_wins_over_suffix_regardless_of_source_order(self):
        # "ecban" is a suffix of "iecban"; the shorter one is listed first, which
        # under source-order matching would shadow the longer one.
        snippets = {"ecban": "short", "iecban": "long"}
        index = compile_trigger_index(snippets, set())
        self.assertEqual("iecban", find_direct_trigger("xiecban", index))
        self.assertEqual("ecban", find_direct_trigger("xecban", index))

    def test_bucket_is_ordered_longest_first(self):
        snippets = {"ab": "1", "xxab": "2", "zab": "3"}
        index = compile_trigger_index(snippets, set())
        self.assertEqual(("xxab", "zab", "ab"), index["direct_by_last_char"]["b"])


class FormTriggerMetadataTests(unittest.TestCase):
    def test_form_variable_snippet_is_flagged(self):
        snippets = {
            "xform": "Hello %%name%%",
            "xplain": "just text",
            "xclip": "paste %%clipboard-paste%%",
        }
        index = compile_trigger_index(snippets, set())
        self.assertIn("xform", index["form_triggers"])
        self.assertNotIn("xplain", index["form_triggers"])
        # clipboard-paste is an inline variable, not a form field.
        self.assertNotIn("xclip", index["form_triggers"])

    def test_callable_and_mapping_are_never_form_triggers(self):
        snippets = {"xcall": lambda: "x", "_codes": {"__prefix__": "c"}}
        index = compile_trigger_index(snippets, set())
        self.assertEqual(frozenset(), index["form_triggers"])

    def test_mapping_item_with_form_variable_is_flagged_by_composed_trigger(self):
        snippets = {
            "_prompt_codes": {
                "__prefix__": "prompt",
                "spec": "Write this: %%spec%%",
                "plain": "No input needed",
            },
        }
        index = compile_trigger_index(snippets, set())
        self.assertIn("promptspec", index["form_triggers"])
        self.assertNotIn("promptplain", index["form_triggers"])

    def test_mapping_and_dynamic_refs_are_not_form_triggers(self):
        snippets = {
            "xmap": "CPF %%cpffulano%%",
            "xdyn": "hoje %%xhj%%",
            "xhj": lambda: "01/01/2026",
            "_cpf_numbers": {"fulano": "123"},
        }
        index = compile_trigger_index(snippets, set())
        self.assertNotIn("xmap", index["form_triggers"])
        self.assertNotIn("xdyn", index["form_triggers"])


class SlowTriggerMetadataTests(unittest.TestCase):
    def test_direct_slow_triggers_are_included(self):
        index = compile_trigger_index({"xdolar": lambda: "R$5"}, {"xdolar"})
        self.assertIn("xdolar", index["slow_triggers"])

    def test_snippet_referencing_slow_trigger_becomes_slow(self):
        snippets = {
            "xreport": "Dólar hoje: %%xdolar%%",
            "xplain": "sem referência",
            "xdolar": lambda: "R$5",
        }
        index = compile_trigger_index(snippets, {"xdolar"})
        self.assertIn("xreport", index["slow_triggers"])
        self.assertNotIn("xplain", index["slow_triggers"])

    def test_reference_to_fast_trigger_stays_fast(self):
        snippets = {"xgreet": "Hoje é %%xhj%%", "xhj": lambda: "01/01/2026"}
        index = compile_trigger_index(snippets, {"xdolar"})
        self.assertNotIn("xgreet", index["slow_triggers"])


class MultiSuffixOrderingTests(unittest.TestCase):
    def test_three_way_suffix_chain_matches_longest_available(self):
        # "x" ⊂ "yx" ⊂ "zzyx": each buffer must resolve to the longest trigger
        # that is actually a suffix of it, never a shorter shadow.
        snippets = {"x": "1", "yx": "2", "zzyx": "3"}
        index = compile_trigger_index(snippets, set())
        self.assertEqual("zzyx", find_direct_trigger("azzyx", index))
        self.assertEqual("yx", find_direct_trigger("ayx", index))
        self.assertEqual("x", find_direct_trigger("ax", index))

    def test_multiple_triggers_share_last_char_all_reachable(self):
        snippets = {"cat": "c", "hat": "h", "splat": "s"}
        index = compile_trigger_index(snippets, set())
        # longest-first, ties keep source order (cat before hat).
        self.assertEqual(("splat", "cat", "hat"), index["direct_by_last_char"]["t"])
        self.assertEqual("cat", find_direct_trigger("wombat cat", index))
        self.assertEqual("hat", find_direct_trigger("with a hat", index))
        self.assertEqual("splat", find_direct_trigger("go splat", index))


class BufferBoundaryTests(unittest.TestCase):
    def test_trigger_longer_than_buffer_does_not_match(self):
        index = compile_trigger_index({"abcdef": "x"}, set())
        self.assertIsNone(find_direct_trigger("cdef", index))

    def test_trigger_exactly_equal_to_buffer_matches(self):
        index = compile_trigger_index({"abc": "x"}, set())
        self.assertEqual("abc", find_direct_trigger("abc", index))

    def test_empty_typed_text_never_matches(self):
        index = compile_trigger_index({"abc": "x"}, set())
        self.assertIsNone(find_direct_trigger("", index))
        self.assertEqual((None, None), find_dynamic_trigger({"abc": "x"}, "", index))


class UnicodeTriggerTests(unittest.TestCase):
    def test_accented_trigger_orders_longest_first_and_matches(self):
        snippets = {"café": "coffee", "xcafé": "big coffee"}
        index = compile_trigger_index(snippets, set())
        self.assertEqual(("xcafé", "café"), index["direct_by_last_char"]["é"])
        self.assertEqual("xcafé", find_direct_trigger("zxcafé", index))
        self.assertEqual("café", find_direct_trigger("zcafé", index))

    def test_single_codepoint_emoji_trigger_matches(self):
        snippets = {"x🎉": "party"}
        index = compile_trigger_index(snippets, set())
        self.assertEqual("x🎉", find_direct_trigger("hey x🎉", index))

    def test_multi_codepoint_emoji_trigger_still_matches(self):
        # A flag is two code points; the bucket keys on the final code point but
        # endswith must still match the whole trigger string.
        flag = "\U0001F1E7\U0001F1F7"  # regional-indicator B + R
        trigger = "x" + flag
        index = compile_trigger_index({trigger: "brasil"}, set())
        self.assertEqual(trigger, find_direct_trigger("vai " + trigger, index))


class DynamicTriggerEdgeTests(unittest.TestCase):
    def setUp(self):
        self.snippets = {"_cpf_numbers": {"fulano": "123", "empty": ""}}
        self.index = compile_trigger_index(self.snippets, set())

    def test_prefix_without_name_does_not_match(self):
        self.assertEqual((None, None), find_dynamic_trigger(self.snippets, "xxcpf", self.index))

    def test_rfind_picks_last_prefix_occurrence(self):
        # The prefix appears twice; only the trailing one carries a real name.
        trigger, value = find_dynamic_trigger(self.snippets, "cpfxcpffulano", self.index)
        self.assertEqual("cpffulano", trigger)
        self.assertEqual("123", value)

    def test_empty_string_mapping_value_is_a_match_not_a_miss(self):
        # value == "" must be treated as a resolved value, never as "no match".
        trigger, value = find_dynamic_trigger(self.snippets, "cpfempty", self.index)
        self.assertEqual("cpfempty", trigger)
        self.assertEqual("", value)

    def test_invalid_mapping_prefix_falls_back_before_indexing(self):
        snippets = {
            "_service_codes": {"__prefix__": None, "restart": "ok"},
        }
        index = compile_trigger_index(snippets, set())
        self.assertEqual(("service",), index["ordered_prefixes"])
        self.assertEqual(
            ("servicerestart", "ok"),
            find_dynamic_trigger(snippets, "servicerestart", index),
        )

    def test_empty_prefix_matches_bare_item_suffix(self):
        snippets = {
            "_service_codes": {
                "__prefix__": "",
                "start": "short",
                "restart": "long",
            },
        }
        index = compile_trigger_index(snippets, set())
        self.assertEqual(("",), index["ordered_prefixes"])
        self.assertEqual(
            ("restart", "long"),
            find_dynamic_trigger(snippets, "typed before restart", index),
        )


class EmptyKeyRegressionTests(unittest.TestCase):
    """An empty-string trigger key is excluded from *every* index set.

    An empty key would suffix-match every keystroke (``endswith("")`` is always
    true) and has no last character to bucket by. ``_is_indexable_trigger``
    excludes it once, consistently, from the direct index and from both metadata
    helpers (``form_triggers`` / ``slow_triggers``) — so the module can never
    disagree with itself about whether "" is a trigger, regardless of the value
    a hand-edited snippets.json pairs with the empty key.
    """

    def test_empty_string_trigger_key_does_not_crash_and_real_trigger_still_indexes(self):
        index = compile_trigger_index({"": "x", "abc": "y"}, set())
        self.assertEqual("abc", find_direct_trigger("zzabc", index))

    def test_empty_key_with_form_variable_is_not_a_form_trigger(self):
        # A form-variable value under an empty key must not leak into
        # form_triggers while the direct index excludes the same key.
        index = compile_trigger_index({"": "Hello %%name%%", "abc": "y"}, set())
        self.assertNotIn("", index["form_triggers"])

    def test_empty_key_referencing_slow_trigger_is_not_a_slow_trigger(self):
        # A body that references a slow dynamic trigger would normally make the
        # snippet slow; keyed under "" it must still be excluded.
        snippets = {"": "Dólar hoje: %%xdolar%%", "xdolar": lambda: "R$5"}
        index = compile_trigger_index(snippets, {"xdolar"})
        self.assertNotIn("", index["slow_triggers"])


if __name__ == "__main__":
    unittest.main()
