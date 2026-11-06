"""Leftover files of retired models and the removed semantic search (``retired_models``).

Every folder is synthetic and lives under ``tests/tmp``, removed in ``tearDown``.
"""

import os
import shutil
import sys
import tempfile
import unittest

SOURCE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, SOURCE_DIR)

from retired_models import SEMANTIC_INDEX_ID, Leftovers

TMP_ROOT = os.path.join(os.path.dirname(__file__), "tmp")


def _write(path, size):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(b"x" * size)


def _link_dir(target, link):
    """A directory link the test user can create: a symlink, or a junction on Windows."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    try:
        import _winapi
        _winapi.CreateJunction(target, link)
        return True
    except (ImportError, OSError):
        return False


class LeftoverTests(unittest.TestCase):
    def setUp(self):
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.tmp = tempfile.mkdtemp(dir=TMP_ROOT)
        self.models = os.path.join(self.tmp, "models")
        self.voice = os.path.join(self.models, "voice-models")
        self.summary = os.path.join(self.models, "summary-models")
        self.data = os.path.join(self.tmp, "data")
        os.makedirs(self.voice)
        os.makedirs(self.summary)
        os.makedirs(self.data)
        self.leftovers = Leftovers(voice_cache=self.voice, summary_cache=self.summary,
                                   data_root=self.data, models_root=self.models)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_nothing_left_behind_lists_nothing(self):
        _write(os.path.join(self.voice, "whisper-small-q8", "whisper-small-Q8_0.gguf"), 10)
        _write(os.path.join(self.data, "library.sqlite"), 10)
        self.assertEqual(self.leftovers.find(), [])

    def test_retired_models_and_the_semantic_index_are_listed_with_sizes(self):
        _write(os.path.join(self.voice, "whisper-large-v3-turbo-q8", "model.gguf"), 300)
        _write(os.path.join(self.voice, "whisper-large-v3-turbo-q8", "manifest.json"), 20)
        _write(os.path.join(self.summary, "lfm2.5-2.6b-q4", "model.gguf"), 50)
        _write(os.path.join(self.models, "embedding-models", "embeddinggemma-2-bf16", "model.gguf"), 70)
        _write(os.path.join(self.data, "semantic.sqlite"), 40)
        _write(os.path.join(self.data, "semantic.sqlite.lock"), 0)
        _write(os.path.join(self.data, "semantic.sqlite.0a1b.tmp"), 5)
        found = {item["id"]: item["size_bytes"] for item in self.leftovers.find()}
        self.assertEqual(found, {"voice:whisper-large-v3-turbo-q8": 320, "summary:lfm2.5-2.6b-q4": 50,
                                 "embedding:embeddinggemma-2-bf16": 70, SEMANTIC_INDEX_ID: 45})

    def test_removing_a_model_deletes_only_its_folder(self):
        _write(os.path.join(self.voice, "whisper-large-v3-turbo-q8", "model.gguf"), 10)
        _write(os.path.join(self.voice, "whisper-small-q8", "model.gguf"), 10)
        _write(os.path.join(self.voice, "history", "entry.json"), 10)
        self.leftovers.remove("voice:whisper-large-v3-turbo-q8")
        self.assertEqual(sorted(os.listdir(self.voice)), ["history", "whisper-small-q8"])
        self.assertEqual(self.leftovers.find(), [])

    def test_removing_the_embedding_model_also_clears_its_empty_folder(self):
        _write(os.path.join(self.models, "embedding-models", "embeddinggemma-2-bf16", "model.gguf"), 10)
        self.leftovers.remove("embedding:embeddinggemma-2-bf16")
        self.assertFalse(os.path.exists(os.path.join(self.models, "embedding-models")))

    def test_an_embedding_folder_with_other_files_is_kept(self):
        _write(os.path.join(self.models, "embedding-models", "embeddinggemma-2-bf16", "model.gguf"), 10)
        _write(os.path.join(self.models, "embedding-models", "notes.txt"), 1)
        self.leftovers.remove("embedding:embeddinggemma-2-bf16")
        self.assertEqual(os.listdir(os.path.join(self.models, "embedding-models")), ["notes.txt"])

    def test_removing_the_semantic_index_keeps_the_word_index(self):
        for name in ("semantic.sqlite", "semantic.sqlite-wal", "semantic.sqlite.lock", "semantic.sqlite.ff.tmp",
                     "library.sqlite", "workspace.json"):
            _write(os.path.join(self.data, name), 1)
        self.leftovers.remove(SEMANTIC_INDEX_ID)
        self.assertEqual(sorted(os.listdir(self.data)), ["library.sqlite", "workspace.json"])

    def test_unknown_or_current_ids_are_refused(self):
        _write(os.path.join(self.voice, "whisper-small-q8", "model.gguf"), 10)
        for key in ("voice:whisper-small-q8", "voice:../data", "whatever"):
            with self.subTest(key=key):
                self.assertIsNone(self.leftovers.name(key))
                with self.assertRaises(ValueError):
                    self.leftovers.remove(key)
        self.assertTrue(os.path.isdir(os.path.join(self.voice, "whisper-small-q8")))

    def test_a_linked_leftover_is_neither_listed_nor_followed(self):
        outside = os.path.join(self.tmp, "outside")
        _write(os.path.join(outside, "keep.bin"), 10)
        if not _link_dir(outside, os.path.join(self.voice, "whisper-large-v3-turbo-q8")):
            self.skipTest("this account cannot create directory links")
        self.assertEqual(self.leftovers.find(), [])
        with self.assertRaises(ValueError):
            self.leftovers.remove("voice:whisper-large-v3-turbo-q8")
        self.assertTrue(os.path.isfile(os.path.join(outside, "keep.bin")))


if __name__ == "__main__":
    unittest.main()
