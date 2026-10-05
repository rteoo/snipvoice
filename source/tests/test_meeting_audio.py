import io
import hashlib
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from meeting_audio import MAX_HEADER, MAX_PAYLOAD, MeetingAudioError, NativeCapture, encode_frame, read_frame
from meeting_settings import MeetingSettings
from meeting_store import MeetingStore


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
                  "def emit(event, payload=b''):\n"
                  " data=json.dumps(event).encode(); sys.stdout.buffer.write(struct.pack('<I',len(data))+data+struct.pack('<I',len(payload))+payload); sys.stdout.buffer.flush()\n" + body)
        return NativeCapture(helper_path=sys.executable, popen=lambda argv, **options:
                             subprocess.Popen([sys.executable, "-u", "-c", script], **options))

    @unittest.skipUnless(sys.platform in ("win32", "darwin"), "supported capture host required")
    def test_stop_after_helper_exit_preserves_buffered_audio_and_terminal_errors(self):
        for exit_code, terminal in ((0, True), (3, True), (1, False)):
            with self.subTest(exit_code=exit_code, terminal=terminal):
                ending = ("emit({'type':'stopped','generation':2,'reason':'transport_overflow'})\n"
                          if exit_code == 3 else "emit({'type':'stopped','generation':2})\n") if terminal else ""
                capture = self.fake_helper(
                    "emit({'type':'ready','version':1,'generation':2})\n"
                    "sys.stdin.readline()\n"
                    "emit({'type':'audio','generation':2,'track':'system','rate':16000,"
                    "'channels':1,'frames':2,'timestamp':0,'sequence':0},struct.pack('<2f',.25,-.5))\n"
                    + ending + f"sys.exit({exit_code})\n")
                try:
                    capture.start(MeetingSettings(), 2)
                    capture._process.stdin.write(b"exit\n")
                    capture._process.stdin.flush()
                    capture._process.wait(timeout=3)
                    self.assertTrue(capture._done.wait(2))
                    capture.command("stop")
                    event, payload = capture.read_event(timeout=2)
                    self.assertEqual(event["type"], "audio")
                    self.assertEqual(payload, struct.pack("<2f", .25, -.5))
                    if terminal:
                        self.assertEqual(capture.read_event(timeout=2)[0]["type"], "stopped")
                    else:
                        with self.assertRaises(MeetingAudioError):
                            capture.read_event(timeout=.01)
                    if exit_code:
                        with self.assertRaises(MeetingAudioError):
                            capture.stop()
                    else:
                        capture.stop()
                finally:
                    capture.stop(force=True)

    def test_stop_tolerates_exit_during_pipe_write_but_reports_a_live_control_failure(self):
        capture = NativeCapture()
        process = mock.Mock()
        capture._process = process
        process.stdin.write.side_effect = BrokenPipeError("closed fixture pipe")
        process.poll.side_effect = [None, 0]
        capture.command("stop")
        process.poll.side_effect = None
        process.poll.return_value = None
        with self.assertRaises(MeetingAudioError):
            capture.command("stop")
        process.poll.return_value = 0
        with self.assertRaises(MeetingAudioError):
            capture.command("pause")

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
    def test_missing_or_boolean_generation_is_rejected(self):
        for generation in (None, True):
            value = "{'type':'ready','version':1}" if generation is None else "{'type':'ready','version':1,'generation':True}"
            capture = self.fake_helper("emit(" + value + ")\ntime.sleep(10)\n")
            with self.subTest(generation=generation), self.assertRaises(MeetingAudioError):
                capture.start(MeetingSettings(), 1)
            self.assertIsNotNone(capture._process.poll())

    @unittest.skipUnless(sys.platform in ("win32", "darwin"), "supported capture host required")
    def test_terminal_frame_with_error_exit_is_not_completed(self):
        capture = self.fake_helper("emit({'type':'ready','version':1,'generation':2})\n"
                                   "sys.stdin.readline()\nemit({'type':'stopped','generation':2})\nsys.exit(3)\n")
        capture.start(MeetingSettings(), 2)
        capture.command("stop")
        capture.read_event(timeout=2)
        with self.assertRaises(MeetingAudioError) as caught:
            capture.stop()
        self.assertFalse(caught.exception.resource_live)
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

    @unittest.skipUnless(sys.platform in ("win32", "darwin"), "supported capture host required")
    def test_temporary_consumer_stall_preserves_all_events_and_stop(self):
        capture = self.fake_helper("emit({'type':'ready','version':1,'generation':2})\n"
                                   "for i in range(200): emit({'type':'gap','generation':2,'timestamp':i,'reason':'test'})\n"
                                   "sys.stdin.readline()\nemit({'type':'stopped','generation':2})\n")
        capture.start(MeetingSettings(), 2)
        try:
            time.sleep(0.6)  # Exceeds the former 250 ms queue-full failure deadline.
            stamps = []
            for _ in range(200):
                event, _ = capture.read_event(timeout=2)
                stamps.append(event["timestamp"])
            self.assertEqual(stamps, list(range(200)))
            capture.command("stop")
            self.assertEqual(capture.read_event(timeout=2)[0]["type"], "stopped")
            capture.stop()
            self.assertFalse(capture._reader.is_alive())
            self.assertFalse(capture._diagnostics.is_alive())
        finally:
            capture.stop(force=True)

    @unittest.skipUnless(sys.platform in ("win32", "darwin"), "supported capture host required")
    def test_persistent_stall_fails_visibly_and_force_stop_cancels_backpressure(self):
        capture = self.fake_helper("emit({'type':'ready','version':1,'generation':2})\n"
                                   "for i in range(200): emit({'type':'gap','generation':2,'timestamp':i})\n"
                                   "time.sleep(20)\n")
        with mock.patch("meeting_audio.QUEUE_STALL_SECONDS", 0.2):
            capture.start(MeetingSettings(), 2)
            try:
                self.assertTrue(capture._done.wait(2))
                self.assertIn("disco", str(capture._failure))
            finally:
                capture.stop(force=True)
        self.assertFalse(capture._reader.is_alive())
        self.assertIsNotNone(capture._process.poll())

    @unittest.skipUnless(sys.platform in ("win32", "darwin"), "supported capture host required")
    def test_native_overflow_retains_actionable_stop_reason(self):
        capture = self.fake_helper("emit({'type':'ready','version':1,'generation':2})\n"
                                   "sys.stdin.readline()\n"
                                   "emit({'type':'stopped','generation':2,'reason':'transport_overflow'})\nsys.exit(3)\n")
        capture.start(MeetingSettings(), 2)
        try:
            capture.command("stop")
            capture.read_event(timeout=2)
            with self.assertRaisesRegex(MeetingAudioError, "disco"):
                capture.stop()
        finally:
            capture.stop(force=True)

    @unittest.skipUnless(sys.platform in ("win32", "darwin"), "supported capture host required")
    def test_force_stop_unblocks_reader_with_full_queue(self):
        capture = self.fake_helper("emit({'type':'ready','version':1,'generation':2})\n"
                                   "for i in range(200): emit({'type':'gap','generation':2,'timestamp':i})\n"
                                   "time.sleep(20)\n")
        capture.start(MeetingSettings(), 2)
        try:
            time.sleep(0.2)
            capture.stop(force=True)
            self.assertFalse(capture._reader.is_alive())
            self.assertIsNotNone(capture._process.poll())
        finally:
            capture.stop(force=True)

    @unittest.skipUnless(sys.platform in ("win32", "darwin"), "supported capture host required")
    def test_dual_source_burst_with_stalls_has_exact_durable_pcm_readback(self):
        blocks = 600
        capture = self.fake_helper(
            "emit({'type':'ready','version':1,'generation':2})\n"
            f"for sequence in range({blocks}):\n"
            " for track,rate,channels in [('microphone',16000,1),('system',48000,2)]:\n"
            "  frames=rate//50; value=(sequence%13)/16; payload=struct.pack('<'+str(frames*channels)+'f',*([value]*(frames*channels)))\n"
            "  emit({'type':'audio','generation':2,'track':track,'rate':rate,'channels':channels,'frames':frames,'timestamp':sequence/50,'sequence':sequence},payload)\n"
            "sys.stdin.readline()\n"
            "emit({'type':'stopped','generation':2})\n"
        )
        root = Path(__file__).parent / "tmp"
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as directory:
            store = MeetingStore(directory)
            session = store.begin(MeetingSettings().payload(), "Synthetic transport stress")
            expected = {track: hashlib.sha256() for track in ("microphone", "system")}
            counts = {track: 0 for track in expected}
            try:
                capture.start(MeetingSettings(), 2)
                for index in range(blocks * 2):
                    if index % 300 == 0:
                        time.sleep(0.3)
                    event, payload = capture.read_event(timeout=3)
                    track = event["track"]
                    self.assertEqual(event["sequence"], counts[track])
                    self.assertEqual(event["generation"], 2)
                    value = (counts[track] % 13) / 16
                    reference = struct.pack("<" + str(event["frames"] * event["channels"]) + "f",
                                            *([value] * (event["frames"] * event["channels"])))
                    self.assertEqual(payload, reference)
                    store.append_audio(session, event, payload)
                    expected[track].update(reference)
                    counts[track] += 1
                capture.command("stop")
                self.assertEqual(capture.read_event(timeout=3)[0]["type"], "stopped")
                capture.stop()
                store.finish(session)
                actual = {track: hashlib.sha256() for track in expected}
                persisted = {track: 0 for track in expected}
                for event, payload in store.iter_audio(session):
                    track = event["track"]
                    self.assertEqual(event["sequence"], persisted[track])
                    actual[track].update(payload)
                    persisted[track] += 1
                self.assertEqual(persisted, {"microphone": blocks, "system": blocks})
                self.assertEqual({track: value.hexdigest() for track, value in actual.items()},
                                 {track: value.hexdigest() for track, value in expected.items()})
                self.assertFalse(capture._reader.is_alive())
                self.assertFalse(capture._diagnostics.is_alive())
            finally:
                capture.stop(force=True)
