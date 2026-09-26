"""Release gate for Snipvoice's custom LGPL audio-only FFmpeg runtime."""

from __future__ import annotations

import importlib
import shlex


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
REQUIRED_FORMATS = {"aac", "flac", "mp3", "ogg", "wav"}
REQUIRED_DEMUXERS = {"aac", "flac", "matroska", "mov", "mp3", "ogg", "wav"}
REQUIRED_CODECS = {"aac", "flac", "mp3", "opus", "pcm_s16le", "vorbis"}
REQUIRED_ENCODERS = {"libmp3lame"}
REQUIRED_MUXERS = {"mp3"}
REQUIRED_FILTERS = {"abuffer", "abuffersink", "aformat", "aresample"}


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
    enabled_demuxers = set()
    enabled_encoders = set()
    enabled_muxers = set()
    enabled_filters = set()
    for token in shlex.split(configuration):
        if token.startswith("--enable-demuxer="):
            enabled_demuxers.update(token.split("=", 1)[1].split(","))
        elif token.startswith("--enable-encoder="):
            enabled_encoders.update(token.split("=", 1)[1].split(","))
        elif token.startswith("--enable-muxer="):
            enabled_muxers.update(token.split("=", 1)[1].split(","))
        elif token.startswith("--enable-filter="):
            enabled_filters.update(token.split("=", 1)[1].split(","))
    missing_demuxers = REQUIRED_DEMUXERS - enabled_demuxers
    missing_formats = REQUIRED_FORMATS - set(av_module.formats_available)
    missing_codecs = REQUIRED_CODECS - set(av_module.codecs_available)
    missing_encoders = REQUIRED_ENCODERS - enabled_encoders
    missing_muxers = REQUIRED_MUXERS - enabled_muxers
    missing_encoder_codecs = REQUIRED_ENCODERS - set(av_module.codecs_available)
    filter_module = getattr(av_module, "filter", None)
    available_filters = set(getattr(filter_module, "filters_available", ()))
    missing_filters = REQUIRED_FILTERS - enabled_filters
    unavailable_filters = REQUIRED_FILTERS - available_filters
    if (missing_demuxers or missing_formats or missing_codecs or missing_encoders
            or missing_muxers or missing_encoder_codecs or missing_filters
            or unavailable_filters):
        raise RuntimeError(
            "Clean FFmpeg runtime is missing required audio support: "
            f"demuxers={sorted(missing_demuxers)}, "
            f"formats={sorted(missing_formats)}, codecs={sorted(missing_codecs)}, "
            f"encoders={sorted(missing_encoders | missing_encoder_codecs)}, "
            f"muxers={sorted(missing_muxers)}, "
            f"filters={sorted(missing_filters | unavailable_filters)}"
        )
    codec_factory = getattr(av_module, "Codec", None)
    if not callable(codec_factory):
        codec_module = getattr(av_module, "codec", None)
        codec_factory = getattr(codec_module, "Codec", None)
    if not callable(codec_factory):
        raise RuntimeError("Clean FFmpeg runtime does not expose the PyAV codec factory")
    try:
        codec_factory("libmp3lame", "w")
    except Exception as exc:
        raise RuntimeError("Clean FFmpeg runtime cannot instantiate the MP3 encoder") from exc
    container_factory = getattr(av_module, "ContainerFormat", None)
    if not callable(container_factory):
        format_module = getattr(av_module, "format", None)
        container_factory = getattr(format_module, "ContainerFormat", None)
    if not callable(container_factory):
        raise RuntimeError("Clean FFmpeg runtime does not expose the container format factory")
    try:
        mp3_format = container_factory("mp3")
    except Exception as exc:
        raise RuntimeError("Clean FFmpeg runtime cannot instantiate the MP3 muxer") from exc
    if not getattr(mp3_format, "is_output", False):
        raise RuntimeError("Clean FFmpeg runtime does not expose MP3 as an output format")
