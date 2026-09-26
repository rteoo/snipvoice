"""MP3 streaming contracts and optional integration against the bundled codec."""

from contextlib import contextmanager
from fractions import Fraction
import math
from pathlib import Path
import struct
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

from meeting_files import PLAY_FRAMES, _final_audio_chunks
from meeting_mixdown import MixdownCancelled, mixdown_tracks


def tone(rate=48000, channels=1, seconds=1):
    for start in range(0, int(rate * seconds), 4096):
        frames = min(4096, int(rate * seconds) - start)
        samples = [0.25 * math.sin(2 * math.pi * 440 * (start + index) / rate)
                   for index in range(frames) for _channel in range(channels)]
        yield ({"type": "audio", "track": "microphone", "rate": rate,
                "channels": channels, "frames": frames, "timestamp": start / rate},
               struct.pack(f"<{len(samples)}f", *samples))


class Mp3WriterTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).parent / "tmp"
        scratch.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.path = self.folder / "recording.mp3"

    @contextmanager
    def encoder(self, fail=False, cancel=None):
        frames = []
        state = {"frames": frames, "flushed": False, "closed": False}
        stream = types.SimpleNamespace(codec_context=types.SimpleNamespace())

        def encode(frame):
            if fail:
                raise ValueError("synthetic encoder failure")
            if frame is None:
                state["flushed"] = True
                return [b"encoder-tail"]
            frames.append(frame)
            if cancel is not None:
                cancel.set()
            return [b"encoded-block"]

        stream.encode = encode

        class Frame:
            def __init__(self, *, format, layout, samples):
                self.format, self.layout, self.samples = format, layout, samples
                self.planes = [types.SimpleNamespace(update=lambda payload: None)]

        @contextmanager
        def open_output(handle, **kwargs):
            state["open"] = kwargs

            def add_stream(codec, rate):
                state["codec"], state["rate"] = codec, rate
                return stream

            try:
                yield types.SimpleNamespace(add_stream=add_stream, mux=handle.write)
            finally:
                state["closed"] = True

        runtime = types.SimpleNamespace(
            codec=types.SimpleNamespace(Codec=mock.Mock()), open=open_output, AudioFrame=Frame,
        )
        with mock.patch.dict(sys.modules, {"av": runtime}):
            yield state, stream

    def test_streams_bounded_frames_and_flushes_before_publication(self):
        with self.encoder() as (state, stream):
            mixdown_tracks({"microphone": tone(channels=2)}, self.path)
        self.assertEqual(state["codec"], "libmp3lame")
        self.assertEqual(state["open"], {"mode": "w", "format": "mp3"})
        self.assertEqual(state["rate"], 48000)
        self.assertEqual(stream.bit_rate, 192000)
        self.assertTrue(state["flushed"] and state["closed"])
        self.assertTrue(self.path.read_bytes().endswith(b"encoder-tail"))
        self.assertFalse(self.path.read_bytes().startswith(b"RIFF"))
        frames = state["frames"]
        self.assertEqual(sum(frame.samples for frame in frames), 48000)
        self.assertTrue(all(frame.samples <= 4096 for frame in frames))
        self.assertEqual(frames[1].pts, frames[0].samples)
        self.assertEqual(frames[0].time_base, Fraction(1, 48000))
        self.assertEqual(list(self.folder.iterdir()), [self.path])

    def test_high_sample_rate_and_low_rate_stereo_use_valid_encoder_settings(self):
        for rate, channels, expected_rate, bitrate in ((96000, 1, 48000, 128000),
                                                     (16000, 2, 16000, 96000),
                                                     (16000, 1, 16000, 64000),
                                                     (8000, 1, 8000, 32000),
                                                     (11025, 2, 11025, 64000)):
            with self.subTest(rate=rate), self.encoder() as (state, stream):
                mixdown_tracks({"microphone": tone(rate, channels, .1)}, self.path)
                self.assertEqual(state["rate"], expected_rate)
                self.assertEqual(stream.bit_rate, bitrate)
                self.assertEqual(state["frames"][0].sample_rate, rate)

    def test_encoder_failure_preserves_previous_export(self):
        self.path.write_bytes(b"previous")
        with self.encoder(fail=True) as (state, _stream):
            with self.assertRaisesRegex(ValueError, "encoder failure"):
                mixdown_tracks({"microphone": tone()}, self.path)
        self.assertTrue(state["closed"])
        self.assertEqual(self.path.read_bytes(), b"previous")
        self.assertEqual(list(self.folder.iterdir()), [self.path])

    def test_cancellation_preserves_previous_export(self):
        self.path.write_bytes(b"previous")
        cancelled = threading.Event()
        with self.encoder(cancel=cancelled) as (state, _stream):
            with self.assertRaises(MixdownCancelled):
                mixdown_tracks({"microphone": tone()}, self.path, cancel_event=cancelled)
        self.assertTrue(state["closed"])
        self.assertFalse(state["flushed"])
        self.assertEqual(self.path.read_bytes(), b"previous")
        self.assertEqual(list(self.folder.iterdir()), [self.path])

    def test_missing_encoder_has_actionable_error_and_no_disguised_wav(self):
        with mock.patch.dict(sys.modules, {"av": None}):
            with self.assertRaisesRegex(RuntimeError, "codificador"):
                mixdown_tracks({"microphone": tone()}, self.path)
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_decode_only_runtime_does_not_publish_wav_with_mp3_extension(self):
        self.path.write_bytes(b"previous")
        runtime = types.SimpleNamespace(codec=types.SimpleNamespace(
            Codec=mock.Mock(side_effect=ValueError("unknown encoder")),
        ))
        with mock.patch.dict(sys.modules, {"av": runtime}):
            with self.assertRaisesRegex(RuntimeError, "codificador"):
                mixdown_tracks({"microphone": tone()}, self.path)
        self.assertEqual(self.path.read_bytes(), b"previous")
        self.assertEqual(list(self.folder.iterdir()), [self.path])


class Mp3CodecIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import av
            av.codec.Codec("libmp3lame", "w")
        except (ImportError, ValueError) as exc:
            raise unittest.SkipTest(f"Bundled MP3 encoder unavailable: {exc}") from exc
        cls.av = av

    def test_real_mp3_roundtrip_is_smaller_and_preserves_timing_and_channels(self):
        scratch = Path(__file__).parent / "tmp"
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            for rate, channels in ((48000, 1), (48000, 2), (96000, 2), (16000, 1),
                                   (16000, 2), (8000, 1), (11025, 2), (44100, 1)):
                with self.subTest(rate=rate, channels=channels):
                    path = Path(directory) / f"{rate}-{channels}.mp3"
                    mixdown_tracks({"microphone": tone(rate, channels, 2)}, path)
                    self.assertLess(path.stat().st_size, rate * channels * 2 * 2 / 3)
                    with self.av.open(str(path)) as container:
                        stream = container.streams.best("audio")
                        self.assertEqual(stream.codec_context.name, "mp3float")
                        decoded = list(container.decode(stream))
                        self.assertTrue(decoded)
                        output_rate = decoded[0].sample_rate
                        self.assertEqual(decoded[0].layout.nb_channels, channels)
                        self.assertAlmostEqual(sum(frame.samples for frame in decoded) / output_rate, 2, delta=.03)

    def test_real_final_playback_seek_tail_and_eof(self):
        scratch = Path(__file__).parent / "tmp"
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            path = Path(directory) / "seek.mp3"
            mixdown_tracks({"microphone": tone(seconds=3)}, path)
            metadata = {"final_audio": {"path": str(path)}}
            for start in (0, .011, 1, 2.99, 3, 8):
                with self.subTest(start=start):
                    chunks = list(_final_audio_chunks(metadata, start, threading.Event()))
                    duration = sum(len(payload) / (rate * channels * 4)
                                   for rate, channels, payload in chunks)
                    self.assertAlmostEqual(duration, max(0, 3 - start), delta=.01)
                    self.assertTrue(all(len(payload) <= PLAY_FRAMES * channels * 4
                                        for _rate, channels, payload in chunks))


if __name__ == "__main__":
    unittest.main()
