import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from voice_provider import LocalVoiceProvider, VoiceProvider
from voice_runtime import FakeAsrBackend, VoiceRuntimeError


class VoiceProviderTests(unittest.TestCase):
    def test_base_provider_is_unavailable(self):
        provider = VoiceProvider()
        self.assertFalse(provider.available())
        with self.assertRaises(VoiceRuntimeError):
            provider.prepare("balanced", "auto")

    def test_local_provider_owns_download_load_and_transcription(self):
        backend = FakeAsrBackend(transcript="pronto")
        download = mock.Mock(return_value="model.gguf")
        provider = LocalVoiceProvider(
            tempfile.mkdtemp(),
            backend=backend,
            download=download,
            is_installed=lambda entry, directory: False,
        )

        provider.prepare("balanced", "pt-BR")

        download.assert_called_once()
        self.assertTrue(provider.is_ready())
        self.assertEqual(provider.transcribe([0.1]), "pronto")
        self.assertEqual(backend.profile, "balanced")
        self.assertEqual(backend.language, "pt-BR")

    def test_local_provider_uses_an_installed_model_without_downloading(self):
        backend = FakeAsrBackend()
        download = mock.Mock()
        provider = LocalVoiceProvider(
            tempfile.mkdtemp(),
            backend=backend,
            download=download,
            is_installed=lambda entry, directory: True,
            installed_path=lambda entry, directory: "installed.gguf",
        )

        provider.prepare("balanced", "auto")

        download.assert_not_called()
        self.assertEqual(backend.loaded_path, "installed.gguf")

    def test_download_profile_does_not_load_the_backend(self):
        backend = FakeAsrBackend()
        download = mock.Mock(return_value="downloaded.gguf")
        provider = LocalVoiceProvider(
            tempfile.mkdtemp(),
            backend=backend,
            download=download,
            is_installed=lambda entry, directory: False,
        )

        path = provider.download_profile("compact")

        self.assertEqual(path, "downloaded.gguf")
        download.assert_called_once()
        self.assertFalse(provider.is_ready())


if __name__ == "__main__":
    unittest.main()
