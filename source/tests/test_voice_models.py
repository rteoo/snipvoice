import hashlib
import os
import sys
import tempfile
import threading
import unittest
import urllib.error

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from voice_models import (
    ENV_VOICE_CACHE,
    VoiceModelError,
    default_voice_cache_dir,
    download_model,
    manifest_path,
    model_path,
)


class VoiceCachePathTests(unittest.TestCase):
    def test_env_override_wins(self):
        previous = os.environ.get(ENV_VOICE_CACHE)
        os.environ[ENV_VOICE_CACHE] = os.path.join(tempfile.gettempdir(), "voice-cache-x")
        try:
            self.assertTrue(default_voice_cache_dir().endswith("voice-cache-x"))
        finally:
            if previous is None:
                os.environ.pop(ENV_VOICE_CACHE, None)
            else:
                os.environ[ENV_VOICE_CACHE] = previous

    def test_windows_uses_localappdata_not_roaming(self):
        path = default_voice_cache_dir(system="windows")
        self.assertIn("Snipvoice", path)
        self.assertIn("voice-models", path)
        self.assertNotIn("Roaming", path)

    def test_macos_and_linux_are_cache_dirs(self):
        mac = default_voice_cache_dir(system="darwin")
        linux = default_voice_cache_dir(system="linux")
        self.assertIn("Library", mac)
        self.assertIn("Caches", mac)
        self.assertIn(".cache", linux)


class VoiceDownloadPolicyTests(unittest.TestCase):
    def test_file_and_http_urls_are_rejected(self):
        for url in ("file:///tmp/evil.gguf", "http://example.com/model.gguf"):
            entry = {
                "id": "fixture-model",
                "filename": "fixture.gguf",
                "url": url,
                "sha256": "0" * 64,
                "size_bytes": 1,
                "license_id": "MIT",
                "upstream_model": "fixture",
            }
            with self.assertRaises(VoiceModelError):
                download_model(entry, tempfile.mkdtemp())


class _Response:
    def __init__(self, payload, status=200, headers=None, fail_after=None,
                 final_url="https://models.example.test/model.gguf"):
        self.payload = payload
        self.status = status
        self.headers = headers or {}
        self.fail_after = fail_after
        self.final_url = final_url
        self.position = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, size):
        if self.fail_after is not None and self.position >= self.fail_after:
            raise urllib.error.URLError("dropped transfer")
        chunk = self.payload[self.position:self.position + size]
        self.position += len(chunk)
        return chunk

    def geturl(self):
        return self.final_url


class _Opener:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        return next(self.responses)


class ResumableVoiceDownloadTests(unittest.TestCase):
    def setUp(self):
        self.cache = tempfile.TemporaryDirectory()
        self.addCleanup(self.cache.cleanup)
        self.payload = b"verified model payload"
        self.entry = {
            "id": "fixture-model",
            "profile": "balanced",
            "filename": "fixture.gguf",
            "url": "https://models.example.test/model.gguf",
            "sha256": hashlib.sha256(self.payload).hexdigest(),
            "size_bytes": len(self.payload),
            "license_id": "MIT",
            "upstream_model": "fixture",
        }

    def _partial_path(self):
        return model_path(self.cache.name, self.entry) + ".partial"

    def _write_partial(self, data):
        path = self._partial_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)

    def test_resumes_catalog_owned_partial_with_verified_range_and_progress(self):
        prefix = self.payload[:8]
        self._write_partial(prefix)
        opener = _Opener([_Response(
            self.payload[8:],
            status=206,
            headers={"Content-Range": f"bytes 8-{len(self.payload) - 1}/{len(self.payload)}"},
        )])
        progress = []

        installed = download_model(
            self.entry, self.cache.name,
            progress=lambda done, total: progress.append((done, total)),
            opener=opener,
        )

        with open(installed, "rb") as handle:
            self.assertEqual(handle.read(), self.payload)
        self.assertFalse(os.path.exists(self._partial_path()))
        self.assertEqual(opener.requests[0].get_header("Range"), "bytes=8-")
        self.assertEqual(progress[0], (8, len(self.payload)))
        self.assertEqual(progress[-1], (len(self.payload), len(self.payload)))
        self.assertTrue(os.path.isfile(manifest_path(self.cache.name, self.entry)))

    def test_ignored_range_restarts_from_full_response(self):
        self._write_partial(self.payload[:5])
        opener = _Opener([_Response(self.payload, status=200)])

        installed = download_model(self.entry, self.cache.name, opener=opener)

        with open(installed, "rb") as handle:
            self.assertEqual(handle.read(), self.payload)
        self.assertEqual(opener.requests[0].get_header("Range"), "bytes=5-")
        self.assertFalse(os.path.exists(self._partial_path()))

    def test_bad_range_offset_discards_partial_then_restarts_without_range(self):
        self._write_partial(self.payload[:5])
        opener = _Opener([
            _Response(
                self.payload[5:],
                status=206,
                headers={"Content-Range": f"bytes 0-{len(self.payload) - 6}/{len(self.payload)}"},
            ),
            _Response(self.payload, status=200),
        ])

        installed = download_model(self.entry, self.cache.name, opener=opener)

        with open(installed, "rb") as handle:
            self.assertEqual(handle.read(), self.payload)
        self.assertEqual(opener.requests[0].get_header("Range"), "bytes=5-")
        self.assertIsNone(opener.requests[1].get_header("Range"))

    def test_unsatisfiable_range_discards_stale_partial_and_restarts(self):
        self._write_partial(self.payload[:5])
        range_error = urllib.error.HTTPError(
            self.entry["url"], 416, "Range Not Satisfiable", {}, None
        )
        opener = _Opener([range_error, _Response(self.payload, status=200)])

        installed = download_model(self.entry, self.cache.name, opener=opener)

        with open(installed, "rb") as handle:
            self.assertEqual(handle.read(), self.payload)
        self.assertEqual(opener.requests[0].get_header("Range"), "bytes=5-")
        self.assertIsNone(opener.requests[1].get_header("Range"))

    def test_truncated_range_download_is_rejected_and_partial_is_deleted(self):
        self._write_partial(self.payload[:5])
        opener = _Opener([_Response(
            self.payload[5:-1],
            status=206,
            headers={"Content-Range": f"bytes 5-{len(self.payload) - 1}/{len(self.payload)}"},
        )])

        with self.assertRaisesRegex(VoiceModelError, "Tamanho"):
            download_model(self.entry, self.cache.name, opener=opener)

        self.assertFalse(os.path.exists(self._partial_path()))
        self.assertFalse(os.path.exists(model_path(self.cache.name, self.entry)))

    def test_oversized_download_is_rejected_and_partial_is_deleted(self):
        opener = _Opener([_Response(self.payload + b"!")])

        with self.assertRaisesRegex(VoiceModelError, "maior"):
            download_model(self.entry, self.cache.name, opener=opener)

        self.assertFalse(os.path.exists(self._partial_path()))
        self.assertFalse(os.path.exists(model_path(self.cache.name, self.entry)))

    def test_cancelled_download_keeps_partial_for_a_later_retry(self):
        prefix = self.payload[:5]
        self._write_partial(prefix)
        cancelled = threading.Event()

        def progress(done, total):
            if done > len(prefix):
                cancelled.set()

        opener = _Opener([_Response(
            self.payload[5:],
            status=206,
            headers={"Content-Range": f"bytes 5-{len(self.payload) - 1}/{len(self.payload)}"},
        )])
        with self.assertRaisesRegex(VoiceModelError, "cancelado"):
            download_model(
                self.entry, self.cache.name, progress=progress,
                cancel_event=cancelled, opener=opener,
            )

        with open(self._partial_path(), "rb") as handle:
            self.assertEqual(handle.read(), self.payload)
        self.assertFalse(os.path.exists(model_path(self.cache.name, self.entry)))

    def test_transport_error_keeps_partial_for_a_later_retry(self):
        prefix = self.payload[:5]
        self._write_partial(prefix)
        opener = _Opener([_Response(
            self.payload[5:],
            status=206,
            headers={"Content-Range": f"bytes 5-{len(self.payload) - 1}/{len(self.payload)}"},
            fail_after=0,
        )])

        with self.assertRaisesRegex(VoiceModelError, "Falha ao baixar"):
            download_model(self.entry, self.cache.name, opener=opener)

        with open(self._partial_path(), "rb") as handle:
            self.assertEqual(handle.read(), prefix)

    def test_integrity_failure_deletes_partial_without_clobbering_existing_file(self):
        destination = model_path(self.cache.name, self.entry)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        with open(destination, "wb") as handle:
            handle.write(b"prior-install")
        opener = _Opener([_Response(b"x" * len(self.payload))])

        with self.assertRaisesRegex(VoiceModelError, "SHA-256"):
            download_model(self.entry, self.cache.name, opener=opener)

        with open(destination, "rb") as handle:
            self.assertEqual(handle.read(), b"prior-install")
        self.assertFalse(os.path.exists(self._partial_path()))

    def test_corrupt_retained_bytes_are_rehashed_and_rejected(self):
        self._write_partial(b"x" * 5)
        opener = _Opener([_Response(
            self.payload[5:],
            status=206,
            headers={"Content-Range": f"bytes 5-{len(self.payload) - 1}/{len(self.payload)}"},
        )])

        with self.assertRaisesRegex(VoiceModelError, "SHA-256"):
            download_model(self.entry, self.cache.name, opener=opener)

        self.assertFalse(os.path.exists(self._partial_path()))

    def test_insecure_redirect_target_is_rejected_before_writing(self):
        opener = _Opener([_Response(self.payload, final_url="http://evil.example/model.gguf")])

        with self.assertRaisesRegex(VoiceModelError, "Redirecionamento inseguro"):
            download_model(self.entry, self.cache.name, opener=opener)

        self.assertFalse(os.path.exists(self._partial_path()))


if __name__ == "__main__":
    unittest.main()
