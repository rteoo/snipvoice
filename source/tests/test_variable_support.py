import threading
import unittest

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from variable_support import (
    VariableResolutionError,
    classify_variable,
    find_variable_names,
    has_form_variables,
    resolve_form_variables,
    resolve_inline,
)


def _resolve_in_thread(*args, timeout=2.0, **kwargs):
    """Run resolve_inline on a daemon thread and report whether it hung.

    Returns (result, timed_out). Used for reference cycles: if the source could
    ever infinite-loop, the join times out and ``timed_out`` is True instead of
    wedging the whole test runner.
    """
    box = {}

    def target():
        box["result"] = resolve_inline(*args, **kwargs)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout)
    return box.get("result"), thread.is_alive()


class TestFindVariableNames(unittest.TestCase):

    def test_empty_string(self):
        self.assertEqual(find_variable_names(""), [])

    def test_no_variables(self):
        self.assertEqual(find_variable_names("hello world"), [])

    def test_single_variable(self):
        self.assertEqual(find_variable_names("%%nome%%"), ["nome"])

    def test_multiple_variables(self):
        self.assertEqual(find_variable_names("%%a%% e %%b%%"), ["a", "b"])

    def test_duplicates_deduplicated(self):
        self.assertEqual(find_variable_names("%%a%% %%a%%"), ["a"])

    def test_order_preserved(self):
        self.assertEqual(find_variable_names("%%z%% %%a%% %%m%%"), ["z", "a", "m"])

    def test_no_spaces_inside(self):
        # Spaces inside %% should not match (pattern requires non-whitespace)
        self.assertEqual(find_variable_names("%% name %%"), [])

    def test_clipboard_paste_name(self):
        self.assertEqual(find_variable_names("%%clipboard-paste%%"), ["clipboard-paste"])


class TestClassifyVariable(unittest.TestCase):

    def test_clipboard(self):
        self.assertEqual(classify_variable("clipboard-paste", {}), "clipboard")

    def test_snippet_ref(self):
        snippets = {"xname": "Alex"}
        self.assertEqual(classify_variable("xname", snippets), "snippet_ref")

    def test_callable_snippet_is_dynamic_ref(self):
        snippets = {"xcot": lambda: "R$10"}
        self.assertEqual(classify_variable("xcot", snippets), "dynamic_ref")

    def test_mapping_trigger_is_mapping_ref(self):
        snippets = {"_cpf_numbers": {"fulano": "123.456.789-00"}}
        self.assertEqual(classify_variable("cpffulano", snippets), "mapping_ref")

    def test_unknown_mapping_item_is_form_field(self):
        snippets = {"_cpf_numbers": {"fulano": "123.456.789-00"}}
        self.assertEqual(classify_variable("cpfciclano", snippets), "form_field")

    def test_unknown_name_is_form_field(self):
        self.assertEqual(classify_variable("nome", {}), "form_field")

    def test_nonexistent_key_is_form_field(self):
        snippets = {"xname": "Alex"}
        self.assertEqual(classify_variable("data", snippets), "form_field")


class TestHasFormVariables(unittest.TestCase):

    def test_no_variables(self):
        self.assertFalse(has_form_variables("plain text", {}))

    def test_only_clipboard(self):
        self.assertFalse(has_form_variables("%%clipboard-paste%%", {}))

    def test_only_snippet_ref(self):
        snippets = {"xname": "Alex"}
        self.assertFalse(has_form_variables("Olá %%xname%%", snippets))

    def test_only_mapping_ref(self):
        snippets = {"_cpf_numbers": {"fulano": "123"}}
        self.assertFalse(has_form_variables("CPF %%cpffulano%%", snippets))

    def test_only_dynamic_ref(self):
        self.assertFalse(has_form_variables("%%xcot%%", {"xcot": lambda: "R$10"}))

    def test_has_form_field(self):
        self.assertTrue(has_form_variables("Olá %%nome%%", {}))

    def test_mixed_with_form_field(self):
        snippets = {"xname": "Alex"}
        self.assertTrue(has_form_variables("%%xname%% e %%data%%", snippets))


class TestResolveInline(unittest.TestCase):

    def test_no_variables(self):
        result = resolve_inline("hello world", {}, lambda: None)
        self.assertEqual(result, "hello world")

    def test_clipboard_substitution(self):
        result = resolve_inline("cmd %%clipboard-paste%%", {}, lambda: "meu texto")
        self.assertEqual(result, "cmd meu texto")

    def test_clipboard_unavailable_fails_instead_of_deleting_token(self):
        with self.assertRaisesRegex(VariableResolutionError, "transferência"):
            resolve_inline("cmd %%clipboard-paste%%", {}, lambda: None)

    def test_clipboard_valid_empty_text_is_substituted(self):
        result = resolve_inline("cmd %%clipboard-paste%%", {}, lambda: "")
        self.assertEqual(result, "cmd ")

    def test_clipboard_read_exception_is_wrapped_truthfully(self):
        def unavailable():
            raise OSError("clipboard busy")

        with self.assertRaises(VariableResolutionError) as caught:
            resolve_inline("cmd %%clipboard-paste%%", {}, unavailable)
        self.assertIsInstance(caught.exception.__cause__, OSError)

    def test_snippet_ref_plain(self):
        snippets = {"xaddr": "Rua das Flores, 10"}
        result = resolve_inline("Endereço: %%xaddr%%", snippets, lambda: None)
        self.assertEqual(result, "Endereço: Rua das Flores, 10")

    def test_snippet_ref_rich_text(self):
        snippets = {"xtitle": {"__kind__": "rich_text", "text": "Relatório"}}
        result = resolve_inline("Ver %%xtitle%%", snippets, lambda: None)
        self.assertEqual(result, "Ver Relatório")

    def test_callable_snippet_is_invoked(self):
        snippets = {"xcot": lambda: "R$10"}
        result = resolve_inline("%%xcot%%", snippets, lambda: None)
        self.assertEqual(result, "R$10")

    def test_callable_returning_none_becomes_empty(self):
        # Action-only flows (xwapp opens the browser) substitute nothing.
        snippets = {"xwapp": lambda: None}
        result = resolve_inline("link: %%xwapp%%", snippets, lambda: None)
        self.assertEqual(result, "link: ")

    def test_raising_callable_substitutes_empty_and_notifies(self):
        def boom():
            raise RuntimeError("sem rede")

        calls = []
        result = resolve_inline(
            "valor: %%xdolar%%", {"xdolar": boom}, lambda: None,
            notify_failure=lambda name, value: calls.append((name, value)),
        )
        self.assertEqual(result, "valor: ")
        self.assertEqual(calls[0][0], "xdolar")
        self.assertIn("sem rede", calls[0][1])

    def test_marker_result_is_substituted_and_notified(self):
        calls = []
        snippets = {"xdolar": lambda: "[Dado indisponível]"}
        result = resolve_inline(
            "%%xdolar%%", snippets, lambda: None,
            notify_failure=lambda name, value: calls.append((name, value)),
        )
        self.assertEqual(result, "[Dado indisponível]")
        self.assertEqual(calls, [("xdolar", "[Dado indisponível]")])

    def test_mapping_ref_resolved(self):
        snippets = {"_cpf_numbers": {"fulano": "123.456.789-00"}}
        result = resolve_inline("CPF: %%cpffulano%%", snippets, lambda: None)
        self.assertEqual(result, "CPF: 123.456.789-00")

    def test_mapping_ref_rich_text_value(self):
        snippets = {"_custom_codes": {"__prefix__": "cc",
                                      "x": {"__kind__": "rich_text", "text": "ABC"}}}
        result = resolve_inline("%%ccx%%", snippets, lambda: None)
        self.assertEqual(result, "ABC")

    def test_dynamic_ref_circular_guard(self):
        snippets = {"xcot": lambda: "R$10"}
        result = resolve_inline("%%xcot%%", snippets, lambda: None, _seen={"xcot"})
        self.assertEqual(result, "%%xcot%%")

    def test_form_field_left_unchanged(self):
        result = resolve_inline("Olá %%nome%%", {}, lambda: None)
        self.assertEqual(result, "Olá %%nome%%")

    def test_circular_ref_guard(self):
        snippets = {"xself": "start %%xself%% end"}
        result = resolve_inline("%%xself%%", snippets, lambda: None, _seen={"xself"})
        self.assertEqual(result, "%%xself%%")

    def test_multiple_variables_mixed(self):
        snippets = {"xaddr": "Rua A"}
        result = resolve_inline(
            "Clip: %%clipboard-paste%%, Ref: %%xaddr%%, Form: %%nome%%",
            snippets,
            lambda: "CB",
        )
        self.assertEqual(result, "Clip: CB, Ref: Rua A, Form: %%nome%%")


class TestResolveFormVariables(unittest.TestCase):

    def test_single_field(self):
        result = resolve_form_variables("Olá %%nome%%", {"nome": "Carlos"})
        self.assertEqual(result, "Olá Carlos")

    def test_multiple_fields(self):
        result = resolve_form_variables(
            "%%nome%%, disponível em %%data%%?",
            {"nome": "Ana", "data": "segunda"},
        )
        self.assertEqual(result, "Ana, disponível em segunda?")

    def test_missing_key_leaves_token(self):
        result = resolve_form_variables("Olá %%nome%%", {})
        self.assertEqual(result, "Olá %%nome%%")

    def test_empty_value(self):
        result = resolve_form_variables("%%campo%%", {"campo": ""})
        self.assertEqual(result, "")

    def test_repeated_token(self):
        result = resolve_form_variables("%%x%% e %%x%%", {"x": "A"})
        self.assertEqual(result, "A e A")


# --------------------------------------------------------------------------- #
# Adversarial: token parsing (VARIABLE_RE / find_variable_names)
# --------------------------------------------------------------------------- #
class TestTokenParsingAdversarial(unittest.TestCase):

    def test_empty_token_does_not_match(self):
        # %%%% has no non-% char between the pairs, so the "+" fails.
        self.assertEqual(find_variable_names("%%%%"), [])

    def test_unterminated_token_no_closing(self):
        self.assertEqual(find_variable_names("%%name"), [])

    def test_unterminated_token_single_trailing_percent(self):
        self.assertEqual(find_variable_names("%%name%"), [])

    def test_lone_double_percent(self):
        self.assertEqual(find_variable_names("%%"), [])

    def test_percent_inside_normal_text(self):
        # "50%% off": the second %% is followed by a space, so nothing matches.
        self.assertEqual(find_variable_names("50%% off"), [])

    def test_internal_percent_breaks_token(self):
        # A % inside the name is excluded by [^%\s]; no closing pair aligns.
        self.assertEqual(find_variable_names("%%a%b%%"), [])

    def test_double_token_with_middle_text(self):
        # "%%a%%b%%": first pair closes after 'a'; the trailing "b%%" is inert.
        self.assertEqual(find_variable_names("%%a%%b%%"), ["a"])

    def test_triple_delimited_token(self):
        # The inner %% pair anchors; extra surrounding % are absorbed.
        self.assertEqual(find_variable_names("%%%name%%%"), ["name"])

    def test_adjacent_tokens(self):
        self.assertEqual(find_variable_names("%%a%%%%b%%"), ["a", "b"])

    def test_token_at_string_start_and_end(self):
        self.assertEqual(find_variable_names("%%a%%"), ["a"])
        self.assertEqual(find_variable_names("x%%a%%"), ["a"])
        self.assertEqual(find_variable_names("%%a%%x"), ["a"])

    def test_tab_inside_token_does_not_match(self):
        self.assertEqual(find_variable_names("%%\t%%"), [])

    def test_unicode_token_name(self):
        self.assertEqual(find_variable_names("%%café%%"), ["café"])
        self.assertEqual(find_variable_names("%%naïve_ção%%"), ["naïve_ção"])

    def test_token_with_dots_digits_hyphens(self):
        self.assertEqual(find_variable_names("%%a.b-c1%%"), ["a.b-c1"])

    def test_extremely_long_token_name(self):
        name = "z" * 5000
        self.assertEqual(find_variable_names(f"%%{name}%%"), [name])


# --------------------------------------------------------------------------- #
# Adversarial: classification (classify_variable)
# --------------------------------------------------------------------------- #
class TestClassifyVariableAdversarial(unittest.TestCase):

    def test_sentinel_callable_classifies_without_invoking(self):
        # sync_export relies on this: a dynamic trigger is mapped to a sentinel
        # callable so classify still returns 'dynamic_ref' — and must NOT run it.
        def sentinel():
            raise AssertionError("classify_variable must never invoke the callable")

        self.assertEqual(classify_variable("xdolar", {"xdolar": sentinel}), "dynamic_ref")

    def test_bare_mapping_prefix_is_form_field(self):
        # "cpf" alone (prefix with no item) is not a composed trigger.
        snippets = {"_cpf_numbers": {"fulano": "123"}}
        self.assertEqual(classify_variable("cpf", snippets), "form_field")

    def test_prefix_plus_reserved_prefix_item_is_form_field(self):
        # The synthetic "__prefix__" entry is not a resolvable mapping item.
        snippets = {"_custom_codes": {"__prefix__": "cc", "x": "ABC"}}
        self.assertEqual(classify_variable("cc__prefix__", snippets), "form_field")

    def test_direct_snippet_key_beats_mapping_pattern(self):
        # A literal key wins over the composed mapping that would also match.
        snippets = {"cpffulano": "static-value", "_cpf_numbers": {"fulano": "123"}}
        self.assertEqual(classify_variable("cpffulano", snippets), "snippet_ref")

    def test_callable_at_composed_name_is_dynamic_ref(self):
        snippets = {"cpffulano": lambda: "x", "_cpf_numbers": {"fulano": "123"}}
        self.assertEqual(classify_variable("cpffulano", snippets), "dynamic_ref")

    def test_unicode_snippet_key_is_snippet_ref(self):
        self.assertEqual(classify_variable("café", {"café": "coffee"}), "snippet_ref")

    def test_clipboard_wins_even_with_matching_snippet(self):
        # Priority order: clipboard-paste is resolved before any snippet lookup.
        snippets = {"clipboard-paste": "shadow"}
        self.assertEqual(classify_variable("clipboard-paste", snippets), "clipboard")


# --------------------------------------------------------------------------- #
# Adversarial: inline resolution (resolve_inline / _resolve_dynamic)
# --------------------------------------------------------------------------- #
class TestResolveInlineAdversarial(unittest.TestCase):

    def test_callable_returning_int_is_stringified(self):
        result = resolve_inline("n=%%x%%", {"x": lambda: 42}, lambda: None)
        self.assertEqual(result, "n=42")

    def test_callable_returning_falsy_zero_becomes_empty(self):
        # 0 is falsy, so _resolve_dynamic substitutes "" (same path as None).
        result = resolve_inline("n=%%x%%", {"x": lambda: 0}, lambda: None)
        self.assertEqual(result, "n=")

    def test_callable_returning_rich_text_dict_uses_plain_text(self):
        calls = []
        rich = {"__kind__": "rich_text", "text": "Olá", "spans": []}
        result = resolve_inline(
            "%%x%%", {"x": lambda: rich}, lambda: None,
            notify_failure=lambda name, value: calls.append((name, value)),
        )
        self.assertEqual(result, "Olá")
        # The raw (dict) result is handed to notify_failure, not the plain text.
        self.assertEqual(calls, [("x", rich)])

    def test_repeated_dynamic_token_invoked_once_all_replaced(self):
        counter = {"n": 0}

        def once():
            counter["n"] += 1
            return "V"

        result = resolve_inline("%%x%% and %%x%%", {"x": once}, lambda: None)
        self.assertEqual(result, "V and V")
        self.assertEqual(counter["n"], 1)  # invoked once despite two occurrences

    def test_clipboard_injected_token_is_not_reresolved(self):
        # Clipboard content that looks like a token stays literal when that token
        # is not independently referenced in the original text.
        result = resolve_inline("%%clipboard-paste%%", {}, lambda: "%%nome%%")
        self.assertEqual(result, "%%nome%%")

    def test_clipboard_injection_hits_a_token_present_at_top_level(self):
        # Characterization of the global str.replace: because 'xcity' is already
        # in the name list, the token injected via clipboard is also substituted.
        snippets = {"xcity": "SP"}
        result = resolve_inline(
            "%%clipboard-paste%% %%xcity%%", snippets, lambda: "%%xcity%%"
        )
        self.assertEqual(result, "SP SP")

    def test_snippet_ref_is_one_level_deep(self):
        # An embedded token inside a referenced snippet is left unresolved when it
        # is not otherwise present at the top level (documented one-level rule).
        snippets = {"xaddr": "Rua %%xcity%%", "xcity": "SP"}
        result = resolve_inline("%%xaddr%%", snippets, lambda: None)
        self.assertEqual(result, "Rua %%xcity%%")

    def test_embedded_token_resolved_only_via_top_level_reference(self):
        # Same data, but 'xcity' also appears at top level, so global replace
        # reaches the copy embedded by xaddr's value too.
        snippets = {"xaddr": "Rua %%xcity%%", "xcity": "SP"}
        result = resolve_inline("%%xaddr%% - %%xcity%%", snippets, lambda: None)
        self.assertEqual(result, "Rua SP - SP")

    def test_unicode_snippet_ref_resolved(self):
        result = resolve_inline("%%café%%", {"café": "coffee"}, lambda: None)
        self.assertEqual(result, "coffee")

    def test_seen_guard_applies_to_mapping_ref(self):
        # The _seen short-circuit sits before the mapping branch too.
        snippets = {"_cpf_numbers": {"fulano": "123"}}
        result = resolve_inline(
            "%%cpffulano%%", snippets, lambda: None, _seen={"cpffulano"}
        )
        self.assertEqual(result, "%%cpffulano%%")

    def test_self_reference_without_seen_does_not_hang(self):
        # A snippet referencing itself must terminate: resolve_inline never
        # recurses, so it expands exactly one level and stops.
        snippets = {"xself": "a %%xself%% b"}
        result, timed_out = _resolve_in_thread("%%xself%%", snippets, lambda: None)
        self.assertFalse(timed_out, "resolve_inline hung on a self-reference")
        self.assertEqual(result, "a %%xself%% b")

    def test_mutual_cycle_without_seen_does_not_hang(self):
        snippets = {"A": "%%B%%", "B": "%%A%%"}
        result, timed_out = _resolve_in_thread(
            "%%A%% %%B%%", snippets, lambda: None
        )
        self.assertFalse(timed_out, "resolve_inline hung on a mutual cycle")
        # Single left-to-right pass swaps each token once; no further passes.
        self.assertEqual(result, "%%A%% %%A%%")

    def test_notify_failure_is_optional_on_raising_callable(self):
        # Without a notify callback, a raising dynamic ref still yields "".
        def boom():
            raise RuntimeError("x")

        result = resolve_inline("v=%%x%%", {"x": boom}, lambda: None)
        self.assertEqual(result, "v=")


# --------------------------------------------------------------------------- #
# Adversarial: form-variable substitution (resolve_form_variables)
# --------------------------------------------------------------------------- #
class TestResolveFormVariablesAdversarial(unittest.TestCase):

    def test_value_with_token_stays_literal_when_no_matching_key(self):
        result = resolve_form_variables("%%nome%%", {"nome": "%%evil%%"})
        self.assertEqual(result, "%%evil%%")

    def test_form_values_can_chain_through_replace(self):
        # Characterization: values are substituted in dict order; a value that is
        # itself a later field's token gets resolved on the subsequent pass.
        result = resolve_form_variables(
            "%%nome%%", {"nome": "%%data%%", "data": "X"}
        )
        self.assertEqual(result, "X")

    def test_unicode_form_value(self):
        result = resolve_form_variables("%%c%%", {"c": "café ☕"})
        self.assertEqual(result, "café ☕")


if __name__ == "__main__":
    unittest.main()
