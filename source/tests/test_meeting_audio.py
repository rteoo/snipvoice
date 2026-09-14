import io
import os
import struct
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from meeting_audio import MAX_HEADER, MAX_PAYLOAD, MeetingAudioError, NativeCapture, encode_frame, read_frame
from meeting_settings import MeetingSettings


class FragmentedReader(io.BytesIO):
    def read(self, size=-1):
        return super().read(min(3, size) if size >= 0 else 3)


class MeetingAudioTests(unittest.TestCase):
    def event(self, **values):
        result = {"type": "audio", "generation": 2, "track": "system", "rate": 48000,
                  "channels": 2, "frames": 2, "timestamp": 0.25, "sequence": 1}
        result.update(values)
        return result

    def test_fragmented_audio_round_trip(self):
        payload = struct.pack("<4f", 0.25, 0.5, -0.25, -0.5)
        event, actual = read_frame(FragmentedReader(encode_frame(self.event(), payload)))
        self.assertEqual(event, self.event())
        self.assertEqual(actual, payload)

    def test_invalid_formats_and_clocks_are_rejected(self):
        for values in ({"rate": True}, {"channels": 9}, {"frames": 0},
                       {"timestamp": -1}, {"sequence": -1}, {"track": "other"}):
            with self.subTest(values=values), self.assertRaises(MeetingAudioError):
                read_frame(io.BytesIO(encode_frame(self.event(**values), b"\0" * 16)))

    def test_control_event_cannot_hide_audio(self):
        with self.assertRaises(MeetingAudioError):
            read_frame(io.BytesIO(encode_frame({"type": "ready"}, b"audio")))

    def test_oversized_header_rejected_before_allocation(self):
        with self.assertRaises(MeetingAudioError):
            read_frame(io.BytesIO(struct.pack("<I", MAX_HEADER + 1)))

    def test_oversized_payload_rejected_before_allocation(self):
        base = encode_frame({"type": "ready"})[:-4]
        with self.assertRaises(MeetingAudioError):
            read_frame(io.BytesIO(base + struct.pack("<I", MAX_PAYLOAD + 1)))

    def test_truncated_payload_is_not_a_successful_take(self):
        with self.assertRaises(MeetingAudioError):
            read_frame(io.BytesIO(encode_frame(self.event(), b"\0" * 16)[:-2]))

    def fake_helper(self, body):
        script = ("import json,struct,sys,time\n"
                  "def emit(event):\n"
                  " data=json.dumps(event).encode(); sys.stdout.buffer.write(struct.pack('<I',len(data))+data+struct.pack('<I',0)); sys.stdout.buffer.flush()\n" + body)
        return NativeCapture(helper_path=sys.executable, popen=lambda argv, **options:
                             subprocess.Popen([sys.executable, "-u", "-c", script], **options))

    @unittest.skipUnless(sys.platform in ("win32", "darwin"), "supported capture host required")
    def test_stderr_flood_is_drained_and_stop_is_reaped(self):
        capture = self.fake_helper("sys.stderr.buffer.write(b'x'*1048576); sys.stderr.buffer.flush()\n"
                                   "emit({'type':'ready','version':1,'generation':2})\n"
                                   "sys.stdin.readline()\n"
                                   "emit({'type':'stopped','generation':2})\n")
        capture.start(MeetingSettings(), 2)
        capture.command("stop")
        event, _ = capture.read_event(timeout=2)
        self.assertEqual(event["type"], "stopped")
        capture.stop()
        self.assertIsNotNone(capture._process.poll())
        self.assertFalse(capture._reader.is_alive())

    @unittest.skipUnless(sys.platform in ("win32", "darwin"), "supported capture host required")
    def test_old_generation_is_rejected_and_process_is_reaped(self):
        capture = self.fake_helper("emit({'type':'ready','version':1,'generation':1})\ntime.sleep(10)\n")
        with self.assertRaises(MeetingAudioError):
            capture.start(MeetingSettings(), 2)
        self.assertIsNotNone(capture._process.poll())

    @unittest.skipUnless(sys.platform in ("win32", "darwin"), "supported capture host required")
    def test_unexpected_eof_is_not_a_successful_stop(self):
        capture = self.fake_helper("emit({'type':'ready','version':1,'generation':2})\ntime.sleep(.1)\n")
        capture.start(MeetingSettings(), 2)
        try:
            with self.assertRaises(MeetingAudioError):
                capture.read_event(timeout=1)
        finally:
            capture.stop(force=True)
