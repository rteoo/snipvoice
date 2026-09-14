import hashlib
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import voice_models
from voice_catalog import (
    DEFAULT_PROFILE,
    LANGUAGE_AUTO,
    LANGUAGE_EN_US,
    LANGUAGE_PT_BR,
    MODEL_CATALOG,
    PROFILE_ACCURACY,
    PROFILE_BALANCED,
    PROFILE_COMPACT,
    PROFILE_STREAMING,
    catalog_entry,
    available_languages,
    default_language_for_profile,
    format_size,
)
from voice_models import (
    VoiceModelError,
    default_voice_cache_dir,
    delete_model,
    download_model,
    model_is_installed,
)


def _tiny_entry(payload, url="https://example.test/model.gguf"):
    digest = hashlib.sha256(payload).hexdigest()
    return {
        "id": "tiny-test-model",
        "profile": "balanced",
        "filename": "tiny.gguf",
        "url": url,
        "sha256": digest,
        "size_bytes": len(payload),
        "license_id": "MIT",
        "upstream_model": "test/tiny",
    }, digest


class _FakeResponse:
    def __init__(self, data, max_read=None):
        self._data = data
        self._offset = 0
        self._max_read = max_read

    def read(self, size=-1):
        if self._offset >= len(self._data):
            return b""
        if self._max_read is not None:
            size = self._max_read if size < 0 else min(size, self._max_read)
        end = len(self._data) if size < 0 else min(len(self._data), self._offset + size)
        chunk = self._data[self._offset:end]
        self._offset = end
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class CatalogTests(unittest.TestCase):
    def test_four_profiles_and_no_f32(self):
        profiles = {entry["profile"] for entry in MODEL_CATALOG}
        self.assertEqual(
            profiles,
            {PROFILE_BALANCED, PROFILE_COMPACT, PROFILE_ACCURACY, PROFILE_STREAMING},
        )
        self.assertEqual(DEFAULT_PROFILE, PROFILE_BALANCED)
        for entry in MODEL_CATALOG:
            self.assertEqual(entry["quantization"], "Q8_0")
            self.assertTrue(entry["url"].startswith("https://"))
            self.assertEqual(len(entry["sha256"]), 64)

    def test_qwen_cannot_take_a_language_hint(self):
        self.assertEqual(catalog_entry(PROFILE_ACCURACY)["language_hint"], "unsupported")
        self.assertEqual(catalog_entry(PROFILE_COMPACT)["language_hint"], "unsupported")
        self.assertEqual(available_languages(PROFILE_ACCURACY), (LANGUAGE_AUTO,))
        self.assertEqual(available_languages(PROFILE_COMPACT), (LANGUAGE_AUTO,))
        self.assertEqual(
            available_languages(PROFILE_BALANCED),
            (LANGUAGE_AUTO, LANGUAGE_PT_BR, LANGUAGE_EN_US),
        )
        self.assertEqual(
            default_language_for_profile(PROFILE_ACCURACY, "pt-BR"),
            LANGUAGE_AUTO,
        )
        self.assertEqual(
            default_language_for_profile(PROFILE_COMPACT, "pt-BR"),
            LANGUAGE_AUTO,
        )

    def test_compact_profile_pins_qwen_0_6b_q8(self):
        entry = catalog_entry(PROFILE_COMPACT)
        self.assertEqual(entry["id"], "qwen3-asr-0.6b-q8")
        self.assertEqual(entry["filename"], "Qwen3-ASR-0.6B-Q8_0.gguf")
        self.assertEqual(entry["size_bytes"], 850423456)
        self.assertEqual(
            entry["sha256"],
            "f081b2d5e23bd669d92cc331d722a8a0681943b8e6f34b48996fd5c319b5acd8",
        )
        self.assertTrue(entry["user_selectable"])

    def test_streaming_defaults_auto_to_pt_br(self):
        self.assertEqual(
            default_language_for_profile(PROFILE_STREAMING, LANGUAGE_AUTO),
            LANGUAGE_PT_BR,
        )
        self.assertEqual(
            default_language_for_profile(PROFILE_STREAMING, "en-US"),
            "en-US",
        )

    def test_format_size_uses_decimal_units(self):
        self.assertEqual(format_size(739508576), "705 MB")
        self.assertEqual(format_size(2185030624), "2.03 GB")

    def test_notices_name_every_catalog_license(self):
        from voice_catalog import third_party_notices
        text = "\n".join(third_party_notices())
        self.assertIn("CC-BY-4.0", text)
        self.assertIn("Qwen3-ASR", text)
        self.assertIn("soxr / libsoxr / PFFFT", text)
        self.assertNotIn("OpenMDW-1.1", text)

    def test_balanced_compact_and_accuracy_are_user_selectable(self):
        from voice_catalog import selectable_catalog
        visible = selectable_catalog()
        self.assertEqual(
            [entry["profile"] for entry in visible],
            ["balanced", "compact", "accuracy"],
        )

    def test_selectable_copy_leads_with_model_names_and_marks_default(self):
        self.assertTrue(
            catalog_entry(PROFILE_BALANCED)["purpose"].startswith(
                "Parakeet TDT 0.6B v3 (padrão):"
            )
        )
        self.assertTrue(
            catalog_entry(PROFILE_COMPACT)["purpose"].startswith(
                "Qwen3-ASR 0.6B:"
            )
        )
        self.assertTrue(
            catalog_entry(PROFILE_ACCURACY)["purpose"].startswith(
                "Qwen3-ASR 1.7B (opcional):"
            )
        )

    def test_accuracy_copy_identifies_optional_resource_cost(self):
        purpose = catalog_entry(PROFILE_ACCURACY)["purpose"].lower()
        self.assertIn("opcional", purpose)
        self.assertIn("memória", purpose)
        self.assertIn("automaticamente", purpose)


class CacheLocationTests(unittest.TestCase):
    def test_env_override_wins(self):
        previous = os.environ.get("SNIPVOICE_VOICE_CACHE")
        os.environ["SNIPVOICE_VOICE_CACHE"] = os.path.join(tempfile.gettempdir(), "vx")
        try:
            self.assertTrue(
                default_voice_cache_dir().endswith("vx")
                or default_voice_cache_dir().endswith("vx".replace("/", os.sep))
            )
        finally:
            if previous is None:
                os.environ.pop("SNIPVOICE_VOICE_CACHE", None)
            else:
                os.environ["SNIPVOICE_VOICE_CACHE"] = previous

    def test_default_is_not_the_snippet_data_dir(self):
        os.environ.pop("SNIPVOICE_VOICE_CACHE", None)
        cache = default_voice_cache_dir(system="windows")
        self.assertNotIn(".snipvoice", cache)
        self.assertIn("voice-models", cache)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.payload = b"gguf-test-bytes-0123456789"
        self.entry, self.digest = _tiny_entry(self.payload)

    def _opener(self, payload=None):
        body = self.payload if payload is None else payload

        def open_url(request, timeout=None):
            self.assertTrue(request.full_url.startswith("https://"))
            return _FakeResponse(body)

        return open_url

    def test_download_verifies_digest_and_writes_manifest(self):
        path = download_model(self.entry, self.tmp, opener=self._opener())
        self.assertTrue(os.path.isfile(path))
        self.assertTrue(model_is_installed(self.entry, self.tmp))
        with open(path, "rb") as handle:
            self.assertEqual(hashlib.sha256(handle.read()).hexdigest(), self.digest)

    def test_same_size_corruption_with_changed_stat_is_rehashed_and_rejected(self):
        path = download_model(self.entry, self.tmp, opener=self._opener())
        verified_stat = os.stat(path)
        with open(path, "wb") as handle:
            handle.write(b"x" * len(self.payload))
        os.utime(
            path,
            ns=(verified_stat.st_atime_ns, verified_stat.st_mtime_ns + 2_000_000_000),
        )

        self.assertFalse(model_is_installed(self.entry, self.tmp))

    def test_manifest_identity_mismatch_is_rehashed_and_repaired(self):
        download_model(self.entry, self.tmp, opener=self._opener())
        manifest_file = os.path.join(
            self.tmp, self.entry["id"], "manifest.json"
        )
        with open(manifest_file, "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
        manifest["profile"] = "accuracy"
        with open(manifest_file, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle)

        self.assertTrue(model_is_installed(self.entry, self.tmp))
        with open(manifest_file, "r", encoding="utf-8") as handle:
            repaired = json.load(handle)
        self.assertEqual(repaired["profile"], self.entry["profile"])

    def test_old_manifest_is_rehashed_once_then_upgraded_for_stat_fast_path(self):
        download_model(self.entry, self.tmp, opener=self._opener())
        manifest_file = os.path.join(
            self.tmp, self.entry["id"], "manifest.json"
        )
        with open(manifest_file, "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
        manifest.pop("verified_stat", None)
        with open(manifest_file, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle)

        with mock.patch("voice_models._hash_file", wraps=voice_models._hash_file) as hashed:
            self.assertTrue(model_is_installed(self.entry, self.tmp))
            self.assertTrue(model_is_installed(self.entry, self.tmp))

        self.assertEqual(hashed.call_count, 1)
        with open(manifest_file, "r", encoding="utf-8") as handle:
            upgraded = json.load(handle)
        self.assertIn("verified_stat", upgraded)

    def test_second_download_is_a_no_op(self):
        calls = []

        def open_url(request, timeout=None):
            calls.append(1)
            return _FakeResponse(self.payload)

        download_model(self.entry, self.tmp, opener=open_url)
        download_model(self.entry, self.tmp, opener=open_url)
        self.assertEqual(len(calls), 1)

    def test_wrong_digest_leaves_no_install(self):
        with self.assertRaises(VoiceModelError):
            download_model(self.entry, self.tmp, opener=self._opener(b"tampered"))
        self.assertFalse(model_is_installed(self.entry, self.tmp))

    def test_http_url_is_rejected(self):
        entry, _ = _tiny_entry(self.payload, url="http://example.test/model.gguf")
        with self.assertRaises(VoiceModelError):
            download_model(entry, self.tmp, opener=self._opener())

    def test_cancel_leaves_no_install(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(VoiceModelError):
            download_model(
                self.entry,
                self.tmp,
                cancel_event=cancel,
                opener=self._opener(),
            )
        self.assertFalse(model_is_installed(self.entry, self.tmp))

    def test_delete_removes_only_the_catalog_directory(self):
        download_model(self.entry, self.tmp, opener=self._opener())
        sibling = os.path.join(self.tmp, "keep-me")
        os.makedirs(sibling)
        delete_model(self.entry, self.tmp)
        self.assertFalse(model_is_installed(self.entry, self.tmp))
        self.assertTrue(os.path.isdir(sibling))

    def test_path_traversal_id_is_rejected(self):
        entry, _ = _tiny_entry(self.payload)
        entry["id"] = "../escape"
        with self.assertRaises(VoiceModelError):
            download_model(entry, self.tmp, opener=self._opener())


if __name__ == "__main__":
    unittest.main()
