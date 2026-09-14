"""Non-recording protocol checks for an explicitly built Windows helper."""

import json
from pathlib import Path
import struct
import subprocess
import sys
import unittest


HELPER = Path(__file__).resolve().parents[1] / "native" / "bin" / "snipvoice-capture.exe"


@unittest.skipUnless(sys.platform == "win32" and HELPER.is_file(), "Windows helper has not been built")
class WindowsCaptureTests(unittest.TestCase):
    def run_helper(self, *args):
        return subprocess.run(
            [str(HELPER), *args],
            input=b"",
            capture_output=True,
            timeout=5,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

    def test_self_test_frames_and_native_conversion_checks(self):
        result = self.run_helper("--self-test")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, b"")
        frames = []
        offset = 0
        while offset < len(result.stdout):
            self.assertGreaterEqual(len(result.stdout) - offset, 4)
            length = struct.unpack_from("<I", result.stdout, offset)[0]
            self.assertGreater(length, 0)
            self.assertLessEqual(length, 65536)
            offset += 4
            header = json.loads(result.stdout[offset : offset + length].decode("utf-8"))
            offset += length
            payload = struct.unpack_from("<I", result.stdout, offset)[0]
            offset += 4
            self.assertEqual(payload, 0, "Self-test must not capture audio")
            frames.append(header)
        self.assertEqual(offset, len(result.stdout))
        self.assertEqual(
            frames,
            [
                {"type": "ready", "version": 1, "generation": 0},
                {"type": "stopped", "generation": 0},
            ],
        )

    def test_invalid_arguments_fail_without_starting_capture(self):
        for args in (
            (),
            ("--unknown",),
            ("--self-test", "--list"),
            ("--capture", "--sources", "invalid"),
            ("--capture", "--generation", "-1"),
            ("--capture", "--generation", "1extra"),
            ("--capture", "--generation"),
        ):
            with self.subTest(args=args):
                result = self.run_helper(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, b"")
                self.assertIn(b"Snipvoice Windows capture:", result.stderr)


if __name__ == "__main__":
    unittest.main()
