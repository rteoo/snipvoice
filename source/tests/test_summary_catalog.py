import unittest

from summary_catalog import DEFAULT_SUMMARY_MODEL, summary_catalog, summary_catalog_entry


class SummaryCatalogTests(unittest.TestCase):
    def test_catalog_is_small_pinned_and_has_default(self):
        entries = summary_catalog()
        self.assertEqual(
            {entry["parameters"] for entry in entries},
            {"2B", "3B", "4B", "E2B / 5B"},
        )
        self.assertIsNotNone(summary_catalog_entry(DEFAULT_SUMMARY_MODEL))
        for entry in entries:
            self.assertTrue(entry["url"].startswith("https://huggingface.co/"))
            self.assertNotIn("/resolve/main/", entry["url"])
            self.assertRegex(entry["sha256"], r"^[0-9a-f]{64}$")
            self.assertGreater(entry["size_bytes"], 700_000_000)
            self.assertLess(entry["size_bytes"], 3_500_000_000)

    def test_current_families_are_used_and_apache_licensed(self):
        entries = summary_catalog()
        self.assertEqual(
            {entry["upstream_model"] for entry in entries},
            {
                "Qwen/Qwen3.5-2B",
                "Qwen/Qwen3.5-4B",
                "ibm-granite/granite-4.2-3b",
                "google/gemma-4-E2B-it",
            },
        )
        self.assertTrue(all(entry["license_id"] == "Apache-2.0" for entry in entries))
        self.assertTrue(all(not entry["requires_acceptance"] for entry in entries))


if __name__ == "__main__":
    unittest.main()
