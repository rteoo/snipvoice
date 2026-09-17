import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import summary_models


class FakeResponse:
    status = 200
    headers = {}

    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def geturl(self):
        return "https://example.test/model.gguf"

    def read(self, _size):
        payload, self.payload = self.payload, b""
        return payload


class SummaryModelsTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).resolve().parent / "tmp"
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)

    def test_summary_cache_is_separate_and_overrideable(self):
        with mock.patch.dict(os.environ, {"SNIPVOICE_SUMMARY_CACHE": self.temp.name}):
            self.assertEqual(summary_models.default_summary_cache_dir(), self.temp.name)
        self.assertIn("summary-models", summary_models.default_summary_cache_dir("windows"))

    def test_download_wrapper_reuses_verified_atomic_downloader(self):
        payload = b"local gguf"
        entry = {
            "id": "tiny", "profile": "tiny", "filename": "tiny.gguf",
            "url": "https://example.test/model.gguf",
            "sha256": hashlib.sha256(payload).hexdigest(), "size_bytes": len(payload),
            "license_id": "MIT", "upstream_model": "example/tiny",
        }
        with mock.patch.object(summary_models, "summary_catalog_entry", return_value=entry):
            path = summary_models.download_summary_model(
                "tiny", self.temp.name, opener=lambda *_args, **_kwargs: FakeResponse(payload),
            )
            self.assertTrue(summary_models.summary_model_is_installed("tiny", self.temp.name))
            self.assertEqual(Path(path).read_bytes(), payload)
            summary_models.delete_summary_model("tiny", self.temp.name)
            self.assertFalse(Path(path).exists())

    def test_summary_wrapper_reports_shared_installation_without_taking_ownership(self):
        payload = b"shared local llm"
        entry = {
            "id": "tiny", "profile": "tiny", "filename": "tiny.gguf",
            "url": "https://example.test/model.gguf",
            "sha256": hashlib.sha256(payload).hexdigest(), "size_bytes": len(payload),
            "license_id": "MIT", "upstream_model": "example/tiny",
        }
        shared = Path(self.temp.name) / "lm-studio" / "tiny.gguf"
        shared.parent.mkdir()
        shared.write_bytes(payload)
        with mock.patch.object(summary_models, "summary_catalog_entry", return_value=entry):
            self.assertEqual(
                summary_models.summary_model_installation("tiny", self.temp.name),
                {"path": str(shared), "managed": False},
            )


if __name__ == "__main__":
    unittest.main()
