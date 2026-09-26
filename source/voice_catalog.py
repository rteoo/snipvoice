"""SHA256-pinned catalog of optional on-demand voice models.

The catalog is the trust anchor for downloads: a profile that is not listed
here cannot be fetched, and a file whose digest does not match is rejected.
F32 Parakeet is a benchmark fixture, not a user-facing profile.

Cloud post-processing is intentionally absent. Capture stays local.
"""

from i18n import N_

PROFILE_BALANCED = "balanced"
PROFILE_COMPACT = "compact"
PROFILE_ACCURACY = "accuracy"
PROFILE_STREAMING = "streaming"
PROFILE_WHISPER_SMALL = "whisper-small"
PROFILE_WHISPER_TURBO = "whisper-turbo"
PROFILE_WHISPER_LARGE = "whisper-large-v3"

PROFILES = (
    PROFILE_BALANCED,
    PROFILE_COMPACT,
    PROFILE_ACCURACY,
    PROFILE_STREAMING,
    PROFILE_WHISPER_SMALL,
    PROFILE_WHISPER_TURBO,
    PROFILE_WHISPER_LARGE,
)

LANGUAGE_AUTO = "auto"
LANGUAGE_PT_BR = "pt-BR"
LANGUAGE_EN_US = "en-US"
LANGUAGES = (LANGUAGE_AUTO, LANGUAGE_PT_BR, LANGUAGE_EN_US)

RUNTIME_TRANSCRIBE_CPP = "transcribe.cpp"

# Hugging Face LFS SHA-256 of the exact GGUF files (verified 2026-08-13).
_PARAKEET_Q8 = {
    "id": "parakeet-tdt-0.6b-v3-q8",
    "profile": PROFILE_BALANCED,
    "filename": "parakeet-tdt-0.6b-v3-Q8_0.gguf",
    "url": (
        "https://huggingface.co/handy-computer/parakeet-tdt-0.6b-v3-gguf/"
        "resolve/main/parakeet-tdt-0.6b-v3-Q8_0.gguf"
    ),
    "sha256": "5859f77944efcd8eafa23a6350731960b2b55b2203df51f319665c807d802cc7",
    "size_bytes": 739508576,
    "upstream_model": "nvidia/parakeet-tdt-0.6b-v3",
    "upstream_commit": "6d590f77001d318fb17a0b5bf7ee329a91b52598",
    "quant_source": "handy-computer/parakeet-tdt-0.6b-v3-gguf",
    "runtime": RUNTIME_TRANSCRIBE_CPP,
    "quantization": "Q8_0",
    "format": "gguf",
    "streaming": False,
    "language_hint": "optional",
    "min_memory_bytes": 1500 * 1024 * 1024,
    "recommended_memory_bytes": 2 * 1024 * 1024 * 1024,
    "license_id": "CC-BY-4.0",
    "license_url": "https://creativecommons.org/licenses/by/4.0/",
    "attribution": (
        "Parakeet TDT 0.6B v3 by NVIDIA, quantized to Q8_0 by handy-computer "
        "for transcribe.cpp."
    ),
    "source_url": "https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3",
    "purpose": N_("Parakeet TDT 0.6B v3 (padrão): ditado após soltar o atalho."),
    "user_selectable": True,
}

_QWEN_06_Q8 = {
    "id": "qwen3-asr-0.6b-q8",
    "profile": PROFILE_COMPACT,
    "filename": "Qwen3-ASR-0.6B-Q8_0.gguf",
    "url": (
        "https://huggingface.co/handy-computer/Qwen3-ASR-0.6B-gguf/resolve/"
        "e4e16599b900eb0cb36e524514756bb92eb092b7/"
        "Qwen3-ASR-0.6B-Q8_0.gguf"
    ),
    "sha256": "f081b2d5e23bd669d92cc331d722a8a0681943b8e6f34b48996fd5c319b5acd8",
    "size_bytes": 850423456,
    "upstream_model": "Qwen/Qwen3-ASR-0.6B",
    "upstream_commit": "5eb144179a02acc5e5ba31e748d22b0cf3e303b0",
    "quant_source": "handy-computer/Qwen3-ASR-0.6B-gguf",
    "runtime": RUNTIME_TRANSCRIBE_CPP,
    "quantization": "Q8_0",
    "format": "gguf",
    "streaming": False,
    "language_hint": "unsupported",
    "min_memory_bytes": 3 * 1024 * 1024 * 1024,
    "recommended_memory_bytes": 4 * 1024 * 1024 * 1024,
    "license_id": "Apache-2.0",
    "license_url": "https://www.apache.org/licenses/LICENSE-2.0",
    "attribution": (
        "Qwen3-ASR-0.6B by Alibaba, quantized to Q8_0 by handy-computer "
        "for transcribe.cpp. Language hints are not supported; decoding is "
        "auto-detect only."
    ),
    "source_url": "https://huggingface.co/Qwen/Qwen3-ASR-0.6B",
    "purpose": (
        N_("Qwen3-ASR 0.6B: menor e mais rápido que o Qwen3-ASR 1.7B; usa "
           "detecção automática de idioma.")
    ),
    "user_selectable": True,
}

_QWEN_Q8 = {
    "id": "qwen3-asr-1.7b-q8",
    "profile": PROFILE_ACCURACY,
    "filename": "Qwen3-ASR-1.7B-Q8_0.gguf",
    "url": (
        "https://huggingface.co/handy-computer/Qwen3-ASR-1.7B-gguf/"
        "resolve/main/Qwen3-ASR-1.7B-Q8_0.gguf"
    ),
    "sha256": "9a0d81792dfea2d5f278b8a63deb3ea6e02139ce42c2301f32ea19c4f77526b7",
    "size_bytes": 2185030624,
    "upstream_model": "Qwen/Qwen3-ASR-1.7B",
    "upstream_commit": "7278e1e70fe206f11671096ffdd38061171dd6e5",
    "quant_source": "handy-computer/Qwen3-ASR-1.7B-gguf",
    "runtime": RUNTIME_TRANSCRIBE_CPP,
    "quantization": "Q8_0",
    "format": "gguf",
    "streaming": False,
    "language_hint": "unsupported",
    "min_memory_bytes": 6 * 1024 * 1024 * 1024,
    "recommended_memory_bytes": 8 * 1024 * 1024 * 1024,
    "license_id": "Apache-2.0",
    "license_url": "https://www.apache.org/licenses/LICENSE-2.0",
    "attribution": (
        "Qwen3-ASR-1.7B by Alibaba, quantized to Q8_0 by handy-computer "
        "for transcribe.cpp. Language hints are not supported; decoding is "
        "auto-detect only."
    ),
    "source_url": "https://huggingface.co/Qwen/Qwen3-ASR-1.7B",
    "purpose": (
        N_("Qwen3-ASR 1.7B (opcional): modelo maior, mais lento e com maior uso "
           "de memória; nunca é selecionado automaticamente.")
    ),
    "user_selectable": True,
}

_NEMOTRON_Q8 = {
    "id": "nemotron-3.5-asr-streaming-0.6b-q8",
    "profile": PROFILE_STREAMING,
    "filename": "nemotron-3.5-asr-streaming-0.6b-Q8_0.gguf",
    "url": (
        "https://huggingface.co/handy-computer/nemotron-3.5-asr-streaming-0.6b-gguf/"
        "resolve/main/nemotron-3.5-asr-streaming-0.6b-Q8_0.gguf"
    ),
    "sha256": "b94545b313b3223fda7b2857a52681da813935c2127643d1e9ff0c23d988089c",
    "size_bytes": 751094240,
    "upstream_model": "nvidia/nemotron-3.5-asr-streaming-0.6b",
    "upstream_commit": "24b151a851dd15909e1fc611b11bb2da52b9fc81",
    "quant_source": "handy-computer/nemotron-3.5-asr-streaming-0.6b-gguf",
    "runtime": RUNTIME_TRANSCRIBE_CPP,
    "quantization": "Q8_0",
    "format": "gguf",
    "streaming": True,
    "language_hint": "required",
    "min_memory_bytes": 1500 * 1024 * 1024,
    "recommended_memory_bytes": 2 * 1024 * 1024 * 1024,
    "license_id": "OpenMDW-1.1",
    "license_url": "https://openmdw.ai/license/1-1/",
    "attribution": (
        "Nemotron 3.5 ASR Streaming 0.6B by NVIDIA, quantized to Q8_0 by "
        "handy-computer for transcribe.cpp. Redistribution of the quantized "
        "weights is subject to OpenMDW-1.1."
    ),
    "source_url": "https://huggingface.co/nvidia/nemotron-3.5-asr-streaming-0.6b",
    "purpose": N_("Transcrição ao vivo (opcional): parciais só na interface."),
    "user_selectable": False,
}

# Whisper pins use immutable repository revisions (verified 2026-09-23).
_WHISPER_SMALL_Q8 = {
    "id": "whisper-small-q8",
    "profile": PROFILE_WHISPER_SMALL,
    "filename": "whisper-small-Q8_0.gguf",
    "url": (
        "https://huggingface.co/handy-computer/whisper-small-gguf/resolve/"
        "a2073177cb69bd74b9ca9460b852d17fbfd5d68c/"
        "whisper-small-Q8_0.gguf"
    ),
    "sha256": "9b9c8811bbcc82a7766f0fb0925614bdacb0923b2cc630daeac17108b655b860",
    "size_bytes": 269751136,
    "upstream_model": "openai/whisper-small",
    "upstream_commit": "973afd24965f72e36ca33b3055d56a652f456b4d",
    "quant_source": "handy-computer/whisper-small-gguf",
    "runtime": RUNTIME_TRANSCRIBE_CPP,
    "quantization": "Q8_0",
    "format": "gguf",
    "streaming": False,
    "language_hint": "optional",
    "min_memory_bytes": 1024 * 1024 * 1024,
    "recommended_memory_bytes": 1500 * 1024 * 1024,
    "license_id": "Apache-2.0",
    "license_url": "https://www.apache.org/licenses/LICENSE-2.0",
    "attribution": (
        "Whisper small by OpenAI, quantized to Q8_0 by handy-computer "
        "for transcribe.cpp."
    ),
    "source_url": "https://huggingface.co/openai/whisper-small",
    "purpose": (
        N_("Whisper Small: download menor e pouca memória; menos preciso que o "
           "Parakeet em português.")
    ),
    "user_selectable": True,
}

_WHISPER_TURBO_Q8 = {
    "id": "whisper-large-v3-turbo-q8",
    "profile": PROFILE_WHISPER_TURBO,
    "filename": "whisper-large-v3-turbo-Q8_0.gguf",
    "url": (
        "https://huggingface.co/handy-computer/whisper-large-v3-turbo-gguf/"
        "resolve/ceea6c8a94a21ab85be244d311e874a39344dbf5/"
        "whisper-large-v3-turbo-Q8_0.gguf"
    ),
    "sha256": "b2e30cc286bc9f3aba4db9099fc7403543497c05ce7100d0d83091ddfd25a183",
    "size_bytes": 886381760,
    "upstream_model": "openai/whisper-large-v3-turbo",
    "upstream_commit": "41f01f3fe87f28c78e2fbf8b568835947dd65ed9",
    "quant_source": "handy-computer/whisper-large-v3-turbo-gguf",
    "runtime": RUNTIME_TRANSCRIBE_CPP,
    "quantization": "Q8_0",
    "format": "gguf",
    "streaming": False,
    "language_hint": "optional",
    "min_memory_bytes": 2 * 1024 * 1024 * 1024,
    "recommended_memory_bytes": 3 * 1024 * 1024 * 1024,
    "license_id": "MIT",
    "license_url": "https://huggingface.co/openai/whisper-large-v3-turbo",
    "attribution": (
        "Whisper large-v3-turbo by OpenAI, quantized to Q8_0 by "
        "handy-computer for transcribe.cpp."
    ),
    "source_url": "https://huggingface.co/openai/whisper-large-v3-turbo",
    "purpose": (
        N_("Whisper Large v3 Turbo (opcional): multilíngue e preciso, porém bem "
           "mais lento na CPU; nunca é selecionado automaticamente.")
    ),
    "user_selectable": True,
}

_WHISPER_LARGE_Q8 = {
    "id": "whisper-large-v3-q8",
    "profile": PROFILE_WHISPER_LARGE,
    "filename": "whisper-large-v3-Q8_0.gguf",
    "url": (
        "https://huggingface.co/handy-computer/whisper-large-v3-gguf/"
        "resolve/b33a05f1459f33b0c876f014a57d51618b77d754/"
        "whisper-large-v3-Q8_0.gguf"
    ),
    "sha256": "2fa1a5f179f8a511a53e2108db270aa4af3ce08cd976af4180e2854666bb4ba3",
    "size_bytes": 1668741440,
    "upstream_model": "openai/whisper-large-v3",
    "upstream_commit": "06f233fe06e710322aca913c1bc4249a0d71fce1",
    "quant_source": "handy-computer/whisper-large-v3-gguf",
    "runtime": RUNTIME_TRANSCRIBE_CPP,
    "quantization": "Q8_0",
    "format": "gguf",
    "streaming": False,
    "language_hint": "optional",
    "min_memory_bytes": 3 * 1024 * 1024 * 1024,
    "recommended_memory_bytes": 4 * 1024 * 1024 * 1024,
    "license_id": "Apache-2.0",
    "license_url": "https://www.apache.org/licenses/LICENSE-2.0",
    "attribution": (
        "Whisper large-v3 by OpenAI, quantized to Q8_0 by handy-computer "
        "for transcribe.cpp."
    ),
    "source_url": "https://huggingface.co/openai/whisper-large-v3",
    "purpose": (
        N_("Whisper Large v3 (opcional): o mais preciso em português, porém "
           "mais lento que o tempo real na CPU e com maior uso de memória; nunca "
           "é selecionado automaticamente.")
    ),
    "user_selectable": True,
}

MODEL_CATALOG = (
    _PARAKEET_Q8,
    _QWEN_06_Q8,
    _QWEN_Q8,
    _NEMOTRON_Q8,
    _WHISPER_SMALL_Q8,
    _WHISPER_TURBO_Q8,
    _WHISPER_LARGE_Q8,
)

DEFAULT_PROFILE = PROFILE_BALANCED


def catalog_entry(profile):
    """Return the catalog dict for ``profile``, or None if unknown."""
    for entry in MODEL_CATALOG:
        if entry["profile"] == profile:
            return entry
    return None


def catalog_entry_by_id(model_id):
    """Return the catalog dict for a stable model id, or None."""
    for entry in MODEL_CATALOG:
        if entry["id"] == model_id:
            return entry
    return None


def is_known_profile(profile):
    return profile in PROFILES


def is_selectable_profile(profile):
    """True when the profile may appear in the settings UI and be downloaded."""
    entry = catalog_entry(profile)
    return bool(entry and entry.get("user_selectable"))


def selectable_catalog():
    """Profiles that have passed (or do not need) adoption gates."""
    return tuple(entry for entry in MODEL_CATALOG if entry.get("user_selectable"))


def is_known_language(language):
    return language in LANGUAGES


def available_languages(profile):
    """Return language choices the selected model can actually honor.

    Qwen's transcribe.cpp adapter has no language-hint parameter. Keeping the
    constraint in the catalog gives both settings normalization and the GUI a
    single source of truth, instead of displaying choices that are silently
    discarded later.
    """
    entry = catalog_entry(profile)
    if entry and entry.get("language_hint") == "unsupported":
        return (LANGUAGE_AUTO,)
    return LANGUAGES


def is_language_available(profile, language):
    """Return whether ``language`` is an honored choice for ``profile``."""
    return language in available_languages(profile)


def format_size(size_bytes):
    """Human-readable size for the download UI (decimal MB/GB)."""
    if size_bytes >= 1024 ** 3:
        return f"{size_bytes / (1024 ** 3):.2f} GB"
    return f"{size_bytes / (1024 ** 2):.0f} MB"


def third_party_notices():
    """License lines for the settings dialog and a future About surface."""
    lines = [
        "transcribe.cpp — MIT",
        "sounddevice / PortAudio — MIT",
        "soxr / libsoxr / PFFFT — LGPLv2.1+ (python-soxr) and BSD-like (PFFFT)",
    ]
    for entry in selectable_catalog():
        lines.append(
            f"{entry['upstream_model']} — {entry['license_id']}. {entry['attribution']}"
        )
    return tuple(lines)


def default_language_for_profile(profile, requested=LANGUAGE_AUTO):
    """Resolve the language stored in settings against profile constraints.

    Qwen cannot take a language hint in transcribe.cpp, so accuracy always
    decodes with auto-detect. Nemotron's published English auto-detect WER is
    weaker, so streaming defaults to ``pt-BR`` when the user left Auto.
    """
    if requested not in LANGUAGES or not is_language_available(profile, requested):
        requested = LANGUAGE_AUTO
    entry = catalog_entry(profile)
    if entry and entry.get("language_hint") == "unsupported":
        return LANGUAGE_AUTO
    if profile == PROFILE_STREAMING and requested == LANGUAGE_AUTO:
        return LANGUAGE_PT_BR
    return requested
