import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from settings_support import load_settings, normalize_runtime_settings, save_settings


class SettingsSupportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "settings.json")

    def _write(self, content):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(content)

    def test_load_missing_returns_empty(self):
        self.assertEqual(load_settings(self.path), {})

    def test_load_malformed_json_returns_empty(self):
        # Truncated, empty, whitespace-only and partially-typed all degrade to {}.
        for content in ("{ not json", "", "   ", "{", '{"terminator_mode": tr', '{"a":'):
            with self.subTest(content=content):
                self._write(content)
                self.assertEqual(load_settings(self.path), {})

    def test_load_wrong_root_type_returns_empty(self):
        # Valid JSON but not an object: a list/scalar must not become settings.
        for content in ("[1, 2]", '"just a string"', "42", "3.14", "true", "null"):
            with self.subTest(content=content):
                self._write(content)
                self.assertEqual(load_settings(self.path), {})

    def test_save_and_load_roundtrip(self):
        self.assertTrue(save_settings(self.path, {"mirror_dir": "D:/cloud"}))
        self.assertEqual(load_settings(self.path), {"mirror_dir": "D:/cloud"})

    def test_load_preserves_unknown_keys_and_value_types(self):
        # settings_support does no schema validation; the app owns value semantics.
        # A wrongly-typed known key or an unknown key must pass through untouched
        # (degrade gracefully), not crash or get dropped.
        weird = {
            "terminator_mode": "yes",   # app expects bool; loader must not coerce
            "mirror_dir": 5,            # app expects str
            "sync_export_dir": None,
            "totally_unknown_key": [1, 2, 3],
        }
        self.assertTrue(save_settings(self.path, weird))
        self.assertEqual(load_settings(self.path), weird)

    def test_roundtrip_preserves_unicode(self):
        settings = {"mirror_dir": "C:/Área de Trabalho/☂/据"}
        self.assertTrue(save_settings(self.path, settings))
        self.assertEqual(load_settings(self.path), settings)
        # Written non-ASCII, not \u escapes (write_json_atomic uses ensure_ascii=False).
        with open(self.path, encoding="utf-8") as handle:
            self.assertIn("Área de Trabalho", handle.read())

    def test_save_returns_false_on_write_error(self):
        # Target directory does not exist: the atomic write cannot create its temp
        # file, so save must report failure instead of raising.
        bad_path = os.path.join(self.tmp, "no-such-dir", "settings.json")
        self.assertFalse(save_settings(bad_path, {"mirror_dir": "x"}))
        self.assertFalse(os.path.exists(bad_path))


class RuntimeSettingsNormalizationTests(unittest.TestCase):
    def test_malformed_known_values_fall_back_without_dropping_unknown_keys(self):
        normalized, invalid = normalize_runtime_settings({
            "terminator_mode": "false",
            "bcb_timeout": "3",
            "bcb_cache_seconds": True,
            "stock_cache_seconds": float("inf"),
            "mirror_dir": 5,
            "future_key": {"kept": True},
        })

        self.assertFalse(normalized["terminator_mode"])
        self.assertEqual(normalized["bcb_timeout"], 3)
        self.assertEqual(normalized["bcb_cache_seconds"], 300)
        self.assertEqual(normalized["stock_cache_seconds"], 600)
        self.assertIsNone(normalized["mirror_dir"])
        self.assertEqual(normalized["future_key"], {"kept": True})
        self.assertEqual(
            set(invalid),
            {
                "terminator_mode",
                "bcb_timeout",
                "bcb_cache_seconds",
                "stock_cache_seconds",
                "mirror_dir",
            },
        )

    def test_valid_runtime_values_are_preserved(self):
        settings = {
            "terminator_mode": True,
            "bcb_timeout": 0.5,
            "bcb_cache_seconds": 0,
            "stock_cache_seconds": 3600,
            "sync_export_dir": " C:/sync ",
        }
        normalized, invalid = normalize_runtime_settings(settings)

        self.assertEqual(invalid, {})
        self.assertTrue(normalized["terminator_mode"])
        self.assertEqual(normalized["bcb_timeout"], 0.5)
        self.assertEqual(normalized["bcb_cache_seconds"], 0)
        self.assertEqual(normalized["stock_cache_seconds"], 3600)
        self.assertEqual(normalized["sync_export_dir"], "C:/sync")


if __name__ == "__main__":
    unittest.main()
