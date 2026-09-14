import os
import unittest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


class PackagingExcludeTests(unittest.TestCase):
    def test_windows_and_macos_scripts_exclude_the_dead_ml_stack(self):
        expected = (
            "torch",
            "torchvision",
            "torchaudio",
            "cv2",
            "transformers",
            "onnxruntime",
            "scipy",
        )
        windows = os.path.join(ROOT, "build_release.bat")
        macos = os.path.join(ROOT, "build_release_macos.sh")
        with open(windows, encoding="utf-8") as handle:
            win_text = handle.read()
        with open(macos, encoding="utf-8") as handle:
            mac_text = handle.read()
        for name in expected:
            self.assertIn(name, win_text, name)
            self.assertIn(name, mac_text, name)

    def test_macos_declares_microphone_usage(self):
        path = os.path.join(ROOT, "build_release_macos.sh")
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertEqual(text.count("plutil -replace NSMicrophoneUsageDescription"), 1)

    def test_release_requires_and_collects_the_complete_voice_runtime(self):
        windows = os.path.join(ROOT, "build_release.bat")
        macos = os.path.join(ROOT, "build_release_macos.sh")
        with open(windows, encoding="utf-8") as handle:
            win_text = handle.read()
        with open(macos, encoding="utf-8") as handle:
            mac_text = handle.read()
        for text in (win_text, mac_text):
            self.assertIn("VOICE_COLLECT_ARGS", text)
            self.assertIn("import sounddevice, soxr, transcribe_cpp, transcribe_cpp_native", text)
            self.assertIn("--collect-all sounddevice", text)
            self.assertIn("--collect-all soxr", text)
            self.assertIn("--copy-metadata soxr", text)
            self.assertIn("THIRD_PARTY_NOTICES.md", text)
            self.assertIn("--collect-all transcribe_cpp", text)
            self.assertIn("--collect-all transcribe_cpp_native", text)
            self.assertIn("--voice-runtime-probe", text)

    def test_release_bundles_the_application_license(self):
        windows = os.path.join(ROOT, "build_release.bat")
        macos = os.path.join(ROOT, "build_release_macos.sh")
        with open(windows, encoding="utf-8") as handle:
            win_text = handle.read()
        with open(macos, encoding="utf-8") as handle:
            mac_text = handle.read()
        self.assertIn('--add-data "%REPO_DIR%\\LICENSE;."', win_text)
        self.assertIn('--add-data "$REPO_DIR/LICENSE:."', mac_text)

    def test_windows_build_fails_early_when_tcl_tk_cannot_initialize(self):
        path = os.path.join(ROOT, "build_release.bat")
        with open(path, encoding="utf-8") as handle:
            text = handle.read()

        tcl_preflight = 'python -c "import tkinter; tkinter.Tcl()"'
        self.assertIn(tcl_preflight, text)
        self.assertLess(text.index(tcl_preflight), text.index("python -m PyInstaller"))

    def test_runtime_probe_covers_the_desktop_and_voice_imports(self):
        path = os.path.join(ROOT, "source", "voice_runtime_probe.py")
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        for package in ("tkinter", "sounddevice", "soxr", "transcribe_cpp_native"):
            self.assertIn(f"import {package}", text)

    def test_entrypoint_runs_the_probe_before_desktop_imports(self):
        path = os.path.join(ROOT, "source", "snipvoice.pyw")
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        probe_call = "\nrun_voice_runtime_probe_if_requested()\n"
        self.assertLess(text.index(probe_call), text.index("import platform_support"))

    def test_voice_build_dependencies_are_version_pinned(self):
        path = os.path.join(ROOT, "source", "requirements-voice.txt")
        with open(path, encoding="utf-8") as handle:
            requirements = {
                line.strip()
                for line in handle
                if line.strip() and not line.lstrip().startswith("#")
            }
        self.assertEqual(
            requirements,
            {
                "sounddevice==0.5.5",
                "soxr==1.1.0",
                "transcribe-cpp==0.1.3",
                "transcribe-cpp-native==0.1.3",
            },
        )


if __name__ == "__main__":
    unittest.main()
