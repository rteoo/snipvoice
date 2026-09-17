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

from meeting_files import (
    IMPORT_FRAMES,
    export_highlight_clip,
    export_meeting,
    export_report,
    import_audio,
    import_wav,
    play_audio,
)
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

    def test_compressed_audio_uses_bounded_pyav_frames_and_provenance(self):
        class Layout:
            name = "stereo"
            nb_channels = 2

        class Format:
            name = "flt"

        class Frame:
            sample_rate = 48000
            layout = Layout()
            format = Format()

            def __init__(self, samples, value):
                self.samples = samples
                self.planes = (struct.pack("<" + "f" * samples * 2, *([value] * samples * 2)),)

        class Streams:
            @staticmethod
            def best(kind):
                return object() if kind == "audio" else None

        class Container:
            streams = Streams()

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            @staticmethod
            def decode(_stream):
                return iter((Frame(IMPORT_FRAMES, 0.25), Frame(3, -0.5)))

        class Resampler:
            def __init__(self, **options):
                self.options = options

            @staticmethod
            def resample(frame):
                return [] if frame is None else [frame]

        fake_av = types.SimpleNamespace(open=lambda *_args, **_kwargs: Container(), AudioResampler=Resampler)
        path = self.root / "reunião.m4a"
        path.write_bytes(b"fake container")
        with mock.patch.dict(sys.modules, {"av": fake_av}):
            sid = import_audio(self.store, path, {})

        chunks = list(self.store.iter_audio(sid))
        self.assertEqual([event["frames"] for event, _ in chunks], [IMPORT_FRAMES, 3])
        self.assertEqual(chunks[1][0]["timestamp"], IMPORT_FRAMES / 48000)
        self.assertAlmostEqual(struct.unpack_from("<f", chunks[0][1])[0], 0.25)
        metadata = self.store.get(sid)
        self.assertEqual(metadata["status"], "completed")
        imported = next(event for event in self.store.iter_events(sid) if event["type"] == "imported_audio")
        self.assertEqual((imported["filename"], imported["source_format"], imported["decoder"]),
                         ("reunião.m4a", "m4a", "PyAV"))

    def test_unsupported_audio_is_rejected_before_session_creation(self):
        path = self.root / "input.wma"
        path.write_bytes(b"unsupported")
        with self.assertRaisesRegex(ValueError, "WAV, MP3, AAC/M4A, FLAC, OGG ou Opus"):
            import_audio(self.store, path, {})
        self.assertEqual(self.store.list_sessions(), [])

    def test_cancelled_compressed_import_preserves_completed_prefix(self):
        path = self.root / "input.mp3"
        path.write_bytes(b"fake container")
        cancellation = threading.Event()
        chunk = (48000, 1, struct.pack("<" + "f" * IMPORT_FRAMES, *([0.25] * IMPORT_FRAMES)))
        append = self.store.append_audio

        def cancel_after_first(*arguments):
            append(*arguments)
            cancellation.set()

        with mock.patch("meeting_files._pyav_chunks", return_value=iter((chunk, chunk))), \
                mock.patch.object(self.store, "append_audio", side_effect=cancel_after_first):
            with self.assertRaisesRegex(RuntimeError, "cancelada"):
                import_audio(self.store, path, {}, cancellation)
        item = self.store.list_sessions()[0]
        self.assertEqual(item["status"], "cancelled")
        self.assertEqual(len(list(self.store.iter_audio(item["id"]))), 1)

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

    def test_json_export_does_not_disclose_local_destination_paths(self):
        private_destination = str(self.root / "private-client-folder")
        sid = self.store.begin({"meeting_destination": private_destination})
        self.store.finish(sid)
        final_audio = self.root / "final.wav"
        final_audio.write_bytes(b"RIFF")
        self.store.save_final_audio(sid, final_audio)
        exported = self.root / "export.json"

        export_meeting(self.store, sid, exported, "json")

        document = json.loads(exported.read_text(encoding="utf-8"))
        self.assertNotIn("meeting_destination", document["metadata"]["settings"])
        self.assertEqual(document["metadata"]["final_audio"]["path"], "final.wav")
        self.assertNotIn(str(self.root), exported.read_text(encoding="utf-8"))

    def test_report_export_is_atomic_bounded_and_path_free(self):
        destination = self.root / "report.md"
        report = {
            "id": "report-1", "kind": "report", "profile_id": "general",
            "profile_version": 1, "session_id": "session-1",
            "transcript_revision": "revision-1",
            "model": {"id": "local", "sha256": "a" * 64, "runtime": "llama.cpp",
                      "path": str(self.root / "secret.gguf")},
            "generated": {"summary": {"text": "Confirmed", "citations": ["s1"]}},
            "created_at": "2026-09-16T12:00:00Z",
        }
        self.assertEqual(export_report(report, destination, section="summary"), str(destination))
        content = destination.read_text(encoding="utf-8")
        self.assertIn("Confirmed", content)
        self.assertNotIn(str(self.root), content)
        self.assertEqual(list(self.root.glob(".report.md-*.tmp")), [])

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

    def test_highlight_clip_trims_exclusive_end_and_preserves_gaps(self):
        sid = self.store.begin({}, "Clip")
        self.audio(sid, [0.1, 0.2, 0.3, 0.4])
        self.audio(sid, [0.5, 0.6, 0.7], timestamp=0.001, sequence=1)
        self.store.finish(sid)
        destination = self.root / "clip.wav"
        highlight = {
            "id": "highlight-1",
            "revision": "revision-1",
            "start": 2 / 8000,
            "end": 10 / 8000,
            "track": "microphone",
            "label": "Decision",
            "note": "Keep the source timing.",
            "segment_ids": ["segment-1"],
            "private_path": str(self.root / "must-not-leak"),
        }

        self.assertEqual(export_highlight_clip(self.store, sid, highlight, destination), str(destination))
        with wave.open(str(destination), "rb") as reader:
            self.assertEqual((reader.getframerate(), reader.getnchannels(), reader.getsampwidth()), (8000, 1, 2))
            samples = struct.unpack("<8h", reader.readframes(20))
        expected = tuple(round(value * 32767) for value in (0.3, 0.4, 0.0, 0.0, 0.0, 0.0, 0.5, 0.6))
        self.assertEqual(samples, expected)
        payload = destination.read_bytes()
        self.assertIn(b"highlight-1", payload)
        self.assertIn(b'"start":0.00025', payload)
        self.assertIn(b'"end":0.00125', payload)
        self.assertIn(b'"gap":true', payload)
        self.assertIn(b'"gaps":[', payload)
        self.assertNotIn(str(self.root).encode(), payload)

    def test_highlight_clip_does_not_overwrite_or_leave_temp_files(self):
        sid = self.session()
        destination = self.root / "clip.wav"
        destination.write_bytes(b"previous")

        with self.assertRaises(FileExistsError):
            export_highlight_clip(
                self.store,
                sid,
                {"id": "h", "start": 0, "end": 1 / 8000, "track": "microphone"},
                destination,
            )
        self.assertEqual(destination.read_bytes(), b"previous")
        self.assertEqual(list(self.root.glob(".clip.wav-*.tmp")), [])

    def test_cancelled_highlight_clip_preserves_destination_and_cleans_temp(self):
        sid = self.session()
        cancellation = threading.Event()
        destination = self.root / "clip.wav"
        original = self.store.iter_audio

        def cancel_after_first(*arguments):
            iterator = original(*arguments)
            for item in iterator:
                cancellation.set()
                yield item

        with mock.patch.object(self.store, "iter_audio", side_effect=cancel_after_first):
            with self.assertRaisesRegex(RuntimeError, "cancelada"):
                export_highlight_clip(
                    self.store,
                    sid,
                    {"id": "h", "start": 0, "end": 1 / 8000, "track": "microphone"},
                    destination,
                    cancel_event=cancellation,
                )
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.root.glob(".clip.wav-*.tmp")), [])

    def test_highlight_clip_rejects_format_changes_and_preserves_destination(self):
        sid = self.store.begin({})
        self.audio(sid, [0.1] * 4)
        self.audio(sid, [0.2] * 4, timestamp=0.0005, sequence=1, rate=16000)
        self.store.finish(sid)
        destination = self.root / "clip.wav"
        with self.assertRaisesRegex(ValueError, "formato"):
            export_highlight_clip(
                self.store,
                sid,
                {"id": "h", "start": 0, "end": 0.001, "track": "microphone"},
                destination,
            )
        self.assertFalse(destination.exists())

    def test_highlight_clip_enforces_riff_limit_before_commit(self):
        sid = self.session()
        destination = self.root / "clip.wav"
        with mock.patch("meeting_files.RIFF_LIMIT", 256):
            with self.assertRaisesRegex(ValueError, "4 GiB"):
                export_highlight_clip(
                    self.store,
                    sid,
                    {"id": "h", "start": 0, "end": 4 / 8000, "track": "microphone"},
                    destination,
                )
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.root.glob(".clip.wav-*.tmp")), [])

    def test_highlight_clip_rejects_empty_or_non_finite_ranges(self):
        sid = self.session()
        for start, end in ((0, 0), (float("nan"), 1), (0, float("inf"))):
            with self.subTest(start=start, end=end):
                with self.assertRaisesRegex(ValueError, "intervalo"):
                    export_highlight_clip(
                        self.store,
                        sid,
                        {"id": "h", "start": start, "end": end, "track": "microphone"},
                        self.root / ("clip-" + str(len(str(start))) + ".wav"),
                    )


if __name__ == "__main__":
    unittest.main()
