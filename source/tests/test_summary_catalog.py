import unittest

from summary_catalog import DEFAULT_SUMMARY_MODEL, summary_catalog, summary_catalog_entry


class SummaryCatalogTests(unittest.TestCase):
    def test_catalog_is_small_pinned_and_has_default(self):
        entries = summary_catalog()
        self.assertEqual(
            {entry["parameters"] for entry in entries},
            {"2B", "4B", "E2B / 5B", "E4B / 8B"},
        )
        self.assertEqual("qwen3.5-2b-q4", DEFAULT_SUMMARY_MODEL)
        self.assertIsNotNone(summary_catalog_entry(DEFAULT_SUMMARY_MODEL))
        for entry in entries:
            self.assertTrue(entry["url"].startswith("https://huggingface.co/"))
            self.assertNotIn("/resolve/main/", entry["url"])
            self.assertRegex(entry["sha256"], r"^[0-9a-f]{64}$")
            self.assertGreater(entry["size_bytes"], 500_000_000)
            self.assertLess(entry["size_bytes"], 5_200_000_000)

    def test_catalog_runs_from_lowest_to_highest_quality(self):
        self.assertEqual(
            [entry["id"] for entry in summary_catalog()],
            [
                "qwen3.5-2b-q4",
                "gemma-4-e2b-q4",
                "qwen3.5-4b-q4",
                "gemma-4-e4b-q4",
            ],
        )
        labels = [entry["label"] for entry in summary_catalog()]
        self.assertEqual(len(labels), len(set(labels)))

    def test_current_families_need_no_license_gate(self):
        entries = summary_catalog()
        self.assertEqual(
            {entry["upstream_model"] for entry in entries},
            {
                "Qwen/Qwen3.5-2B",
                "Qwen/Qwen3.5-4B",
                "google/gemma-4-E2B-it",
                "google/gemma-4-E4B-it",
            },
        )
        for removed in ("qwen3.5-0.8b-q4", "lfm2.5-2.6b-q4", "granite-4.2-3b-q4"):
            self.assertIsNone(summary_catalog_entry(removed))
        self.assertTrue(
            all(entry["license_id"] == "Apache-2.0" and not entry["requires_acceptance"]
                for entry in entries)
        )


if __name__ == "__main__":
    unittest.main()
