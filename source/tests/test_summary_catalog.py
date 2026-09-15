import unittest

from summary_catalog import DEFAULT_SUMMARY_MODEL, summary_catalog, summary_catalog_entry


class SummaryCatalogTests(unittest.TestCase):
    def test_catalog_is_small_pinned_and_has_default(self):
        entries = summary_catalog()
        self.assertEqual({entry["parameters"] for entry in entries}, {"1B", "1.7B", "2B"})
        self.assertIsNotNone(summary_catalog_entry(DEFAULT_SUMMARY_MODEL))
        for entry in entries:
            self.assertTrue(entry["url"].startswith("https://huggingface.co/"))
            self.assertRegex(entry["sha256"], r"^[0-9a-f]{64}$")
            self.assertGreater(entry["size_bytes"], 700_000_000)
            self.assertLess(entry["size_bytes"], 2_000_000_000)

    def test_gemma_requires_explicit_license_acceptance(self):
        gemma = summary_catalog_entry("gemma-3-1b-q4")
        self.assertTrue(gemma["requires_acceptance"])
        self.assertIn("gemma", gemma["license_url"])


if __name__ == "__main__":
    unittest.main()
