import hashlib
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import voice_models as models


class LocalModelImportTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent / "tmp"
        root.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.entry = {"id": "fixture", "filename": "fixture.gguf", "profile": "balanced",
                      "size_bytes": 4, "sha256": hashlib.sha256(b"test").hexdigest(),
                      "license_id": "MIT", "upstream_model": "fixture"}
        self.source = self.root / "input.gguf"
        self.source.write_bytes(b"test")

    def test_verified_bytes_install_without_network(self):
        with mock.patch.object(models, "resolve_entry", return_value=self.entry), \
                mock.patch.object(models, "_opener") as network:
            path = models.import_local_model("balanced", self.source, self.root / "cache")
            self.assertEqual(Path(path).read_bytes(), b"test")
            self.assertTrue(models.model_is_installed(self.entry, self.root / "cache"))
            network.assert_not_called()

    def test_invalid_hash_preserves_existing_model(self):
        destination = Path(models.model_path(self.root / "cache", self.entry))
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"test")
        self.source.write_bytes(b"evil")
        with mock.patch.object(models, "resolve_entry", return_value=self.entry), \
                self.assertRaises(models.VoiceModelError):
            models.import_local_model("balanced", self.source, self.root / "cache")
        self.assertEqual(destination.read_bytes(), b"test")

    def test_cancel_preserves_previous_bytes(self):
        cancel = threading.Event()
        cancel.set()
        with mock.patch.object(models, "resolve_entry", return_value=self.entry), \
                self.assertRaises(models.VoiceModelError):
            models.import_local_model("balanced", self.source, self.root / "cache", cancel_event=cancel)
