import json
import os
import unittest
from pathlib import Path
from unittest import mock

from snippet_utils import (
    check_dynamic_pattern,
    get_dynamic_prefixes,
    load_json_file,
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

    def test_load_json_file_reads_utf8_json(self):
        path = self.make_test_path('load')
        path.write_text('{"x": "á"}', encoding='utf-8')

        self.assertEqual({"x": "á"}, load_json_file(path))

    def test_write_json_atomic_replaces_existing_json(self):
        path = self.make_test_path('write')
        path.write_text('{"old": true}', encoding='utf-8')

        write_json_atomic(path, {"new": "value"})

        self.assertEqual({"new": "value"}, json.loads(path.read_text(encoding='utf-8')))

    def test_write_json_atomic_retries_transient_permission_errors(self):
        path = self.make_test_path('retry')
        path.write_text('{"old": true}', encoding='utf-8')
        original_replace = os.replace
        attempts = []

        def flaky_replace(source, destination):
            attempts.append((source, destination))
            if len(attempts) < 3:
                raise PermissionError("destination is temporarily busy")
            original_replace(source, destination)

        with mock.patch("snippet_utils.os.replace", side_effect=flaky_replace), mock.patch(
            "snippet_utils.time.sleep"
        ) as sleep:
            write_json_atomic(path, {"new": "value"})

        self.assertEqual(3, len(attempts))
        self.assertEqual([mock.call(0.01), mock.call(0.02)], sleep.call_args_list)
        self.assertEqual({"new": "value"}, json.loads(path.read_text(encoding='utf-8')))

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
