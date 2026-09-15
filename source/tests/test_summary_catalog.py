import unittest

from summary_catalog import DEFAULT_SUMMARY_MODEL, summary_catalog, summary_catalog_entry


class SummaryCatalogTests(unittest.TestCase):
    def test_catalog_is_small_pinned_and_has_default(self):
        entries = summary_catalog()
        self.assertEqual(
            {entry["parameters"] for entry in entries},
            {"0.8B", "2B", "2.6B", "4B", "E2B / 5B", "E4B / 8B"},
        )
        self.assertEqual("qwen3.5-2b-q4", DEFAULT_SUMMARY_MODEL)
        self.assertIsNotNone(summary_catalog_entry(DEFAULT_SUMMARY_MODEL))
        for entry in entries:
            self.assertTrue(entry["url"].startswith("https://huggingface.co/"))
            self.assertNotIn("/resolve/main/", entry["url"])
            self.assertRegex(entry["sha256"], r"^[0-9a-f]{64}$")
            self.assertGreater(entry["size_bytes"], 500_000_000)
            self.assertLess(entry["size_bytes"], 5_200_000_000)

    def test_current_families_and_license_gate_are_explicit(self):
        entries = summary_catalog()
        self.assertEqual(
            {entry["upstream_model"] for entry in entries},
            {
                "Qwen/Qwen3.5-0.8B",
                "Qwen/Qwen3.5-2B",
                "Qwen/Qwen3.5-4B",
                "LiquidAI/LFM2.5-2.6B",
                "google/gemma-4-E2B-it",
                "google/gemma-4-E4B-it",
            },
        )
        liquid = summary_catalog_entry("lfm2.5-2.6b-q4")
        self.assertEqual(liquid["license_id"], "LFM Open License v1.0")
        self.assertTrue(liquid["requires_acceptance"])
        self.assertIn("não é MIT nem Apache-2.0", liquid["license_notice"])
        self.assertIn("US$ 10 milhões", liquid["license_notice"])
        self.assertIsNone(summary_catalog_entry("granite-4.2-3b-q4"))
        budget = summary_catalog_entry("qwen3.5-0.8b-q4")
        self.assertEqual(budget["size_bytes"], 527_502_816)
        self.assertEqual(
            budget["sha256"],
            "f5b14da98939b60bbe1019a964eba656407e1e0b64f1fe3003ff6d650e93bfec",
        )
        self.assertTrue(
            all(entry["license_id"] == "Apache-2.0" and not entry["requires_acceptance"]
                for entry in entries if entry["id"] != liquid["id"])
        )


if __name__ == "__main__":
    unittest.main()
