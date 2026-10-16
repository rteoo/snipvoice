import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from voice_activity import SileroVoiceActivity, VoiceActivityError


class _Input:
    def __init__(self, name):
        self.name = name


class _Session:
    def __init__(self, probabilities):
        self.probabilities = iter(probabilities)
        self.calls = 0
        self.input_lengths = []
        self.input_frames = []

    def get_inputs(self):
        return [_Input("input"), _Input("state"), _Input("sr")]

    def run(self, _outputs, _payload):
        import numpy as np
        self.calls += 1
        self.input_lengths.append(len(_payload["input"].reshape(-1)))
        self.input_frames.append(_payload["input"].reshape(-1).copy())
        probability = next(self.probabilities)
        return [np.asarray([[probability]], dtype=np.float32),
                np.zeros((2, 1, 128), dtype=np.float32)]


class _Runtime:
    def __init__(self, session):
        self.session = session

    def InferenceSession(self, _path, providers):
        self.providers = providers
        return self.session


class VoiceActivityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        (Path(__file__).parent / "tmp").mkdir(exist_ok=True)

    def _detector(self, probabilities, **kwargs):
        root = tempfile.TemporaryDirectory(dir=Path(__file__).parent / "tmp")
        path = Path(root.name) / "silero.onnx"
        path.write_bytes(b"fixture")
        session = _Session(probabilities)
        detector = SileroVoiceActivity(
            path, runtime=_Runtime(session), frame_samples=512, **kwargs
        )
        self.addCleanup(root.cleanup)
        return detector, session

    def test_requires_two_consecutive_speech_frames(self):
        detector, session = self._detector([0.8, 0.1, 0.8])
        self.assertFalse(detector.detect([0.0] * 1536))
        self.assertEqual(session.calls, 3)

        detector, session = self._detector([0.8, 0.8])
        self.assertTrue(detector.detect([1.0] * 1024))
        self.assertEqual(session.calls, 2)
        self.assertEqual(session.input_lengths, [576, 576])
        self.assertTrue((session.input_frames[0][:64] == 0).all())
        self.assertTrue((session.input_frames[1][:64] == session.input_frames[0][-64:]).all())

    def test_cancellation_is_actionable(self):
        import threading
        detector, _session = self._detector([0.1])
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaisesRegex(VoiceActivityError, "cancelled"):
            detector.detect([0.0] * 512, cancelled)

    def test_invalid_model_output_fails_closed(self):
        class BadSession(_Session):
            def run(self, _outputs, _payload):
                import numpy as np
                return [np.asarray([[2.0]], dtype=np.float32),
                        np.zeros((2, 1, 128), dtype=np.float32)]

        root = tempfile.TemporaryDirectory(dir=Path(__file__).parent / "tmp")
        path = Path(root.name) / "silero.onnx"
        path.write_bytes(b"fixture")
        detector = SileroVoiceActivity(path, runtime=_Runtime(BadSession([])), frame_samples=512)
        self.addCleanup(root.cleanup)
        with self.assertRaisesRegex(VoiceActivityError, "invalid probability"):
            detector.detect([0.0] * 512)

    def test_ranges_have_context_padding_and_do_not_duplicate_overlaps(self):
        detector, _session = self._detector(
            [0.8, 0.8, 0.1, 0.1, 0.8, 0.8],
            min_speech_frames=2,
        )
        ranges = detector.speech_ranges([0.0] * (6 * 512))
        self.assertEqual(ranges, ((0, 6 * 512),))
        detector, _session = self._detector([0.8, 0.8, 0.1, 0.1, 0.8, 0.8])
        voiced = detector.voiced_samples([float(index) for index in range(6 * 512)])
        self.assertEqual(len(voiced), 6 * 512)

    def test_long_noise_without_voiced_frames_is_rejected(self):
        detector, _session = self._detector([0.01] * 240)
        self.assertFalse(detector.detect([0.0] * (240 * 512)))
