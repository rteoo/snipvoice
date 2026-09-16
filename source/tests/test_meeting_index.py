import copy
import os
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from meeting_index import (
    IndexCancelled,
    IndexUnavailable,
    MeetingIndex,
    STATE_INCOMPLETE,
    STATE_READY,
    STATE_UNAVAILABLE,
    fingerprint_canonical,
    _cross_process_lock,
)
from meeting_library import MeetingLibrary
from meeting_store import MeetingStore


FIXTURE = Path(__file__).parent / "fixtures" / "meeting-v1"
TMP_ROOT = Path(__file__).parent / "tmp"


class MeetingIndexTests(unittest.TestCase):
    def setUp(self):
        TMP_ROOT.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TMP_ROOT)
        self.home = Path(self.temp.name)
        self.meetings = self.home / "meetings"
        shutil.copytree(FIXTURE, self.meetings / "fixture-meeting-v1")
        self.store = MeetingStore(self.meetings)
        self.library = MeetingLibrary(self.store, workspace_root=self.home)
        self.metadata = self.store.get("fixture-meeting-v1", include_events=False)
        self.transcripts = {
            "revision-1": list(self.store.get_transcript("fixture-meeting-v1", "revision-1"))
        }
        self.annotations = self.library.read_annotations("fixture-meeting-v1")
        self.entry = (self.metadata, self.annotations, self.transcripts, [])
        self.path = self.home / "library.sqlite"
        self.index = MeetingIndex(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_missing_index_is_explicitly_unavailable_and_canonical_access_survives(self):
        self.assertEqual(self.index.state, STATE_UNAVAILABLE)
        self.assertEqual(self.library.get_session("fixture-meeting-v1")["title"], "Weekly product review")
        self.assertEqual(self.index.list_sessions(), [])

    def test_indexed_projection_lists_and_searches_without_audio_blobs(self):
        self.index.index_session(*self.entry)
        self.assertEqual(self.index.state, STATE_READY)
        self.assertEqual(self.index.list_sessions()[0]["id"], "fixture-meeting-v1")
        result = self.index.search("próximo marco")
        self.assertEqual(result[0]["source_kind"], "transcript")
        self.assertEqual(result[0]["segment_id"], "microphone:0.000000:3.000000")
        connection = __import__("sqlite3").connect(self.path)
        try:
            columns = {
                row[1]: row[2]
                for table in ("sessions", "transcript_segments", "reports", "memberships")
                for row in connection.execute(f"PRAGMA table_info({table})")
            }
        finally:
            connection.close()
        self.assertNotIn("BLOB", {value.upper() for value in columns.values()})

    def test_indexed_filtered_list_qualifies_session_order_columns(self):
        self.index.index_session(*self.entry)
        self.assertEqual(self.index.list_sessions(query="marco")[0]["id"], "fixture-meeting-v1")

    def test_control_fts_input_is_rejected_without_poisoning_ready_state(self):
        self.index.index_session(*self.entry)
        with self.assertRaises(ValueError):
            self.index.search("marco\x00")
        with self.assertRaises(ValueError):
            self.index.list_sessions(query="marco\n\x01")
        self.assertEqual(self.index.state, STATE_READY)

    def test_fingerprint_detects_same_ids_with_changed_transcript_report_and_annotation(self):
        original = fingerprint_canonical(*self.entry[:3], reports=[])
        changed_segment = copy.deepcopy(self.transcripts)
        changed_segment["revision-1"][0]["text"] = "conteúdo diferente"
        self.assertNotEqual(
            original,
            fingerprint_canonical(self.metadata, self.annotations, changed_segment, []),
        )
        changed_annotation = copy.deepcopy(self.annotations)
        changed_annotation["generation"] += 1
        self.assertNotEqual(
            original,
            fingerprint_canonical(self.metadata, changed_annotation, self.transcripts, []),
        )
        changed_report = [{"id": "report-1", "text": "report"}]
        self.assertNotEqual(
            original,
            fingerprint_canonical(self.metadata, self.annotations, self.transcripts, changed_report),
        )

    def test_rebuild_after_database_deletion_converges_to_same_projection(self):
        self.index.index_session(*self.entry)
        before = self.index.search("equipe")
        for suffix in ("", "-wal", "-shm"):
            target = Path(str(self.path) + suffix)
            if target.exists():
                target.unlink()
        rebuilt = MeetingIndex(self.path)
        self.assertEqual(rebuilt.state, STATE_UNAVAILABLE)
        result = rebuilt.rebuild([self.entry])
        self.assertEqual(result["state"], STATE_READY)
        self.assertEqual(rebuilt.search("equipe"), before)

    def test_cancelled_rebuild_keeps_prior_ready_database(self):
        self.index.index_session(*self.entry)
        before = self.index.search("equipe")
        self.index.batch_size = 1
        second = copy.deepcopy(self.metadata)
        second["id"] = "second-meeting"
        second["title"] = "Second"
        cancel = threading.Event()

        def progress(processed, _total):
            if processed:
                cancel.set()

        with self.assertRaises(IndexCancelled):
            self.index.rebuild([self.entry, (second, self.annotations, {}, [])],
                               cancel_event=cancel, progress=progress)
        self.assertEqual(self.index.state, STATE_READY)
        self.assertEqual(self.index.search("equipe"), before)
        self.assertEqual(self.index.search("Second"), [])

    def test_cancelled_first_rebuild_publishes_only_explicit_incomplete_database(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(IndexCancelled):
            self.index.rebuild([self.entry], cancel_event=cancel)
        self.assertEqual(self.index.state, STATE_UNAVAILABLE)
        # An already-cancelled operation does not create a production file.
        # A cancellation after opening the temp replacement is covered by the
        # progress-injected test above; no canonical bundle is ever touched.
        self.assertFalse(self.path.exists())

    def test_cancelled_new_rebuild_publishes_incomplete_state_without_partial_rows(self):
        self.index.batch_size = 1
        second = copy.deepcopy(self.metadata)
        second["id"] = "second-meeting"
        cancel = threading.Event()

        def progress(processed, _total):
            if processed:
                cancel.set()

        with self.assertRaises(IndexCancelled):
            self.index.rebuild([self.entry, (second, self.annotations, {}, [])],
                               cancel_event=cancel, progress=progress)
        self.assertEqual(self.index.state, STATE_INCOMPLETE)
        self.assertTrue(self.path.exists())
        self.assertEqual(self.index.list_sessions(), [])

    def test_corrupt_disposable_database_can_be_rebuilt_without_touching_bundle(self):
        self.path.write_bytes(b"not sqlite")
        corrupt_before = (self.meetings / "fixture-meeting-v1" / "metadata.json").read_bytes()
        broken = MeetingIndex(self.path)
        self.assertEqual(broken.state, STATE_UNAVAILABLE)
        broken.rebuild([self.entry])
        self.assertEqual(broken.state, STATE_READY)
        self.assertEqual((self.meetings / "fixture-meeting-v1" / "metadata.json").read_bytes(), corrupt_before)

    def test_rebuild_removes_old_wal_sidecars_before_swap(self):
        self.index.index_session(*self.entry)
        Path(str(self.path) + "-wal").write_bytes(b"old wal")
        Path(str(self.path) + "-shm").write_bytes(b"old shm")
        self.index.rebuild([self.entry])
        self.assertFalse(Path(str(self.path) + "-wal").exists())
        self.assertFalse(Path(str(self.path) + "-shm").exists())
        self.assertEqual(self.index.search("equipe")[0]["session_id"], "fixture-meeting-v1")

    def test_rebuild_consumes_session_and_transcript_iterators_without_materializing_them(self):
        self.index.batch_size = 1
        consumed = {"sessions": 0, "segments": 0}

        def segment_stream():
            for index in range(3):
                consumed["segments"] += 1
                yield {
                    "id": f"microphone:{index}.000000:{index + 1}.000000",
                    "track": "microphone",
                    "start": float(index),
                    "end": float(index + 1),
                    "text": f"streamed segment {index}",
                }

        def session_stream():
            consumed["sessions"] += 1
            yield (
                self.metadata,
                self.annotations,
                {"revision-1": segment_stream()},
                [],
            )

        progress_observations = []

        def progress(processed, total):
            progress_observations.append((processed, total, consumed["sessions"], consumed["segments"]))

        result = self.index.rebuild(session_stream(), progress=progress)
        self.assertEqual(result["sessions"], 1)
        self.assertEqual(consumed, {"sessions": 1, "segments": 3})
        self.assertEqual(progress_observations[-1][1], 1)
        self.assertEqual(self.index.search("streamed segment 2")[0]["session_id"], "fixture-meeting-v1")

    def test_active_report_follows_canonical_creation_order_not_uuid_sorting(self):
        reports = [
            {"id": "z-older", "kind": "report", "profile_id": "general", "text": "old"},
            {"id": "a-newer", "kind": "report", "profile_id": "general", "text": "new"},
        ]
        self.index.index_session(
            self.metadata, self.annotations, self.transcripts, reports,
        )
        connection = __import__("sqlite3").connect(self.path)
        try:
            active = connection.execute(
                "SELECT active_report FROM sessions WHERE session_id=?",
                ("fixture-meeting-v1",),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(active, "a-newer")

    @unittest.skipUnless(os.name == "nt", "Windows-specific msvcrt lock behavior")
    def test_windows_writer_lock_times_out_with_actionable_error(self):
        import msvcrt

        with mock.patch.object(__import__("meeting_index"), "LOCK_TIMEOUT_SECONDS", 0.01), \
                mock.patch.object(__import__("meeting_index"), "LOCK_POLL_SECONDS", 0), \
                mock.patch.object(msvcrt, "locking", side_effect=OSError("busy")):
            with self.assertRaises(IndexUnavailable) as context:
                with _cross_process_lock(self.path):
                    pass
        self.assertIn("ocupado há muito tempo", str(context.exception))

    @unittest.skipIf(os.name == "nt", "POSIX-specific flock behavior")
    def test_posix_writer_lock_times_out_with_actionable_error(self):
        import fcntl

        with mock.patch.object(__import__("meeting_index"), "LOCK_TIMEOUT_SECONDS", 0.01), \
                mock.patch.object(__import__("meeting_index"), "LOCK_POLL_SECONDS", 0), \
                mock.patch.object(fcntl, "flock", side_effect=BlockingIOError("busy")):
            with self.assertRaises(IndexUnavailable) as context:
                with _cross_process_lock(self.path):
                    pass
        self.assertIn("ocupado há muito tempo", str(context.exception))


if __name__ == "__main__":
    unittest.main()
