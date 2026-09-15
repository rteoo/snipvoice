import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock
import wave

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from meeting_files import IMPORT_FRAMES, export_meeting, import_wav, play_audio
from meeting_store import MeetingStore


class MeetingFilesTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).resolve().parent / "tmp"
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = MeetingStore(self.root / "meetings")

    def wav(self, width=2, frames=4):
        path = self.root / "input.wav"
        with wave.open(str(path), "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(width)
            writer.setframerate(8000)
            writer.writeframes(b"\0" * (width * frames))
        return path

    def session(self):
        sid = self.store.begin({}, "Test")
        self.audio(sid, [0.1, 0.2, 0.3, 0.4])
        self.store.add_event(sid, {"type": "gap", "timestamp": 0.0005, "track": "microphone", "reason": "paused"})
        self.store.finish(sid)
        self.store.update(sid, notes="Keep these notes", bookmarks=[{"timestamp": 0.0001, "text": "Bookmark"}])
        revision = self.store.begin_revision(sid, "test-model", "pt")
        self.store.add_transcript(sid, revision, {"id": "s1", "track": "microphone", "start": 0, "end": 0.0005, "text": "Evidence"})
        self.store.finish_revision(sid, revision)
        return sid

    def audio(self, sid, values, timestamp=0, sequence=0, rate=8000, channels=1):
        self.store.append_audio(sid, {"type": "audio", "generation": 0, "track": "microphone",
                                      "sequence": sequence, "timestamp": timestamp, "rate": rate,
                                      "channels": channels, "frames": len(values) // channels},
                                struct.pack("<" + "f" * len(values), *values))

    def test_import_supported_widths_and_native_frame_timestamps(self):
        for width in (1, 2, 3, 4):
            with self.subTest(width=width):
                sid = import_wav(self.store, self.wav(width), {})
                event, payload = next(self.store.iter_audio(sid))
                self.assertEqual((event["rate"], event["channels"], event["timestamp"], event["generation"]), (8000, 1, 0, 0))
                self.assertEqual(struct.unpack("<4f", payload), (-1.0,) * 4 if width == 1 else (0.0,) * 4)
                self.assertEqual(self.store.get(sid)["status"], "completed")

    def test_import_rotates_bounded_chunks(self):
        sid = import_wav(self.store, self.wav(frames=IMPORT_FRAMES + 3), {})
        chunks = list(self.store.iter_audio(sid))
        self.assertEqual([event["frames"] for event, _ in chunks], [IMPORT_FRAMES, 3])
        self.assertEqual(chunks[1][0]["timestamp"], IMPORT_FRAMES / 8000)
        self.assertEqual(chunks[1][0]["sequence"], 1)

    def test_failed_import_preserves_saved_prefix(self):
        append = self.store.append_audio
        calls = 0
        def fail_second(*arguments):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("disk full")
            return append(*arguments)
        with mock.patch.object(self.store, "append_audio", side_effect=fail_second):
            with self.assertRaises(OSError):
                import_wav(self.store, self.wav(frames=IMPORT_FRAMES + 3), {})
        item = self.store.list_sessions()[0]
        self.assertEqual(item["status"], "failed")
        self.assertEqual(len(list(self.store.iter_audio(item["id"]))), 1)

    def test_truncated_and_compressed_wav_fail_before_session_creation(self):
        path = self.wav()
        original = path.read_bytes()
        for corrupted in (original[:-1], original[:20] + b"\3\0" + original[22:], b"not WAV"):
            path.write_bytes(corrupted)
            with self.assertRaises(ValueError):
                import_wav(self.store, path, {})
        self.assertEqual(self.store.list_sessions(), [])

    def test_json_and_text_include_revision_notes_bookmarks_and_gaps(self):
        sid = self.session()
        for format in ("json", "plain", "markdown"):
            path = self.root / ("export." + format)
            self.assertEqual(export_meeting(self.store, sid, path, format), str(path))
            text = path.read_text(encoding="utf-8")
            for expected in ("Keep these notes", "Bookmark", "paused", "Evidence", "test-model", "microphone"):
                self.assertIn(expected, text)
            if format == "json":
                self.assertEqual(json.loads(text)["transcripts"][0]["segments"][0]["id"], "s1")

    def test_export_failure_keeps_previous_destination(self):
        sid = self.session()
        path = self.root / "export.md"
        path.write_bytes(b"prior bytes")
        with mock.patch.object(self.store, "get_transcript", side_effect=OSError("read failed")):
            with self.assertRaises(OSError):
                export_meeting(self.store, sid, path)
        self.assertEqual(path.read_bytes(), b"prior bytes")
        self.assertEqual(list(self.root.glob(".export.md-*.tmp")), [])

    def test_wav_export_has_playable_pcm_and_provenance(self):
        sid = self.session()
        path = self.root / "export.wav"
        export_meeting(self.store, sid, path, "wav-microphone")
        with wave.open(str(path), "rb") as reader:
            self.assertEqual((reader.getframerate(), reader.getnchannels(), reader.getsampwidth(), reader.getnframes()), (8000, 1, 2, 4))
        self.assertIn(b"svpr", path.read_bytes())
        self.assertIn(b"Keep these notes", path.read_bytes())

    def test_wav_format_change_and_internal_destination_are_rejected(self):
        sid = self.store.begin({})
        self.audio(sid, [0.1] * 4)
        self.audio(sid, [0.2] * 4, timestamp=0.0005, sequence=1, rate=16000)
        self.store.finish(sid)
        path = self.root / "export.wav"
        path.write_bytes(b"keep")
        with self.assertRaises(ValueError):
            export_meeting(self.store, sid, path, "wav-microphone")
        self.assertEqual(path.read_bytes(), b"keep")
        with self.assertRaises(ValueError):
            export_meeting(self.store, sid, Path(self.store.root) / sid / "metadata.json", "json")

    def test_playback_seek_and_gap_silence(self):
        sid = self.store.begin({})
        self.audio(sid, [0.1, 0.2, 0.3, 0.4])
        self.audio(sid, [0.5, 0.6], timestamp=0.001, sequence=1)
        self.store.finish(sid)
        writes = []
        stream = mock.Mock()
        stream.write.side_effect = lambda view: writes.extend(row[0] for row in view.tolist())
        output = mock.Mock(return_value=stream)
        with mock.patch.dict(sys.modules, {"sounddevice": types.SimpleNamespace(OutputStream=output)}):
            play_audio(self.store, sid, "microphone", 2 / 8000, threading.Event())
        self.assertEqual(len(writes), 8)
        self.assertAlmostEqual(writes[0], 0.3)
        self.assertEqual(writes[2:6], [0.0] * 4)
        self.assertAlmostEqual(writes[-1], 0.6)
        output.assert_called_once_with(samplerate=8000, channels=1, dtype="float32", device=None)
        stream.close.assert_called_once()

    def test_cancelled_playback_aborts_stream(self):
        sid = self.session()
        cancellation = threading.Event()
        stream = mock.Mock()
        stream.write.side_effect = lambda _: cancellation.set()
        with mock.patch.dict(sys.modules, {"sounddevice": types.SimpleNamespace(OutputStream=mock.Mock(return_value=stream))}):
            play_audio(self.store, sid, "microphone", 0, cancellation)
        stream.abort.assert_called_once()
        stream.close.assert_called_once()

    def test_cancelled_import_preserves_completed_prefix(self):
        cancellation = threading.Event()
        append = self.store.append_audio
        def cancel_after_first(*arguments):
            append(*arguments)
            cancellation.set()
        with mock.patch.object(self.store, "append_audio", side_effect=cancel_after_first):
            with self.assertRaisesRegex(RuntimeError, "cancelada"):
                import_wav(self.store, self.wav(frames=IMPORT_FRAMES + 3), {}, cancellation)
        item = self.store.list_sessions()[0]
        self.assertEqual(item["status"], "cancelled")
        self.assertEqual(len(list(self.store.iter_audio(item["id"]))), 1)

    def test_cancelled_export_preserves_previous_destination(self):
        sid = self.session()
        cancellation = threading.Event()
        events = self.store.iter_events
        def cancel_while_reading(*arguments):
            for event in events(*arguments):
                cancellation.set()
                yield event
        path = self.root / "export.json"
        path.write_bytes(b"previous")
        with mock.patch.object(self.store, "iter_events", side_effect=cancel_while_reading):
            with self.assertRaisesRegex(RuntimeError, "cancelada"):
                export_meeting(self.store, sid, path, "json", cancellation)
        self.assertEqual(path.read_bytes(), b"previous")
        self.assertEqual(list(self.root.glob(".export.json-*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
