import json
import unittest
from pathlib import Path

from snippet_utils import (
    build_saveable_snippets,
    find_shadowed_statics,
    calculate_max_trigger_length_with_mappings,
    check_dynamic_pattern,
    get_default_snippets,
    get_dynamic_prefixes,
    load_json_file,
    merge_snippets,
    validate_static_snippets,
    write_json_atomic,
)


class SnippetUtilsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_root = Path(__file__).resolve().parent / "tmp"
        cls.temp_root.mkdir(exist_ok=True)

    def setUp(self):
        self.snippets = {
            "xname": "Alex",
            "xsig": "Assinatura",
            "_cpf_numbers": {
                "fulano": "123.456.789-00",
            },
            "_cnpj_numbers": {
                "empresa": "12.345.678/0001-90",
            },
            "_service_codes": {
                "__prefix__": "clw",
                "gtw": "service gateway restart",
            },
            "_email_codes": {
                "work": "work@example.com",
            },
        }

    def tearDown(self):
        for path in self.temp_root.glob('snippet-utils-test-*.json'):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def make_test_path(self, suffix):
        return self.temp_root / f"snippet-utils-test-{suffix}.json"

    def test_default_snippets_returns_fresh_copy(self):
        first = get_default_snippets()
        second = get_default_snippets()

        first["_cpf_numbers"]["fulano"] = "changed"

        self.assertEqual("123.456.789-00", second["_cpf_numbers"]["fulano"])

    def test_validate_static_snippets_accepts_dict_root(self):
        self.assertEqual({"x": "y"}, validate_static_snippets({"x": "y"}))

    def test_validate_static_snippets_rejects_non_dict_root(self):
        self.assertIsNone(validate_static_snippets(["not", "a", "dict"]))

    def test_load_json_file_reads_utf8_json(self):
        path = self.make_test_path('load')
        path.write_text('{"x": "á"}', encoding='utf-8')

        self.assertEqual({"x": "á"}, load_json_file(path))

    def test_write_json_atomic_replaces_existing_json(self):
        path = self.make_test_path('write')
        path.write_text('{"old": true}', encoding='utf-8')

        write_json_atomic(path, {"new": "value"})

        self.assertEqual({"new": "value"}, json.loads(path.read_text(encoding='utf-8')))

    def test_build_saveable_snippets_filters_runtime_callables(self):
        saveable = build_saveable_snippets({"xname": "Alex", "xdyn": lambda: "value"})

        self.assertEqual({"xname": "Alex"}, saveable)

    def test_find_shadowed_statics_reports_names_taken_by_a_callable(self):
        shadowed = find_shadowed_statics(
            {"xhj": "meu texto", "xname": "Alex"},
            {"xhj": lambda: "data de hoje"},
        )

        self.assertEqual({"xhj": "meu texto"}, shadowed)

    def test_build_saveable_snippets_keeps_statics_shadowed_by_a_callable(self):
        # Regression: merging a dynamic trigger over a static of the same name
        # used to delete the static key from snippets.json on the next save.
        static = {"xhj": "meu texto importante"}
        dynamic = {"xhj": lambda: "data de hoje"}
        merged = merge_snippets(static, dynamic)

        saveable = build_saveable_snippets(merged, find_shadowed_statics(static, dynamic))

        self.assertEqual({"xhj": "meu texto importante"}, saveable)

    def test_build_saveable_snippets_prefers_the_live_value_over_the_shadow(self):
        saveable = build_saveable_snippets({"xhj": "editado"}, {"xhj": "antigo"})

        self.assertEqual({"xhj": "editado"}, saveable)

    def test_merge_snippets_keeps_dynamic_priority(self):
        merged = merge_snippets({"xhj": "static"}, {"xhj": "dynamic", "xnow": "dynamic-now"})

        self.assertEqual("dynamic", merged["xhj"])
        self.assertEqual("dynamic-now", merged["xnow"])

    def test_get_dynamic_prefixes_includes_builtin_and_custom_mappings(self):
        prefixes = get_dynamic_prefixes(self.snippets)

        self.assertEqual("_cpf_numbers", prefixes["cpf"])
        self.assertEqual("_cnpj_numbers", prefixes["cnpj"])
        self.assertEqual("_service_codes", prefixes["clw"])
        self.assertEqual("_email_codes", prefixes["email"])

    def test_check_dynamic_pattern_resolves_builtin_mapping(self):
        value, trigger_length = check_dynamic_pattern(self.snippets, "cpffulano")

        self.assertEqual("123.456.789-00", value)
        self.assertEqual(len("cpffulano"), trigger_length)

    def test_check_dynamic_pattern_resolves_custom_prefix(self):
        prefixes = get_dynamic_prefixes(self.snippets)
        value, trigger_length = check_dynamic_pattern(self.snippets, "clwgtw", prefixes)

        self.assertEqual("service gateway restart", value)
        self.assertEqual(len("clwgtw"), trigger_length)

    def test_check_dynamic_pattern_ignores_prefix_metadata(self):
        value, trigger_length = check_dynamic_pattern(self.snippets, "clw__prefix__")

        self.assertIsNone(value)
        self.assertEqual(0, trigger_length)

    def test_calculate_max_trigger_length_with_mappings_counts_full_dynamic_trigger(self):
        snippets = {
            "x": "y",
            "_custom_codes": {
                "__prefix__": "clw",
                "verylongidentifier": "value",
            },
        }

        self.assertEqual(len("clwverylongidentifier"), calculate_max_trigger_length_with_mappings(snippets))


class ValidateStaticSnippetsRejectionTests(unittest.TestCase):
    def test_rejects_non_dict_roots(self):
        for bad in (["a"], "string", 42, 3.14, None, True):
            self.assertIsNone(validate_static_snippets(bad), repr(bad))

    def test_accepts_empty_dict(self):
        self.assertEqual({}, validate_static_snippets({}))


class CheckDynamicPatternEdgeTests(unittest.TestCase):
    def setUp(self):
        self.snippets = {
            "_cpf_numbers": {"fulano": "123.456.789-00"},
            "_service_codes": {"__prefix__": "clw", "gtw": "restart"},
        }

    def test_text_equal_to_prefix_returns_none(self):
        value, length = check_dynamic_pattern(self.snippets, "cpf")
        self.assertIsNone(value)
        self.assertEqual(0, length)

    def test_unknown_prefix_returns_none(self):
        value, length = check_dynamic_pattern(self.snippets, "zzznope")
        self.assertIsNone(value)
        self.assertEqual(0, length)

    def test_mapping_container_that_is_not_a_dict_is_ignored(self):
        # A garbage mapping value must not crash resolution.
        value, _ = check_dynamic_pattern({"_cpf_numbers": "notadict"}, "cpffulano")
        self.assertIsNone(value)

    def test_dunder_prefix_renames_the_typed_prefix(self):
        value, length = check_dynamic_pattern(self.snippets, "clwgtw")
        self.assertEqual("restart", value)
        self.assertEqual(len("clwgtw"), length)


class GetDynamicPrefixesEdgeTests(unittest.TestCase):
    def test_absent_builtin_is_not_registered(self):
        self.assertEqual({}, get_dynamic_prefixes({"xname": "value"}))

    def test_underscore_key_not_ending_in_numbers_or_codes_is_ignored(self):
        self.assertEqual({}, get_dynamic_prefixes({"_internal_flag": True}))

    def test_dunder_prefix_overrides_the_derived_name(self):
        prefixes = get_dynamic_prefixes({"_service_codes": {"__prefix__": "clw"}})
        self.assertEqual("_service_codes", prefixes["clw"])
        self.assertNotIn("service", prefixes)

    def test_non_dict_mapping_still_registers_a_prefix_but_resolves_to_nothing(self):
        prefixes = get_dynamic_prefixes({"_bad_numbers": "notadict"})
        self.assertEqual("_bad_numbers", prefixes["bad"])
        # resolution against the bogus container yields no value (no crash).
        self.assertIsNone(check_dynamic_pattern({"_bad_numbers": "notadict"}, "badx", prefixes)[0])

    def test_invalid_custom_prefix_falls_back_to_derived_prefix(self):
        for invalid in (None, 42, []):
            snippets = {"_service_codes": {"__prefix__": invalid, "restart": "ok"}}
            prefixes = get_dynamic_prefixes(snippets)
            self.assertEqual("_service_codes", prefixes["service"])
            self.assertTrue(all(isinstance(prefix, str) for prefix in prefixes))
            self.assertEqual(
                ("ok", len("servicerestart")),
                check_dynamic_pattern(snippets, "servicerestart", prefixes),
            )

    def test_invalid_custom_prefix_does_not_break_max_length(self):
        snippets = {"_service_codes": {"__prefix__": None, "restart": "ok"}}
        self.assertEqual(
            len("servicerestart"),
            calculate_max_trigger_length_with_mappings(snippets),
        )


class MergeAndShadowRoundTripTests(unittest.TestCase):
    def test_full_save_round_trip_keeps_shadow_and_strips_all_callables(self):
        static = {"xhj": "important", "xname": "Alex", "_cpf_numbers": {"fulano": "1"}}
        dynamic = {"xhj": lambda: "date", "xdolar": lambda: "R$5"}

        merged = merge_snippets(static, dynamic)
        preserved = find_shadowed_statics(static, dynamic)
        saveable = build_saveable_snippets(merged, preserved)

        self.assertEqual("important", saveable["xhj"])          # shadowed static survives
        self.assertEqual("Alex", saveable["xname"])
        self.assertEqual({"fulano": "1"}, saveable["_cpf_numbers"])  # mapping container kept
        self.assertNotIn("xdolar", saveable)                    # runtime-only callable dropped
        self.assertFalse(any(callable(v) for v in saveable.values()))

    def test_merge_does_not_mutate_its_inputs(self):
        static = {"a": "1"}
        dynamic = {"b": "2"}
        merge_snippets(static, dynamic)
        self.assertEqual({"a": "1"}, static)
        self.assertEqual({"b": "2"}, dynamic)

    def test_find_shadowed_statics_excludes_a_callable_static(self):
        # Defensive: a static value that is itself callable is never "preserved".
        self.assertEqual({}, find_shadowed_statics({"x": lambda: 1}, {"x": lambda: 2}))


class CalculateMaxTriggerLengthEdgeTests(unittest.TestCase):
    def test_empty_snippets_use_fallback(self):
        self.assertEqual(20, calculate_max_trigger_length_with_mappings({}))

    def test_with_mappings_ignores_non_dict_mapping_values(self):
        snippets = {"x": "y", "_cpf_numbers": "notadict"}
        # Must not crash; the mapping key itself is the longest counted trigger.
        self.assertEqual(
            len("_cpf_numbers"),
            calculate_max_trigger_length_with_mappings(snippets),
        )


class WriteJsonAtomicFailureTests(unittest.TestCase):
    """The atomic write must never clobber the existing file or leak temp files."""

    @classmethod
    def setUpClass(cls):
        cls.temp_root = Path(__file__).resolve().parent / "tmp"
        cls.temp_root.mkdir(exist_ok=True)

    def tearDown(self):
        for pattern in ("atomic-fail-*.json", "atomic-fail-*.json.*", "atomic-fail-*.tmp"):
            for path in self.temp_root.glob(pattern):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass

    def path(self, name):
        return self.temp_root / f"atomic-fail-{name}.json"

    def test_non_serializable_data_raises_and_leaves_existing_file_intact(self):
        target = self.path("existing")
        target.write_text('{"keep": true}', encoding="utf-8")

        with self.assertRaises(TypeError):
            write_json_atomic(str(target), {"bad": {1, 2, 3}})  # set is not JSON-serializable

        self.assertEqual({"keep": True}, json.loads(target.read_text(encoding="utf-8")))
        self.assertEqual([], list(self.temp_root.glob("atomic-fail-existing.json.*")))

    def test_failed_write_to_fresh_path_creates_no_file(self):
        target = self.path("fresh")
        with self.assertRaises(TypeError):
            write_json_atomic(str(target), {"f": lambda: 1})
        self.assertFalse(target.exists())

    def test_round_trips_unicode_with_ensure_ascii_false(self):
        target = self.path("unicode")
        write_json_atomic(str(target), {"emoji": "🎉", "accent": "café"})
        self.assertEqual({"emoji": "🎉", "accent": "café"}, load_json_file(str(target)))


class LoadJsonFileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_root = Path(__file__).resolve().parent / "tmp"
        cls.temp_root.mkdir(exist_ok=True)

    def tearDown(self):
        for path in self.temp_root.glob("load-json-*.json"):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def test_invalid_json_raises(self):
        path = self.temp_root / "load-json-invalid.json"
        path.write_text("{not valid", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            load_json_file(str(path))


if __name__ == "__main__":
    unittest.main()
