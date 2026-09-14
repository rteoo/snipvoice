import math
import os
import sys
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from voice_audio import AudioCapture, BLOCK_SIZE, CaptureIssue, VoiceAudioError


class _FakeResampleStream:
    def __init__(self, *args, **kwargs):
        self.chunks = []
        self.finished = False

    def resample_chunk(self, values, last=False):
        self.chunks.append(list(values))
        if last:
            self.finished = True
            return [0.75]
        return values


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


class _BlockingResampler:
    entered = None
    release = None

    def __init__(self, *args, **kwargs):
        del args, kwargs

    def push(self, values):
        self.__class__.entered.set()
        self.__class__.release.wait(2)
        return values

    def finish(self):
        return []


class AudioCaptureTests(unittest.TestCase):
    def _sounddevice(self, info=None):
        stream = mock.Mock()
        options = {}

        def input_stream(**kwargs):
            options.update(kwargs)
            return stream

        sounddevice = mock.Mock(InputStream=mock.Mock(side_effect=input_stream))
        sounddevice.query_devices.return_value = info if info is not None else mock.Mock()
        return sounddevice, options, stream

    def test_stop_without_start_is_empty_result(self):
        result = AudioCapture().stop()
        self.assertEqual(result.samples, [])
        self.assertTrue(result.ok)
        samples, failed = result
        self.assertEqual(samples, [])
        self.assertFalse(failed)

    def test_missing_sounddevice_is_loud(self):
        capture = AudioCapture()
        with mock.patch.dict("sys.modules", {"sounddevice": None}):
            with mock.patch("builtins.__import__", side_effect=ImportError("no sd")):
                with self.assertRaises(VoiceAudioError):
                    capture.start()

    def test_stream_is_closed_even_without_journal(self):
        sounddevice, options, stream = self._sounddevice()
        capture = AudioCapture()
        with mock.patch.dict("sys.modules", {"sounddevice": sounddevice}):
            capture.start()
            options["callback"]([0.0] * BLOCK_SIZE, BLOCK_SIZE, None, None)
            result = capture.stop()
        stream.stop.assert_called_once_with()
        stream.close.assert_called_once_with()
        self.assertTrue(result.ok)

    def test_sounddevice_mono_matrix_is_flattened(self):
        sounddevice, options, _stream = self._sounddevice(
            {"default_samplerate": 16000, "max_input_channels": 1}
        )
        capture = AudioCapture()
        with mock.patch.dict("sys.modules", {"sounddevice": sounddevice}):
            capture.start()
            options["callback"]([[0.25], [-0.5]], 2, None, None)
            result = capture.stop()

        self.assertTrue(result.ok)
        self.assertEqual(result.samples, [0.25, -0.5])

    def test_native_rate_and_stereo_are_normalized(self):
        fake_soxr = types.SimpleNamespace(ResampleStream=_FakeResampleStream)
        sounddevice, options, _stream = self._sounddevice(
            {"default_samplerate": 48000, "max_input_channels": 2}
        )
        sounddevice.check_input_settings.side_effect = (
            lambda **kwargs: (_ for _ in ()).throw(OSError("mono unsupported"))
            if kwargs["channels"] == 1
            else None
        )
        capture = AudioCapture()
        stereo = [[0.2, 0.4], [-0.4, 0.0]]
        with mock.patch.dict(
            "sys.modules",
            {"sounddevice": sounddevice, "soxr": fake_soxr, "numpy": _FakeNumpy},
        ):
            capture.start()
            options["callback"](stereo, 2, None, None)
            result = capture.stop()
        self.assertEqual(options["samplerate"], 48000.0)
        self.assertEqual(options["channels"], 2)
        self.assertEqual(result.source_sample_rate, 48000.0)
        self.assertEqual(result.source_channels, 2)
        self.assertEqual(len(result.samples), 3)
        self.assertAlmostEqual(result.samples[0], 0.3, places=6)
        self.assertAlmostEqual(result.samples[1], -0.2, places=6)
        self.assertAlmostEqual(result.samples[2], 0.75, places=6)

    def test_raw_queue_covers_full_native_rate_capture_window(self):
        fake_soxr = types.SimpleNamespace(ResampleStream=_FakeResampleStream)
        sounddevice, _options, _stream = self._sounddevice(
            {"default_samplerate": 48000, "max_input_channels": 1}
        )
        capture = AudioCapture(max_samples=16000 * 3)
        with mock.patch.dict(
            "sys.modules",
            {"sounddevice": sounddevice, "soxr": fake_soxr, "numpy": _FakeNumpy},
        ):
            capture.start()
            self.assertGreaterEqual(
                capture._raw_queue.maxsize,
                math.ceil(3 * 48000 / BLOCK_SIZE),
            )
            capture.stop()

    def test_read_chunk_exposes_normalized_public_stream(self):
        sounddevice, options, _stream = self._sounddevice()
        capture = AudioCapture()
        with mock.patch.dict("sys.modules", {"sounddevice": sounddevice}):
            capture.start()
            options["callback"]([0.25] * BLOCK_SIZE, BLOCK_SIZE, None, None)
            deadline = time.time() + 1
            chunk = None
            while time.time() < deadline:
                try:
                    chunk = capture.read_chunk(timeout=0.01)
                    break
                except Exception:
                    pass
            result = capture.stop()
        self.assertEqual(len(chunk), BLOCK_SIZE)
        self.assertEqual(len(result.samples), BLOCK_SIZE)

    def test_callback_status_is_not_mislabeled_as_duration_overflow(self):
        sounddevice, options, _stream = self._sounddevice()
        capture = AudioCapture()
        with mock.patch.dict("sys.modules", {"sounddevice": sounddevice}):
            capture.start()
            options["callback"]([0.0] * BLOCK_SIZE, BLOCK_SIZE, None, "input overflow")
            result = capture.stop()
        self.assertEqual(result.issue, CaptureIssue.INPUT_STATUS)
        self.assertNotEqual(result.issue, CaptureIssue.DURATION_LIMIT)
        self.assertEqual(result.samples, [0.0] * BLOCK_SIZE)

    def test_unexpected_stream_finish_is_a_capture_issue(self):
        sounddevice, options, _stream = self._sounddevice()
        capture = AudioCapture()
        with mock.patch.dict("sys.modules", {"sounddevice": sounddevice}):
            capture.start()
            options["finished_callback"]()
            result = capture.stop()
        self.assertEqual(result.issue, CaptureIssue.INPUT_STATUS)
        self.assertIn("inesperadamente", result.message)

    def test_normal_stop_does_not_report_stream_finish(self):
        sounddevice, options, stream = self._sounddevice()
        stream.stop.side_effect = lambda: options["finished_callback"]()
        capture = AudioCapture()
        with mock.patch.dict("sys.modules", {"sounddevice": sounddevice}):
            capture.start()
            result = capture.stop()
        self.assertTrue(result.ok)

    def test_duration_ceiling_preserves_partial_audio_and_issue(self):
        sounddevice, options, _stream = self._sounddevice()
        capture = AudioCapture(max_samples=BLOCK_SIZE * 2, queue_max=10)
        with mock.patch.dict("sys.modules", {"sounddevice": sounddevice}):
            capture.start()
            for _ in range(3):
                options["callback"]([0.0] * BLOCK_SIZE, BLOCK_SIZE, None, None)
            result = capture.stop()
        self.assertEqual(len(result.samples), BLOCK_SIZE * 2)
        self.assertEqual(result.issue, CaptureIssue.DURATION_LIMIT)

    def test_journal_receives_canonical_chunks_before_stop_returns(self):
        sounddevice, options, _stream = self._sounddevice()
        journal = mock.Mock()
        capture = AudioCapture()
        capture.set_journal(journal)
        with mock.patch.dict("sys.modules", {"sounddevice": sounddevice}):
            capture.start()
            options["callback"]([0.25] * BLOCK_SIZE, BLOCK_SIZE, None, None)
            result = capture.stop()
        journal.write_chunk.assert_called_once_with([0.25] * BLOCK_SIZE)
        self.assertEqual(len(result.samples), BLOCK_SIZE)
        self.assertTrue(result.ok)

    def test_stop_and_close_errors_are_structured(self):
        sounddevice, options, stream = self._sounddevice()
        stream.stop.side_effect = OSError("stop failed")
        stream.close.side_effect = OSError("close failed")
        capture = AudioCapture()
        with mock.patch.dict("sys.modules", {"sounddevice": sounddevice}):
            capture.start()
            options["callback"]([0.0], 1, None, None)
            result = capture.stop()
        self.assertEqual(result.issue, CaptureIssue.STOP)
        self.assertEqual(
            {issue for issue, _message in result.issues},
            {CaptureIssue.STOP, CaptureIssue.CLOSE},
        )

    def test_timed_out_worker_cannot_mutate_next_session(self):
        sounddevice, options, _stream = self._sounddevice()
        capture = AudioCapture()
        _BlockingResampler.entered = __import__("threading").Event()
        _BlockingResampler.release = __import__("threading").Event()
        with mock.patch("voice_audio.StreamingResampler", _BlockingResampler), mock.patch.dict(
            "sys.modules", {"sounddevice": sounddevice}
        ):
            capture.start()
            options["callback"]([0.5], 1, None, None)
            self.assertTrue(_BlockingResampler.entered.wait(1))
            with mock.patch.object(capture, "_join_worker"):
                result = capture.stop()
            self.assertEqual(result.issue, CaptureIssue.NORMALIZATION)
            frozen = list(result.samples)
            with self.assertRaisesRegex(VoiceAudioError, "ainda está encerrando"):
                capture.start()
            other_capture = AudioCapture()
            with self.assertRaisesRegex(VoiceAudioError, "ainda está encerrando"):
                other_capture.start()
            _BlockingResampler.release.set()
            deadline = time.time() + 1
            while capture._worker.is_alive() and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(result.samples, frozen)

            capture.start()
            options["callback"]([0.25], 1, None, None)
            next_result = capture.stop()
            other_capture.start()
            other_result = other_capture.stop()
        self.assertEqual(next_result.samples, [0.25])
        self.assertTrue(next_result.ok)
        self.assertTrue(other_result.ok)


if __name__ == "__main__":
    unittest.main()
