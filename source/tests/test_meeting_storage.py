import hashlib
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from meeting_storage import (CaptureCompactionError, CaptureVerificationError,
                             compact_capture, recover_capture_compaction,
                             verify_final_audio, _session_lock)
from meeting_store import MeetingStore


class FakeStore:
    def __init__(self, root, metadata):
        self.root = Path(root)
        self.session = "fixture-session"
        self.path = self.root / self.session
        self.path.mkdir()
        self.metadata = metadata
        self.commits = []
        self.payloads_complete = True

    def audio_payloads_complete(self, session_id):
        return self.payloads_complete

    def _session_dir(self, session_id):
        if session_id != self.session:
            raise ValueError(session_id)
        return str(self.path)

    def get(self, session_id, include_events=False):
        return json.loads(json.dumps(self.metadata))

    def commit_capture_compaction(self, session_id, tracks, evidence, operation_id):
        self.commits.append((session_id, tracks, evidence, operation_id))
        self.metadata["capture_storage"] = {"format": "mp3", "state": "compacted", **evidence,
                                             "operation_id": operation_id}
        for name in tracks:
            self.metadata.setdefault("tracks", {}).setdefault(name, {}).update(
                {"available": False, "raw_removed": True})


def _decoder_factory(chunks):
    def decoder(metadata, start, cancel_event):
        yield from chunks
    return decoder


class MeetingStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="tests/tmp")
        self.root = Path(self.temp.name)
        self.mp3 = self.root / "final.mp3"
        self.mp3.write_bytes(b"synthetic mp3")
        self.metadata = {
            "status": "completed",
            "duration": 2.0,
            "final_audio": {"path": str(self.mp3)},
            "tracks": {"system": {"segments": [{"start": 0.0, "end": 2.0, "duration": 2.0}]}},
        }
        self.store = FakeStore(self.root, self.metadata)
        self.payload = struct.pack(
            "<16f", 0.1, 0.0, -0.2, 0.0, 0.3, 0.0, 0.0, 0.0,
            0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_verify_decodes_to_eof_and_records_hash(self):
        with mock.patch("meeting_storage._final_audio_chunks", return_value=iter([(4, 2, self.payload)])):
            evidence = verify_final_audio(self.store, self.store.session)
        self.assertEqual(evidence["frames"], 8)
        self.assertEqual(evidence["sha256"], hashlib.sha256(self.mp3.read_bytes()).hexdigest())

    def test_verify_rejects_empty_and_duration_mismatch(self):
        with mock.patch("meeting_storage._final_audio_chunks", return_value=iter(())):
            with self.assertRaises(CaptureVerificationError):
                verify_final_audio(self.store, self.store.session)
        with mock.patch("meeting_storage._final_audio_chunks", return_value=iter([(4, 2, self.payload[:8])])):
            with self.assertRaises(CaptureVerificationError):
                verify_final_audio(self.store, self.store.session)

    def test_changed_final_blocks_compaction(self):
        with mock.patch("meeting_storage._final_audio_chunks", return_value=iter([(4, 2, self.payload)])):
            evidence = verify_final_audio(self.store, self.store.session)
        self.mp3.write_bytes(b"changed")
        with self.assertRaises(CaptureCompactionError):
            compact_capture(self.store, self.store.session, evidence)
        self.assertEqual(self.store.commits, [])

    def test_success_removes_raw_and_keeps_final(self):
        (self.store.path / "microphone").mkdir()
        (self.store.path / "microphone" / "segment.pcm").write_bytes(b"raw")
        with mock.patch("meeting_storage._final_audio_chunks", return_value=iter([(4, 2, self.payload)])):
            evidence = verify_final_audio(self.store, self.store.session)
            result = compact_capture(self.store, self.store.session, evidence)
        self.assertEqual(result["state"], "compacted")
        self.assertTrue(self.mp3.is_file())
        self.assertFalse((self.store.path / "microphone").exists())
        self.assertEqual(len(self.store.commits), 1)

    def test_repeated_compaction_is_noop(self):
        (self.store.path / "system").mkdir()
        (self.store.path / "system" / "segment.pcm").write_bytes(b"raw")
        with mock.patch("meeting_storage._final_audio_chunks", return_value=iter([(4, 2, self.payload)])):
            evidence = verify_final_audio(self.store, self.store.session)
            compact_capture(self.store, self.store.session, evidence)
            compact_capture(self.store, self.store.session, evidence)
        self.assertEqual(len(self.store.commits), 1)

    def test_real_meeting_store_accepts_single_track_compaction(self):
        root = self.root / "real"
        store = MeetingStore(root)
        session = store.begin({"microphone": {"mode": "default"}}, "fixture")
        store.append_audio(session, {"type": "audio", "track": "system", "rate": 4,
                                     "channels": 2, "frames": 8, "timestamp": 0.0,
                                     "sequence": 0, "generation": 0}, self.payload)
        store.finish(session)
        final = root / "final.tmp"
        final.write_bytes(b"synthetic")
        store.save_final_audio(session, final)
        with mock.patch("meeting_storage._final_audio_chunks", return_value=iter([(4, 2, self.payload)])):
            evidence = verify_final_audio(store, session)
            compact_capture(store, session, evidence)
        metadata = store.get(session, include_events=False)
        self.assertEqual(metadata["capture_storage"]["state"], "compacted")
        self.assertFalse((root / session / "system").exists())

    def test_truncated_segment_blocks_compaction_and_is_reported(self):
        root = self.root / "truncated"
        store = MeetingStore(root)
        session = store.begin({"microphone": {"mode": "default"}}, "fixture")
        for index in range(3):
            store.append_audio(session, {"type": "audio", "track": "system", "rate": 8,
                                         "channels": 2, "frames": 8, "timestamp": float(index),
                                         "sequence": index, "generation": 0}, self.payload)
        store.finish(session)
        final = root / "final.tmp"
        final.write_bytes(b"synthetic")
        store.save_final_audio(session, final)
        segment = next((root / session / "system").iterdir())
        segment.write_bytes(segment.read_bytes()[:len(self.payload)])
        missing = []
        self.assertEqual(len(list(store.iter_audio(session, on_missing=missing.append))), 1)
        self.assertEqual([event["sequence"] for event in missing], [1, 2])
        self.assertFalse(store.audio_payloads_complete(session))
        with mock.patch("meeting_storage._final_audio_chunks", return_value=iter([(8, 2, self.payload * 3)])):
            evidence = verify_final_audio(store, session)
        with self.assertRaises(CaptureCompactionError):
            compact_capture(store, session, evidence)
        self.assertTrue(segment.is_file())
        self.assertNotEqual(store.get(session, include_events=False).get("capture_storage", {}).get("state"),
                            "compacted")

    def test_final_mixed_over_unreadable_audio_keeps_originals(self):
        (self.store.path / "system").mkdir()
        (self.store.path / "system" / "segment.pcm").write_bytes(b"raw")
        with mock.patch("meeting_storage._final_audio_chunks", return_value=iter([(4, 2, self.payload)])):
            evidence = verify_final_audio(self.store, self.store.session)
        with self.assertRaises(CaptureCompactionError):
            compact_capture(self.store, self.store.session, {**evidence, "sources_complete": False})
        self.store.payloads_complete = False
        with self.assertRaises(CaptureCompactionError):
            compact_capture(self.store, self.store.session, evidence)
        self.assertTrue((self.store.path / "system" / "segment.pcm").is_file())
        self.assertEqual(self.store.commits, [])

    def test_failure_before_commit_rolls_back(self):
        (self.store.path / "system").mkdir()
        (self.store.path / "system" / "segment.pcm").write_bytes(b"raw")
        with mock.patch("meeting_storage._final_audio_chunks", return_value=iter([(4, 2, self.payload)])):
            evidence = verify_final_audio(self.store, self.store.session)
        with self.assertRaisesRegex(OSError, "boom"):
            compact_capture(self.store, self.store.session, evidence,
                            fault_injector=lambda stage: (_ for _ in ()).throw(OSError("boom"))
                            if stage == "before_commit" else None)
        self.assertTrue((self.store.path / "system" / "segment.pcm").is_file())
        self.assertEqual(self.store.commits, [])

    def test_linked_capture_tree_is_refused(self):
        outside = self.root / "outside"
        outside.mkdir()
        link = self.store.path / "microphone"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        with mock.patch("meeting_storage._final_audio_chunks", return_value=iter([(4, 2, self.payload)])):
            evidence = verify_final_audio(self.store, self.store.session)
        with self.assertRaises(CaptureCompactionError):
            compact_capture(self.store, self.store.session, evidence)

    def test_recovery_rolls_back_staged_tree(self):
        (self.store.path / "microphone").mkdir()
        (self.store.path / "microphone" / "segment.pcm").write_bytes(b"raw")
        with mock.patch("meeting_storage._final_audio_chunks", return_value=iter([(4, 2, self.payload)])):
            evidence = verify_final_audio(self.store, self.store.session)
        with self.assertRaises(OSError):
            compact_capture(self.store, self.store.session, evidence,
                            fault_injector=lambda stage: (_ for _ in ()).throw(OSError("crash"))
                            if stage == "after_stage_microphone" else None)
        result = recover_capture_compaction(self.store, self.store.session)
        self.assertEqual(result["state"], "rolled_back")
        self.assertTrue((self.store.path / "microphone" / "segment.pcm").is_file())

    def test_base_exception_after_rename_is_recoverable(self):
        (self.store.path / "microphone").mkdir()
        (self.store.path / "microphone" / "segment.pcm").write_bytes(b"raw")
        with mock.patch("meeting_storage._final_audio_chunks", return_value=iter([(4, 2, self.payload)])):
            evidence = verify_final_audio(self.store, self.store.session)
        class SimulatedCrash(BaseException):
            pass
        with self.assertRaises(SimulatedCrash):
            compact_capture(self.store, self.store.session, evidence,
                            fault_injector=lambda stage: (_ for _ in ()).throw(SimulatedCrash())
                            if stage == "after_stage_microphone" else None)
        result = recover_capture_compaction(self.store, self.store.session)
        self.assertEqual(result["state"], "rolled_back")
        self.assertTrue((self.store.path / "microphone" / "segment.pcm").is_file())

    def test_recovery_rejects_traversal_journal_without_deleting(self):
        journal = self.store.path / "capture-compaction.json"
        journal.write_text(json.dumps({"session_id": self.store.session,
                                       "operation_id": "../outside",
                                       "state": "staged"}), encoding="utf-8")
        with self.assertRaises(CaptureCompactionError):
            recover_capture_compaction(self.store, self.store.session)

    def test_compaction_lock_rejects_contention(self):
        with _session_lock(str(self.store.path)):
            with self.assertRaises(CaptureCompactionError):
                with _session_lock(str(self.store.path)):
                    pass


if __name__ == "__main__":
    unittest.main()
