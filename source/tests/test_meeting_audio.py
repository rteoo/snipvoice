import io
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from meeting_audio import MAX_HEADER, MAX_PAYLOAD, MeetingAudioError, encode_frame, read_frame


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
