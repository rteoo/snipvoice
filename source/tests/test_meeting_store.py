import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from meeting_store import MeetingStore, SEGMENT_SECONDS


class MeetingStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp_root = Path(__file__).resolve().parent / "tmp"
        cls.tmp_root.mkdir(exist_ok=True)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=self.tmp_root)
        self.store = MeetingStore(self.temp.name)
        self.session = self.store.begin({"microphone": {"mode": "default"}}, "Reunião teste")

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def event(track="microphone", timestamp=0.0, sequence=0, frames=4, rate=4, channels=1):
        return {
            "type": "audio",
            "generation": 1,
            "track": track,
            "rate": rate,
            "channels": channels,
            "frames": frames,
            "timestamp": timestamp,
            "sequence": sequence,
        }

    def test_begin_has_versioned_metadata_and_separate_tracks(self):
        item = self.store.get(self.session)
        self.assertEqual(item["schema_version"], 1)
        self.assertEqual(item["status"], "recording")
        self.assertEqual(item["settings"]["microphone"]["mode"], "default")
        self.assertEqual(item["tracks"], {})
        self.assertEqual(item["events"], [])

    def test_audio_round_trip_and_bounded_segment_metadata(self):
        payload = struct.pack("<4f", 0.1, -0.2, 0.3, -0.4)
        self.store.append_audio(self.session, self.event(), payload)
        self.store.append_audio(self.session, self.event(sequence=1, timestamp=1.0), payload)
        values = list(self.store.iter_audio(self.session))
        self.assertEqual([entry[0]["sequence"] for entry in values], [0, 1])
        self.assertEqual([entry[1] for entry in values], [payload, payload])
        track = self.store.get(self.session)["tracks"]["microphone"]
        self.assertEqual(track["segments"][0]["bytes"], len(payload) * 2)
        self.assertEqual(track["segments"][0]["events"], 2)

    def test_tracks_rotate_at_fixed_duration_and_format_change(self):
        payload = b"\0" * (4 * 4)
        self.store.append_audio(self.session, self.event(frames=120, rate=4), payload=b"\0" * 480)
        self.store.append_audio(self.session, self.event(timestamp=30.0, sequence=1, frames=4, rate=4), payload)
        self.store.append_audio(self.session, self.event(timestamp=31.0, sequence=2, rate=8, frames=8), b"\0" * 32)
        segments = self.store.get(self.session)["tracks"]["microphone"]["segments"]
        self.assertEqual(len(segments), 3)
        self.assertEqual(segments[0]["duration"], SEGMENT_SECONDS)
        self.assertEqual(segments[2]["rate"] if "rate" in segments[2] else 8, 8)

    def test_system_track_and_gap_event_are_preserved(self):
        payload = b"\0" * 16
        self.store.append_audio(self.session, self.event(track="system"), payload)
        self.store.add_event(self.session, {"type": "gap", "track": "microphone", "timestamp": 2.0, "duration": 0.5, "sequence": 1, "reason": "device lost"})
        item = self.store.get(self.session)
        self.assertIn("system", item["tracks"])
        self.assertEqual(item["events"][1]["type"], "gap")
        self.assertEqual(item["duration"], 2.5)

    def test_start_offset_filters_audio_without_loading_everything(self):
        payload = b"\0" * 16
        self.store.append_audio(self.session, self.event(sequence=0, timestamp=0), payload)
        self.store.append_audio(self.session, self.event(sequence=1, timestamp=5), payload)
        self.assertEqual([e[0]["timestamp"] for e in self.store.iter_audio(self.session, start=1)], [5])

    def test_crash_recovery_rebuilds_from_valid_journal_prefix(self):
        payload = b"\0" * 16
        self.store.append_audio(self.session, self.event(), payload)
        metadata_path = Path(self.temp.name) / self.session / "metadata.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["events"] = []
        metadata["tracks"] = {}
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        with open(Path(self.temp.name) / self.session / "events.journal", "ab") as handle:
            handle.write(b"SVJ1\x10\x00")
        recovered = MeetingStore(self.temp.name)
        item = recovered.get(self.session)
        self.assertEqual(len(item["events"]), 1)
        self.assertEqual(list(recovered.iter_audio(self.session))[0][1], payload)

    def test_interrupted_recording_is_marked_without_truncating_audio(self):
        payload = b"\0" * 16
        self.store.append_audio(self.session, self.event(), payload)
        path = Path(self.temp.name) / self.session / "microphone" / "segment-000000.pcm"
        path.write_bytes(path.read_bytes() + b"orphan")
        recovered = MeetingStore(self.temp.name)
        self.assertEqual(recovered.get(self.session)["status"], "interrupted")
        self.assertTrue(path.read_bytes().endswith(b"orphan"))

    def test_short_write_and_disk_failure_leave_valid_prefix(self):
        payload = b"\0" * 16
        original = self.store._append_segment

        def short_write(path, data):
            with open(path, "ab", buffering=0) as handle:
                handle.write(data[:3])
            raise OSError("disk full")

        with mock.patch.object(self.store, "_append_segment", side_effect=short_write):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.store.append_audio(self.session, self.event(), payload)
        self.assertEqual(list(self.store.iter_audio(self.session)), [])
        self.assertEqual((Path(self.temp.name) / self.session / "microphone" / "segment-000000.pcm").stat().st_size, 3)
        self.assertIsNotNone(original)

    def test_listing_is_paginated_and_searchable(self):
        self.store.finish(self.session)
        other = self.store.begin({}, "Outra reunião")
        self.store.finish(other)
        self.assertEqual(len(self.store.list_sessions(limit=1)), 1)
        self.assertEqual(self.store.list_sessions(query="outra")[0]["id"], other)
        self.assertEqual(self.store.list_sessions(offset=10), [])

    def test_update_and_transcript_revision_are_additive(self):
        self.store.update(self.session, title="Novo", notes="Pauta", bookmarks=[{"time": 1.0, "label": "início"}])
        revision = self.store.begin_revision(self.session, "balanced", "pt-BR")
        self.store.add_transcript(self.session, revision, {"id": "s1", "start": 0.0, "end": 1.0, "text": "Olá"})
        self.store.finish_revision(self.session, revision)
        item = self.store.get(self.session)
        self.assertEqual(item["title"], "Novo")
        self.assertEqual(list(self.store.get_transcript(self.session, revision))[0]["text"], "Olá")
        self.assertEqual(item["revisions"][0]["status"], "completed")

    def test_event_metadata_tail_is_bounded_and_journal_streams_all(self):
        for sequence in range(1005):
            self.store.add_event(
                self.session,
                {"type": "marker", "track": "microphone", "timestamp": sequence / 10, "sequence": sequence},
            )
        item = self.store.get(self.session)
        self.assertEqual(len(item["events"]), 1000)
        self.assertEqual(len(list(self.store.iter_events(self.session))), 1005)

    def test_summary_checkpoint_failure_keeps_previous_summary(self):
        self.store.save_summary(self.session, {"summary": "anterior", "revision": "r1"})
        with mock.patch("meeting_store.write_json_atomic", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.store.save_summary(self.session, {"summary": "novo", "revision": "r2"})
        self.assertEqual(self.store.get(self.session)["summary"]["summary"], "anterior")

    def test_reviewed_summary_and_notes_survive_regeneration_and_failed_save(self):
        self.store.update(self.session, notes="notes", reviewed_summary="manual review")
        self.store.save_summary(self.session, {"summary": "generated"})
        self.assertEqual(self.store.get(self.session)["reviewed_summary"], "manual review")
        with mock.patch("meeting_store.write_json_atomic", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.store.update(self.session, notes="unsaved")
        self.assertEqual(self.store.get(self.session)["notes"], "notes")

    def test_final_audio_metadata_requires_an_existing_file(self):
        self.store.finish(self.session)
        output = Path(self.temp.name) / "final.wav"
        output.write_bytes(b"RIFF")

        saved = self.store.save_final_audio(self.session, output, voice_boost=True)

        self.assertEqual(saved["path"], str(output.resolve()))
        self.assertTrue(saved["voice_boost"])
        self.assertEqual(self.store.get(self.session)["final_audio"], saved)
        with self.assertRaises(ValueError):
            self.store.save_final_audio(self.session, Path(self.temp.name) / "missing.wav")

    def test_invalid_paths_and_unknown_schema_are_rejected_read_only(self):
        with self.assertRaises(ValueError):
            self.store.get("../outside")
        with self.assertRaises(ValueError):
            self.store.iter_audio(self.session, track="other")
        path = Path(self.temp.name) / self.session / "metadata.json"
        original = path.read_text(encoding="utf-8")
        metadata = json.loads(original)
        metadata["schema_version"] = 999
        path.write_text(json.dumps(metadata), encoding="utf-8")
        with self.assertRaises(ValueError):
            MeetingStore(self.temp.name).get(self.session)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["schema_version"], 999)


if __name__ == "__main__":
    unittest.main()
