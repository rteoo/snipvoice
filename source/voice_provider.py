"""Voice-provider boundary for local and future transcription services.

The controller owns the user session. A provider owns everything required to
turn PCM into text: readiness, model/runtime lifecycle, cancellation, and
optional streaming. Keeping that boundary above a native backend prevents a
future provider from leaking download or transport decisions into the hotkey
state machine.
"""

from voice_catalog import catalog_entry
from voice_models import (
    VoiceModelError,
    delete_model,
    download_model,
    installed_model_path,
    model_is_installed,
)
from voice_runtime import VoiceRuntimeError, create_backend


class VoiceProvider:
    """Complete transcription provider contract used by ``VoiceController``."""

    provider_id = "unavailable"

    def available(self):
        return False

    def is_ready(self):
        return False

    def prepare(self, profile, language, progress=None, cancel_event=None):
        raise VoiceRuntimeError("Provedor de voz indisponível.")

    def unload(self):
        return None

    def cancel(self):
        return None

    def transcribe(self, pcm, cancel_event=None):
        raise VoiceRuntimeError("Provedor de voz indisponível.")

    def supports_stream(self):
        return False

    def start_stream(self):
        raise VoiceRuntimeError("Este provedor não faz transcrição contínua.")

    def feed(self, pcm_chunk):
        return ""

    def finalize_stream(self):
        return ""

    def profile_installed(self, profile):
        return False

    def download_profile(self, profile, progress=None, cancel_event=None):
        raise VoiceModelError("Provedor de voz indisponível.")

    def delete_profile(self, profile):
        return None


class LocalVoiceProvider(VoiceProvider):
    """Local model provider backed by the optional transcribe.cpp runtime."""

    provider_id = "local"

    def __init__(
        self,
        cache_dir,
        backend=None,
        download=None,
        is_installed=None,
        installed_path=None,
        delete=None,
    ):
        self.cache_dir = cache_dir
        self.backend = backend if backend is not None else create_backend()
        self._download = download or download_model
        self._is_installed = is_installed or model_is_installed
        self._installed_path = installed_path or installed_model_path
        self._delete = delete or delete_model

    def available(self):
        return bool(self.backend.available() or self.backend.is_loaded())

    def is_ready(self):
        return bool(self.backend.is_loaded())

    def profile_installed(self, profile):
        entry = catalog_entry(profile)
        return bool(entry and self._is_installed(entry, self.cache_dir))

    def download_profile(self, profile, progress=None, cancel_event=None):
        entry = catalog_entry(profile)
        if entry is None:
            raise VoiceModelError("Perfil de voz desconhecido.")
        if self._is_installed(entry, self.cache_dir):
            return self._installed_path(entry, self.cache_dir)

        def report(done, total):
            if progress is not None:
                progress(done, total or entry["size_bytes"])

        return self._download(
            entry,
            self.cache_dir,
            progress=report,
            cancel_event=cancel_event,
        )

    def prepare(self, profile, language, progress=None, cancel_event=None):
        if not self.available():
            raise VoiceRuntimeError(
                "O runtime transcribe.cpp não está instalado. "
                "A voz fica desligada até o pacote nativo estar disponível."
            )
        path = self.download_profile(
            profile,
            progress=progress,
            cancel_event=cancel_event,
        )
        if cancel_event is not None and cancel_event.is_set():
            return
        self.backend.load(path, profile, language)

    def unload(self):
        return self.backend.unload()

    def cancel(self):
        return self.backend.cancel()

    def transcribe(self, pcm, cancel_event=None):
        return self.backend.transcribe(pcm, cancel_event=cancel_event)

    def supports_stream(self):
        return self.backend.supports_stream()

    def start_stream(self):
        return self.backend.start_stream()

    def feed(self, pcm_chunk):
        return self.backend.feed(pcm_chunk)

    def finalize_stream(self):
        return self.backend.finalize_stream()

    def delete_profile(self, profile):
        entry = catalog_entry(profile)
        if entry is not None:
            self._delete(entry, self.cache_dir)


def create_provider(cache_dir, backend=None, download=None, **kwargs):
    """Return the default local provider without selecting a cloud service."""
    return LocalVoiceProvider(
        cache_dir,
        backend=backend,
        download=download,
        **kwargs,
    )
