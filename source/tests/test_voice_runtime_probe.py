import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import voice_runtime_probe


class VoiceRuntimeProbeTests(unittest.TestCase):
    def test_probe_accepts_a_complete_runtime(self):
        backend = mock.Mock()
        backend.available.return_value = True
        modules = {
            "tkinter": types.ModuleType("tkinter"),
            "av": types.ModuleType("av"),
            "onnxruntime": types.ModuleType("onnxruntime"),
            "sounddevice": types.ModuleType("sounddevice"),
            "soxr": types.ModuleType("soxr"),
            "transcribe_cpp_native": types.ModuleType("transcribe_cpp_native"),
        }
        with mock.patch.dict(sys.modules, modules), \
                mock.patch.object(voice_runtime_probe, "create_backend", return_value=backend), \
                mock.patch.object(voice_runtime_probe, "verify_clean_ffmpeg_runtime") as verify, \
                mock.patch.object(voice_runtime_probe, "probe_summary_isolation") as isolate, \
                mock.patch.object(voice_runtime_probe, "verified_model_path", return_value="vad.onnx"), \
                mock.patch.object(voice_runtime_probe, "SileroVoiceActivity") as detector_type:
            detector_type.return_value.detect.return_value = False
            self.assertTrue(voice_runtime_probe.probe_voice_runtime())
            isolate.assert_called_once_with()
        verify.assert_called_once_with(modules["av"])
        detector_type.assert_called_once_with(model_path="vad.onnx", runtime=modules["onnxruntime"])
        detector_type.return_value.detect.assert_called_once_with([0.0] * 16_000)

    def test_probe_fails_when_the_backend_cannot_load(self):
        backend = mock.Mock()
        backend.available.return_value = False
        modules = {
            "tkinter": types.ModuleType("tkinter"),
            "av": types.ModuleType("av"),
            "onnxruntime": types.ModuleType("onnxruntime"),
            "sounddevice": types.ModuleType("sounddevice"),
            "soxr": types.ModuleType("soxr"),
            "transcribe_cpp_native": types.ModuleType("transcribe_cpp_native"),
        }
        with mock.patch.dict(sys.modules, modules), \
                mock.patch.object(voice_runtime_probe, "create_backend", return_value=backend), \
                mock.patch.object(voice_runtime_probe, "verify_clean_ffmpeg_runtime"), \
                mock.patch.object(voice_runtime_probe, "probe_summary_isolation"), \
                mock.patch.object(voice_runtime_probe, "verified_model_path", return_value="vad.onnx"), \
                mock.patch.object(voice_runtime_probe, "SileroVoiceActivity") as detector_type:
            detector_type.return_value.detect.return_value = False
            with self.assertRaisesRegex(RuntimeError, "transcribe.cpp"):
                voice_runtime_probe.probe_voice_runtime()

    def test_probe_rejects_silence_classified_as_speech(self):
        backend = mock.Mock()
        backend.available.return_value = True
        modules = {
            name: types.ModuleType(name)
            for name in ("tkinter", "av", "onnxruntime", "sounddevice", "soxr", "transcribe_cpp_native")
        }
        with mock.patch.dict(sys.modules, modules), \
                mock.patch.object(voice_runtime_probe, "create_backend", return_value=backend), \
                mock.patch.object(voice_runtime_probe, "verify_clean_ffmpeg_runtime"), \
                mock.patch.object(voice_runtime_probe, "probe_summary_isolation"), \
                mock.patch.object(voice_runtime_probe, "verified_model_path", return_value="vad.onnx"), \
                mock.patch.object(voice_runtime_probe, "SileroVoiceActivity") as detector_type:
            detector_type.return_value.detect.return_value = True
            with self.assertRaisesRegex(RuntimeError, "silence"):
                voice_runtime_probe.probe_voice_runtime()


if __name__ == "__main__":
    unittest.main()
