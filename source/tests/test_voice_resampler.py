import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from voice_resampler import StreamingResampler, VoiceResamplerError


class _FakeStream:
    instances = []

    def __init__(self, *args, **kwargs):
        self.calls = []
        self.args = args
        self.kwargs = kwargs
        self.__class__.instances.append(self)

    def resample_chunk(self, values, last=False):
        values = list(values)
        self.calls.append((values, last))
        return [0.5] if last else values


class _FakeNumpy:
    float32 = object()

    @staticmethod
    def asarray(values, dtype=None):
        del dtype
        return list(values)

    @staticmethod
    def empty(shape, dtype=None):
        del shape, dtype
        return []


class StreamingResamplerTests(unittest.TestCase):
    def test_same_rate_is_passthrough_without_soxr(self):
        with mock.patch.dict("sys.modules", {"soxr": None}):
            resampler = StreamingResampler(16000)
        self.assertEqual(resampler.push([0.1, 0.2]), [0.1, 0.2])
        self.assertEqual(resampler.finish(), [])
        self.assertEqual(resampler.finish(), [])

    def test_missing_soxr_is_actionable_for_conversion(self):
        with mock.patch.dict("sys.modules", {"soxr": None}):
            with self.assertRaisesRegex(VoiceResamplerError, "conversor"):
                StreamingResampler(48000)

    def test_stream_state_is_retained_and_tail_flushed_once(self):
        _FakeStream.instances = []
        fake_soxr = types.SimpleNamespace(ResampleStream=_FakeStream)
        with mock.patch.dict(
            "sys.modules", {"soxr": fake_soxr, "numpy": _FakeNumpy}
        ):
            resampler = StreamingResampler(48000)
            self.assertEqual(len(resampler.push([0.1, 0.2])), 2)
            self.assertAlmostEqual(resampler.push([0.3])[0], 0.3, places=6)
            self.assertEqual(resampler.finish(), [0.5])
            self.assertEqual(resampler.finish(), [])
        stream = _FakeStream.instances[0]
        self.assertEqual(len(stream.calls), 3)
        self.assertFalse(stream.calls[0][1])
        self.assertFalse(stream.calls[1][1])
        self.assertTrue(stream.calls[2][1])

    def test_push_after_finish_fails_loudly(self):
        resampler = StreamingResampler(16000)
        resampler.finish()
        with self.assertRaises(VoiceResamplerError):
            resampler.push([0.1])

    def test_real_soxr_matches_one_shot_and_rejects_alias_band(self):
        try:
            import numpy as np
            import soxr
        except ImportError:
            self.skipTest("optional soxr runtime is not installed")

        time = np.arange(48000) / 48000
        speech_band = np.sin(2 * np.pi * 440 * time).astype(np.float32)
        resampler = StreamingResampler(48000)
        streamed = []
        for offset in range(0, len(speech_band), 997):
            streamed.extend(resampler.push(speech_band[offset : offset + 997]))
        streamed.extend(resampler.finish())
        one_shot = soxr.resample(speech_band, 48000, 16000, quality="HQ")
        np.testing.assert_allclose(streamed, one_shot, rtol=0, atol=1e-7)

        alias_band = np.sin(2 * np.pi * 12000 * time).astype(np.float32)
        resampler = StreamingResampler(48000)
        rejected = []
        for offset in range(0, len(alias_band), 997):
            rejected.extend(resampler.push(alias_band[offset : offset + 997]))
        rejected.extend(resampler.finish())
        interior = np.asarray(rejected[100:-100])
        self.assertLess(float(np.sqrt(np.mean(np.square(interior)))), 1e-5)


if __name__ == "__main__":
    unittest.main()
