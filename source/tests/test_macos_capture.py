"""Build and exercise the native transport without opening recording streams."""
import json
import platform
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest


def decode_frames(data):
    result = []
    offset = 0
    while offset < len(data):
        if len(data) - offset < 4:
            raise AssertionError("Truncated header length")
        header_size = struct.unpack_from("<I", data, offset)[0]
        offset += 4
        if not 0 < header_size <= 64 * 1024 or offset + header_size + 4 > len(data):
            raise AssertionError("Invalid header size")
        header = json.loads(data[offset:offset + header_size])
        offset += header_size
        payload_size = struct.unpack_from("<I", data, offset)[0]
        offset += 4
        if payload_size > 4 * 1024 * 1024 or offset + payload_size > len(data):
            raise AssertionError("Invalid payload size")
        result.append((header, data[offset:offset + payload_size]))
        offset += payload_size
    return result


@unittest.skipUnless(sys.platform == "darwin", "Requires the real macOS CoreAudio SDK")
class MacOSCaptureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = Path(__file__).resolve().parents[1]
        scratch = source / "tests" / "tmp"
        scratch.mkdir(parents=True, exist_ok=True)
        cls.temporary = tempfile.TemporaryDirectory(prefix="macos-capture-", dir=scratch)
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.helper = Path(cls.temporary.name) / "snipvoice-capture"
        sdk = subprocess.run(
            ["xcrun", "--sdk", "macosx", "--show-sdk-path"],
            check=True, capture_output=True, text=True, timeout=15,
        ).stdout.strip()
        compiler = subprocess.run(
            ["xcrun", "--sdk", "macosx", "--find", "swiftc"],
            check=True, capture_output=True, text=True, timeout=15,
        ).stdout.strip()
        subprocess.run(
            [compiler, "-swift-version", "5", "-sdk", sdk,
             "-target", f"{platform.machine()}-apple-macosx14.4",
             "-framework", "Foundation", "-framework", "CoreAudio",
             "-framework", "AudioToolbox", "-framework", "AVFoundation",
             str(source / "native" / "macos_capture.swift"),
             "-o", str(cls.helper)],
            check=True, capture_output=True, text=True, timeout=120,
        )

    def invoke(self, *arguments):
        return subprocess.run(
            [str(self.helper), *arguments], input=b"", capture_output=True, timeout=10,
        )

    def test_self_test_has_only_ready_and_stopped(self):
        result = self.invoke("--self-test")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(result.stderr, b"")
        self.assertEqual(decode_frames(result.stdout), [
            ({"type": "ready", "version": 1, "generation": 0}, b""),
            ({"type": "stopped", "generation": 0}, b""),
        ])

    def test_list_uses_stable_native_device_ids(self):
        result = self.invoke("--list")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        frames = decode_frames(result.stdout)
        self.assertEqual(len(frames), 1)
        header, payload = frames[0]
        self.assertEqual(header["type"], "devices")
        self.assertEqual(header["version"], 1)
        self.assertEqual(payload, b"")
        unique = set()
        for device in header["devices"]:
            self.assertIsInstance(device["id"], str)
            self.assertTrue(device["id"])
            self.assertIsInstance(device["name"], str)
            self.assertIn(device["kind"], {"microphone", "system"})
            self.assertIsInstance(device["default"], bool)
            self.assertIsInstance(device["communications_default"], bool)
            self.assertEqual(device["default"], device["communications_default"])
            identity = (device["id"], device["kind"])
            self.assertNotIn(identity, unique)
            unique.add(identity)

    def test_bad_arguments_fail_before_starting_capture(self):
        for arguments in [
            ("--capture", "--sources", "invalid", "--generation", "1"),
            ("--capture", "--sources", "both", "--generation", "-1"),
            ("--capture", "--sources", "both", "--sources", "system", "--generation", "1"),
            ("--self-test", "--capture"),
        ]:
            with self.subTest(arguments=arguments):
                result = self.invoke(*arguments)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, b"")
                self.assertIn(b"Snipvoice capture:", result.stderr)


if __name__ == "__main__":
    unittest.main()
