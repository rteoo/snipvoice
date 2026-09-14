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
            "sounddevice": types.ModuleType("sounddevice"),
            "soxr": types.ModuleType("soxr"),
            "transcribe_cpp_native": types.ModuleType("transcribe_cpp_native"),
        }
        with mock.patch.dict(sys.modules, modules), \
                mock.patch.object(voice_runtime_probe, "create_backend", return_value=backend):
            self.assertTrue(voice_runtime_probe.probe_voice_runtime())

    def test_probe_fails_when_the_backend_cannot_load(self):
        backend = mock.Mock()
        backend.available.return_value = False
        modules = {
            "tkinter": types.ModuleType("tkinter"),
            "sounddevice": types.ModuleType("sounddevice"),
            "soxr": types.ModuleType("soxr"),
            "transcribe_cpp_native": types.ModuleType("transcribe_cpp_native"),
        }
        with mock.patch.dict(sys.modules, modules), \
                mock.patch.object(voice_runtime_probe, "create_backend", return_value=backend):
            with self.assertRaisesRegex(RuntimeError, "transcribe.cpp"):
                voice_runtime_probe.probe_voice_runtime()


if __name__ == "__main__":
    unittest.main()
