"""Release gate for Snipvoice's custom LGPL audio-only FFmpeg runtime."""

from __future__ import annotations

import importlib


REQUIRED_FLAGS = {
    "--disable-static",
    "--enable-shared",
    "--disable-gpl",
    "--disable-nonfree",
    "--disable-version3",
    "--disable-network",
    "--disable-autodetect",
    "--disable-everything",
    "--enable-protocol=file",
}
FORBIDDEN_FLAGS = {
    "--enable-gpl",
    "--enable-nonfree",
    "--enable-version3",
    "--enable-libx264",
    "--enable-libx265",
    "--enable-libxvid",
    "--enable-libfdk-aac",
    "--enable-librubberband",
    "--enable-libvidstab",
}
REQUIRED_FORMATS = {"aac", "flac", "mov", "mp3", "ogg", "wav"}
REQUIRED_CODECS = {"aac", "flac", "mp3", "opus", "pcm_s16le", "vorbis"}


def verify_clean_ffmpeg_runtime(av_module=None) -> None:
    """Reject release wheels not built from the approved audio-only recipe."""
    if av_module is None:
        import av as av_module

    if av_module.__version__ != "18.1.0":
        raise RuntimeError(f"Unexpected PyAV version: {av_module.__version__}")
    if av_module.ffmpeg_version_info != "8.1.2":
        raise RuntimeError(
            f"Unexpected FFmpeg version: {av_module.ffmpeg_version_info}"
        )
    core = getattr(av_module, "_core", None)
    if core is None:
        core = importlib.import_module("av._core")
    metadata = core.library_meta
    if not metadata:
        raise RuntimeError("PyAV did not expose FFmpeg library metadata")
    configurations = {item["configuration"] for item in metadata.values()}
    licenses = {item["license"] for item in metadata.values()}
    if len(configurations) != 1:
        raise RuntimeError("Bundled FFmpeg libraries have inconsistent configurations")
    configuration = configurations.pop()
    missing = sorted(flag for flag in REQUIRED_FLAGS if flag not in configuration)
    forbidden = sorted(flag for flag in FORBIDDEN_FLAGS if flag in configuration)
    if missing or forbidden:
        raise RuntimeError(
            f"Unapproved FFmpeg configuration; missing={missing}, forbidden={forbidden}"
        )
    if not licenses or any(not license_name.startswith("LGPL") for license_name in licenses):
        raise RuntimeError(f"Bundled FFmpeg is not LGPL: {sorted(licenses)}")
    missing_formats = REQUIRED_FORMATS - set(av_module.formats_available)
    missing_codecs = REQUIRED_CODECS - set(av_module.codecs_available)
    if missing_formats or missing_codecs:
        raise RuntimeError(
            "Clean FFmpeg runtime is missing required audio support: "
            f"formats={sorted(missing_formats)}, codecs={sorted(missing_codecs)}"
        )
