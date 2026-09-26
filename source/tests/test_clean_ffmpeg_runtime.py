import os
import sys
import types
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from clean_ffmpeg_runtime import (  # noqa: E402
    REQUIRED_CODECS,
    REQUIRED_DEMUXERS,
    REQUIRED_ENCODERS,
    REQUIRED_FILTERS,
    REQUIRED_FLAGS,
    REQUIRED_FORMATS,
    REQUIRED_MUXERS,
    verify_clean_ffmpeg_runtime,
)


def fake_av(*, configuration=None, license_name="LGPL version 2.1 or later"):
    configuration = configuration or " ".join(
        (
            *sorted(REQUIRED_FLAGS),
            f"--enable-demuxer='{','.join(sorted(REQUIRED_DEMUXERS))}'",
            f"--enable-encoder='{','.join(sorted(REQUIRED_ENCODERS))}'",
            f"--enable-muxer='{','.join(sorted(REQUIRED_MUXERS))}'",
            f"--enable-filter='{','.join(sorted(REQUIRED_FILTERS))}'",
        )
    )
    metadata = {
        name: {"configuration": configuration, "license": license_name}
        for name in (
            "libavcodec",
            "libavdevice",
            "libavfilter",
            "libavformat",
            "libavutil",
            "libswresample",
            "libswscale",
        )
    }
    class FakeCodec:
        def __new__(cls, name, mode):
            if name != "libmp3lame" or mode != "w":
                raise ValueError(name)
            return object()

    class FakeContainerFormat:
        def __init__(self, name):
            if name != "mp3":
                raise ValueError(name)
            self.is_output = True

    return types.SimpleNamespace(
        __version__="18.1.0",
        ffmpeg_version_info="8.1.2",
        _core=types.SimpleNamespace(library_meta=metadata),
        formats_available=REQUIRED_FORMATS,
        codecs_available=REQUIRED_CODECS | REQUIRED_ENCODERS,
        Codec=FakeCodec,
        ContainerFormat=FakeContainerFormat,
        filter=types.SimpleNamespace(filters_available=REQUIRED_FILTERS),
    )


class CleanFfmpegRuntimeTests(unittest.TestCase):
    def test_accepts_approved_shared_lgpl_audio_runtime(self):
        verify_clean_ffmpeg_runtime(fake_av())

    def test_rejects_gpl_configuration(self):
        configuration = " ".join((*sorted(REQUIRED_FLAGS), "--enable-gpl"))
        with self.assertRaisesRegex(RuntimeError, "--enable-gpl"):
            verify_clean_ffmpeg_runtime(fake_av(configuration=configuration))

    def test_rejects_non_lgpl_runtime(self):
        with self.assertRaisesRegex(RuntimeError, "not LGPL"):
            verify_clean_ffmpeg_runtime(fake_av(license_name="GPL version 3 or later"))

    def test_rejects_wrong_ffmpeg_version(self):
        runtime = fake_av()
        runtime.ffmpeg_version_info = "8.2"
        with self.assertRaisesRegex(RuntimeError, "8.2"):
            verify_clean_ffmpeg_runtime(runtime)

    def test_rejects_missing_audio_decoder(self):
        runtime = fake_av()
        runtime.codecs_available = REQUIRED_CODECS - {"opus"}
        with self.assertRaisesRegex(RuntimeError, "opus"):
            verify_clean_ffmpeg_runtime(runtime)

    def test_rejects_missing_m4a_demuxer(self):
        runtime = fake_av()
        configuration = runtime._core.library_meta["libavcodec"]["configuration"]
        configuration = configuration.replace(",mov", "")
        for metadata in runtime._core.library_meta.values():
            metadata["configuration"] = configuration
        with self.assertRaisesRegex(RuntimeError, "mov"):
            verify_clean_ffmpeg_runtime(runtime)

    def test_rejects_missing_mp3_encoder_flag(self):
        runtime = fake_av()
        configuration = runtime._core.library_meta["libavcodec"]["configuration"]
        configuration = configuration.replace("--enable-encoder='libmp3lame'", "")
        for metadata in runtime._core.library_meta.values():
            metadata["configuration"] = configuration
        with self.assertRaisesRegex(RuntimeError, "encoders"):
            verify_clean_ffmpeg_runtime(runtime)

    def test_rejects_missing_mp3_encoder_codec(self):
        runtime = fake_av()
        runtime.codecs_available = REQUIRED_CODECS
        with self.assertRaisesRegex(RuntimeError, "encoders"):
            verify_clean_ffmpeg_runtime(runtime)

    def test_rejects_runtime_without_constructible_mp3_encoder(self):
        runtime = fake_av()
        runtime.Codec = lambda *_args: (_ for _ in ()).throw(ValueError("missing"))
        with self.assertRaisesRegex(RuntimeError, "instantiate the MP3 encoder"):
            verify_clean_ffmpeg_runtime(runtime)

    def test_rejects_runtime_without_audio_resampler_filters(self):
        runtime = fake_av()
        runtime.filter.filters_available = REQUIRED_FILTERS - {"aformat"}
        with self.assertRaisesRegex(RuntimeError, "filters"):
            verify_clean_ffmpeg_runtime(runtime)

    def test_rejects_recipe_without_audio_resampler_filters(self):
        runtime = fake_av()
        configuration = runtime._core.library_meta["libavcodec"]["configuration"]
        configuration = configuration.replace(
            "--enable-filter='abuffer,abuffersink,aformat,aresample'", ""
        )
        for metadata in runtime._core.library_meta.values():
            metadata["configuration"] = configuration
        with self.assertRaisesRegex(RuntimeError, "filters"):
            verify_clean_ffmpeg_runtime(runtime)


if __name__ == "__main__":
    unittest.main()
