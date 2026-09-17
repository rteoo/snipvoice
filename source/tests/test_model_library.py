import os
from pathlib import Path
import tempfile
import unittest

from model_library import (
    MODEL_CATEGORIES,
    ensure_model_library,
    model_category_dir,
    model_library_payload,
    normalize_model_library_root,
    resolve_model_library_root,
)


class ModelLibraryTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).resolve().parent / "tmp"
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)

    def test_shared_root_uses_stable_model_categories(self):
        root = normalize_model_library_root(self.temp.name)
        self.assertEqual(
            [model_category_dir(root, category) for category in MODEL_CATEGORIES],
            [os.path.join(root, category) for category in ("llm", "tts", "asr")],
        )
        self.assertEqual(model_library_payload(root), {"local_models_root": root})

    def test_saved_root_creates_only_category_directories(self):
        created = ensure_model_library(self.temp.name)
        self.assertEqual(len(created), 3)
        self.assertEqual(
            sorted(item.name for item in Path(self.temp.name).iterdir()),
            ["asr", "llm", "tts"],
        )

    def test_invalid_saved_root_falls_back_without_breaking_startup(self):
        warnings = []
        self.assertIsNone(resolve_model_library_root(
            {"local_models_root": "relative/models"}, warnings,
        ))
        self.assertEqual(len(warnings), 1)

    def test_empty_root_keeps_application_defaults(self):
        self.assertIsNone(normalize_model_library_root(""))
        self.assertEqual(model_library_payload(None), {"local_models_root": ""})


if __name__ == "__main__":
    unittest.main()
