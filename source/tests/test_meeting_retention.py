from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from meeting_retention import (
    ActiveLeaseError,
    ConfirmationRequired,
    MeetingRetention,
    OperationRecoveryError,
    PlanConflict,
    RetentionLockTimeout,
    RetentionPolicy,
    RetentionError,
    RetentionSafetyError,
)
from meeting_store import MeetingStore


FIXTURE = Path(__file__).parent / "fixtures" / "meeting-v1"
TMP_ROOT = Path(__file__).parent / "tmp"
UTC = timezone.utc


class FailingProjectionLibrary:
    def __init__(self, store, home):
        self.store = store
        self.home_root = home
        self.meetings_root = store.root
        self.stale = False
        self.deleted_calls = 0

    def get_session(self, session_id, include_events=False):
        return self.store.get(session_id, include_events=include_events)

    def on_session_deleted(self, _session_id):
        self.deleted_calls += 1
        raise OSError("simulated index failure")

    def _mark_index_stale(self):
        self.stale = True


def _hold_retention_lock(path, started, release, errors):
    try:
        from meeting_retention import _cross_process_lock

        with _cross_process_lock(path, 5.0):
            started.set()
            release.wait(5.0)
    except BaseException as error:  # pragma: no cover - only child-process diagnostics
        errors.put(repr(error))
        started.set()


class MeetingRetentionTests(unittest.TestCase):
    def setUp(self):
        TMP_ROOT.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TMP_ROOT)
        self.home = Path(self.temp.name)
        self.meetings = self.home / "meetings"
        shutil.copytree(FIXTURE, self.meetings / "fixture-meeting-v1")
        self.store = MeetingStore(self.meetings)
        self.now = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
        self.external = self.home / "recordings" / "exported.wav"
        self.external.parent.mkdir()
        self.external.write_bytes(b"RIFF external export")
        metadata_path = self.meetings / "fixture-meeting-v1" / "metadata.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["final_audio"] = {"path": str(self.external), "created_at": "2026-09-16T12:04:00Z"}
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def retention(self, **kwargs):
        return MeetingRetention(
            self.store,
            workspace_root=self.home,
            clock=lambda: self.now,
            **kwargs,
        )

    def test_operation_id_uses_injected_clock(self):
        self.assertTrue(self.retention()._operation_id().startswith("20260920120000-"))

    def test_cross_process_writer_lock_times_out_without_mutation(self):
        retention = self.retention(lock_timeout=0.1)
        plan = retention.plan(
            "fixture-meeting-v1", RetentionPolicy.whole_meeting(after_days=1)
        )
        before = {
            path.relative_to(self.home).as_posix(): path.read_bytes()
            for path in (self.meetings / "fixture-meeting-v1").rglob("*")
            if path.is_file()
        }
        context = multiprocessing.get_context("spawn" if os.name == "nt" else "fork")
        started = context.Event()
        release = context.Event()
        errors = context.Queue()
        process = context.Process(
            target=_hold_retention_lock,
            args=(str(self.home / "retention.lock"), started, release, errors),
        )
        process.start()
        try:
            self.assertTrue(started.wait(5.0), errors.get_nowait() if not errors.empty() else "child did not acquire lock")
            with self.assertRaises(RetentionLockTimeout):
                retention.apply(plan, confirm=True)
            after = {
                path.relative_to(self.home).as_posix(): path.read_bytes()
                for path in (self.meetings / "fixture-meeting-v1").rglob("*")
                if path.is_file()
            }
            self.assertEqual(before, after)
            self.assertFalse((self.home / "trash").exists())
        finally:
            release.set()
            process.join(5.0)
            if process.is_alive():
                process.terminate()
                process.join(5.0)
        self.assertEqual(process.exitcode, 0, errors.get_nowait() if not errors.empty() else "child failed")

    def test_plan_is_immutable_exact_and_non_mutating(self):
        session_root = self.meetings / "fixture-meeting-v1"
        before = {
            path.relative_to(self.home).as_posix(): path.read_bytes()
            for path in self.home.rglob("*")
            if path.is_file()
        }
        retention = self.retention()
        plan = retention.plan(
            "fixture-meeting-v1",
            RetentionPolicy.whole_meeting(after_days=1),
        )

        self.assertTrue(plan.eligible)
        self.assertEqual(plan.operation, "whole_meeting")
        self.assertGreaterEqual(plan.byte_estimate, 0)
        self.assertEqual(plan.recovery_mode, "same-root-trash")
        self.assertIn(str(self.external), plan.excluded_external_exports)
        self.assertTrue(all(path.startswith(str(session_root)) for path in plan.target_paths))
        with self.assertRaises((FrozenInstanceError, AttributeError, TypeError)):
            plan.operation = "purge"
        with self.assertRaises((AttributeError, TypeError)):
            plan.targets.append("unsafe")

        after = {
            path.relative_to(self.home).as_posix(): path.read_bytes()
            for path in self.home.rglob("*")
            if path.is_file()
        }
        self.assertEqual(before, after)

    def test_raw_plan_requires_completed_reviewable_transcript_and_rejects_lease(self):
        retention = self.retention(lease_checker=lambda _session: True)
        plan = retention.plan(
            "fixture-meeting-v1",
            RetentionPolicy.raw_tracks(after_days=1),
        )
        self.assertFalse(plan.eligible)
        self.assertIn("lease", " ".join(plan.reasons).lower())
        with self.assertRaises(ActiveLeaseError):
            retention.apply(plan, confirm=True)

        shutil.copytree(FIXTURE, self.meetings / "no-review")
        path = self.meetings / "no-review" / "metadata.json"
        metadata = json.loads(path.read_text(encoding="utf-8"))
        metadata["reviewed_summary"] = ""
        metadata["summary"] = None
        path.write_text(json.dumps(metadata), encoding="utf-8")
        no_review_retention = MeetingRetention(
            MeetingStore(self.meetings),
            workspace_root=self.home,
            clock=lambda: self.now,
        )
        no_review_plan = no_review_retention.plan(
            "no-review", RetentionPolicy.raw_tracks(after_days=1)
        )
        self.assertFalse(no_review_plan.eligible)
        self.assertTrue(any("review" in reason.lower() for reason in no_review_plan.reasons))

    def test_keep_and_clock_rollback_plans_are_safe_noops(self):
        retention = self.retention()
        keep = retention.plan("fixture-meeting-v1", RetentionPolicy.keep_indefinitely())
        self.assertTrue(keep.eligible)
        self.assertEqual((keep.operation, keep.targets, keep.byte_estimate), ("keep", (), 0))
        self.assertEqual(retention.apply(keep), retention.apply(keep))

        rollback_clock = MeetingRetention(
            self.store,
            workspace_root=self.home,
            clock=lambda: datetime(2026, 9, 15, 12, 0, tzinfo=UTC),
        )
        plan = rollback_clock.plan(
            "fixture-meeting-v1", RetentionPolicy.whole_meeting(after_days=1)
        )
        self.assertFalse(plan.eligible)
        self.assertTrue(any("relógio" in reason for reason in plan.reasons))

    def test_unresolvable_citations_block_raw_removal(self):
        metadata_path = self.meetings / "fixture-meeting-v1" / "metadata.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["summary"]["citations"] = ["missing-segment"]
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        plan = self.retention().plan(
            "fixture-meeting-v1", RetentionPolicy.raw_tracks(after_days=1)
        )
        self.assertFalse(plan.eligible)
        self.assertTrue(any("citação" in reason for reason in plan.reasons))

    def test_whole_meeting_trash_restore_and_permanent_purge_are_explicit(self):
        retention = self.retention()
        plan = retention.plan(
            "fixture-meeting-v1", RetentionPolicy.whole_meeting(after_days=1)
        )
        with self.assertRaises(ConfirmationRequired):
            retention.apply(plan)
        result = retention.apply(plan, confirm=True)
        self.assertEqual(result.state, "trashed")
        self.assertFalse((self.meetings / "fixture-meeting-v1").exists())
        self.assertTrue(self.external.is_file())
        self.assertEqual(retention.list_trash()[0].session_id, "fixture-meeting-v1")

        restored = retention.restore("fixture-meeting-v1")
        self.assertEqual(restored.state, "restored")
        self.assertTrue((self.meetings / "fixture-meeting-v1").is_dir())
        self.assertTrue(self.external.is_file())

        plan = retention.plan(
            "fixture-meeting-v1", RetentionPolicy.whole_meeting(after_days=1)
        )
        retention.apply(plan, confirm=True)
        with self.assertRaises(ConfirmationRequired):
            retention.purge("fixture-meeting-v1")
        purged = retention.purge("fixture-meeting-v1", confirm=True)
        self.assertEqual(purged.state, "purged")
        self.assertFalse(retention.list_trash())
        self.assertTrue(self.external.is_file())

    def test_raw_removal_stages_tracks_updates_only_canonical_projection_and_keeps_transcript(self):
        retention = self.retention()
        plan = retention.plan(
            "fixture-meeting-v1",
            RetentionPolicy.raw_tracks(after_days=1, tracks=("microphone",)),
        )
        self.assertTrue(plan.eligible)
        result = retention.apply(plan, confirm=True)
        self.assertEqual(result.state, "finalized")
        metadata = self.store.get("fixture-meeting-v1", include_events=False)
        self.assertFalse(metadata["tracks"]["microphone"]["available"])
        self.assertTrue(metadata["tracks"]["system"]["segments"])
        self.assertEqual(
            list(self.store.get_transcript("fixture-meeting-v1", "revision-1"))[0]["id"],
            "microphone:0.000000:3.000000",
        )
        self.assertTrue(self.external.is_file())
        self.assertFalse((self.meetings / "fixture-meeting-v1" / "microphone").exists())

    def test_raw_operation_recovers_staged_state_after_restart(self):
        failures = {"raw.metadata_committed": OSError("simulated crash")}

        def fail(stage):
            error = failures.pop(stage, None)
            if error:
                raise error

        retention = self.retention(failure_injector=fail)
        plan = retention.plan(
            "fixture-meeting-v1", RetentionPolicy.raw_tracks(after_days=1)
        )
        with self.assertRaises(OSError):
            retention.apply(plan, confirm=True)
        self.assertTrue(list((self.home / "retention-ops").glob("*.jsonl")))

        restarted = self.retention()
        recovered = restarted.recover_operations()
        self.assertTrue(recovered)
        metadata = self.store.get("fixture-meeting-v1", include_events=False)
        self.assertFalse(metadata["tracks"]["microphone"]["available"])
        self.assertFalse(metadata["tracks"]["system"]["available"])
        self.assertFalse((self.meetings / "fixture-meeting-v1" / "microphone").exists())
        self.assertFalse((self.meetings / "fixture-meeting-v1" / "system").exists())

    def test_raw_stage_failure_rolls_back_without_losing_canonical_audio(self):
        def fail(stage):
            if stage == "raw.staged":
                raise OSError("simulated crash before canonical commit")

        retention = self.retention(failure_injector=fail)
        plan = retention.plan(
            "fixture-meeting-v1", RetentionPolicy.raw_tracks(after_days=1)
        )
        with self.assertRaises(OSError):
            retention.apply(plan, confirm=True)
        metadata = self.store.get("fixture-meeting-v1", include_events=False)
        self.assertTrue(metadata["tracks"]["microphone"].get("available", True))
        self.assertTrue((self.meetings / "fixture-meeting-v1" / "microphone").is_dir())
        self.assertEqual(retention.recover_operations(), ())

    def test_external_metadata_change_during_raw_staging_fails_closed(self):
        metadata_path = self.meetings / "fixture-meeting-v1" / "metadata.json"

        def fail(stage):
            if stage == "raw.track_staged":
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                metadata["tracks"]["microphone"]["available"] = False
                metadata["updated_at"] = "2026-09-20T12:00:01Z"
                metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
                raise OSError("simulated crash after external metadata edit")

        retention = self.retention(failure_injector=fail)
        plan = retention.plan(
            "fixture-meeting-v1",
            RetentionPolicy.raw_tracks(after_days=1, tracks=("microphone",)),
        )
        with self.assertRaises(OSError):
            retention.apply(plan, confirm=True)
        self.assertFalse((self.meetings / "fixture-meeting-v1" / "microphone").exists())
        restarted = self.retention()
        with self.assertRaises(OperationRecoveryError):
            restarted.recover_operations()
        self.assertTrue(list((self.home / "trash" / ".retention-ops").rglob("microphone")))

    def test_stale_review_and_citation_cannot_apply_raw_plan(self):
        metadata_path = self.meetings / "fixture-meeting-v1" / "metadata.json"
        retention = self.retention()
        review_plan = retention.plan(
            "fixture-meeting-v1",
            RetentionPolicy.raw_tracks(after_days=1, tracks=("microphone",)),
        )
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["reviewed_summary"] = ""
        metadata["summary"] = None
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        with self.assertRaises(PlanConflict):
            retention.apply(review_plan, confirm=True)

        metadata = json.loads(FIXTURE.joinpath("metadata.json").read_text(encoding="utf-8"))
        metadata["final_audio"] = {"path": str(self.external), "created_at": "2026-09-16T12:04:00Z"}
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        citation_plan = retention.plan(
            "fixture-meeting-v1",
            RetentionPolicy.raw_tracks(after_days=1, tracks=("microphone",)),
        )
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["summary"]["citations"] = ["missing-segment"]
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        with self.assertRaises(PlanConflict):
            retention.apply(citation_plan, confirm=True)

    def test_whole_meeting_plan_rechecks_age_before_apply(self):
        retention = self.retention()
        plan = retention.plan(
            "fixture-meeting-v1", RetentionPolicy.whole_meeting(after_days=1),
        )
        self.assertTrue(plan.eligible)
        changed = self.store.get("fixture-meeting-v1", include_events=False)
        changed["updated_at"] = "2099-09-16T12:04:00Z"
        with mock.patch.object(retention, "_metadata", return_value=changed):
            with self.assertRaisesRegex(PlanConflict, "elegibilidade"):
                retention.apply(plan, confirm=True)
        self.assertTrue((self.meetings / "fixture-meeting-v1").is_dir())

    def test_legacy_time_citation_must_match_real_track_and_segment_range(self):
        metadata_path = self.meetings / "fixture-meeting-v1" / "metadata.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["summary"]["citations"] = ["camera:0.000000:3.000000"]
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        invalid_track = self.retention().plan(
            "fixture-meeting-v1",
            RetentionPolicy.raw_tracks(after_days=1, tracks=("microphone",)),
        )
        self.assertFalse(invalid_track.eligible)
        self.assertTrue(any("citação" in reason for reason in invalid_track.reasons))

        metadata["summary"]["citations"] = ["microphone:300.000000:301.000000"]
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        outside_transcript = self.retention().plan(
            "fixture-meeting-v1",
            RetentionPolicy.raw_tracks(after_days=1, tracks=("microphone",)),
        )
        self.assertFalse(outside_transcript.eligible)
        self.assertTrue(any("citação" in reason for reason in outside_transcript.reasons))

    def test_projection_failure_marks_index_stale_and_stays_pending(self):
        library = FailingProjectionLibrary(self.store, self.home)
        retention = MeetingRetention(
            self.store,
            library=library,
            workspace_root=self.home,
            clock=lambda: self.now,
        )
        plan = retention.plan(
            "fixture-meeting-v1", RetentionPolicy.whole_meeting(after_days=1)
        )
        result = retention.apply(plan, confirm=True)
        self.assertEqual(result.state, "trashed")
        self.assertTrue(library.stale)
        journals = list((self.home / "retention-ops").glob("*.jsonl"))
        records = [json.loads(line) for line in journals[0].read_text(encoding="utf-8").splitlines()]
        self.assertEqual(records[-1]["state"], "tombstoned")
        self.assertFalse(any(record["state"] == "projected" for record in records))
        restarted = MeetingRetention(
            self.store,
            library=library,
            workspace_root=self.home,
            clock=lambda: self.now,
        )
        recovered = restarted.recover_operations()
        self.assertEqual(recovered[0].state, "trashed")
        self.assertEqual(
            json.loads(journals[0].read_text(encoding="utf-8").splitlines()[-1])["state"],
            "tombstoned",
        )

    def _assert_whole_recovery_ambiguous(self, failure_stage):
        def fail(stage):
            if stage == failure_stage:
                raise OSError("simulated whole-meeting interruption")

        retention = self.retention(failure_injector=fail)
        plan = retention.plan(
            "fixture-meeting-v1", RetentionPolicy.whole_meeting(after_days=1)
        )
        with self.assertRaises(OSError):
            retention.apply(plan, confirm=True)
        for path in (
            self.meetings / "fixture-meeting-v1",
            *(self.home / "trash").glob("fixture-meeting-v1--*"),
        ):
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
        with self.assertRaises(OperationRecoveryError):
            self.retention().recover_operations()

    def test_whole_prepared_recovery_rejects_missing_source_and_trash(self):
        self._assert_whole_recovery_ambiguous("whole.moved")

    def test_whole_tombstoned_recovery_rejects_missing_source_and_trash(self):
        self._assert_whole_recovery_ambiguous("whole.tombstoned")

    def test_retention_roots_cannot_escape_workspace(self):
        outside = self.home.parent / "retention-outside"
        with self.assertRaises(RetentionSafetyError):
            MeetingRetention(
                self.store,
                workspace_root=self.home,
                trash_root=outside,
                clock=lambda: self.now,
            )
        with self.assertRaises(RetentionSafetyError):
            MeetingRetention(
                self.store,
                workspace_root=self.home,
                retention_ops_root=outside,
                clock=lambda: self.now,
            )

    def test_whole_move_failure_after_rename_is_reconciled_without_touching_export(self):
        def fail(stage):
            if stage == "whole.moved":
                raise OSError("simulated crash after rename")

        retention = self.retention(failure_injector=fail)
        plan = retention.plan(
            "fixture-meeting-v1", RetentionPolicy.whole_meeting(after_days=1)
        )
        with self.assertRaises(OSError):
            retention.apply(plan, confirm=True)
        self.assertFalse((self.meetings / "fixture-meeting-v1").exists())
        self.assertTrue(self.external.is_file())
        restarted = self.retention()
        recovered = restarted.recover_operations()
        self.assertEqual(len(recovered), 1)
        self.assertEqual(restarted.list_trash()[0].session_id, "fixture-meeting-v1")
        restarted.restore("fixture-meeting-v1")
        self.assertTrue((self.meetings / "fixture-meeting-v1").is_dir())
        self.assertTrue(self.external.is_file())

    def test_purge_failure_after_remove_converges_on_restart(self):
        retention = self.retention()
        plan = retention.plan(
            "fixture-meeting-v1", RetentionPolicy.whole_meeting(after_days=1)
        )
        retention.apply(plan, confirm=True)

        def fail(stage):
            if stage == "whole.purged":
                raise OSError("simulated crash after purge rename")

        retention.failure_injector = fail
        with self.assertRaises(OSError):
            retention.purge("fixture-meeting-v1", confirm=True)
        restarted = self.retention()
        recovered = restarted.recover_operations()
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].state, "purged")
        self.assertFalse(restarted.list_trash())
        self.assertTrue(self.external.is_file())

    def test_orphaned_tombstone_is_rejected_before_any_restore_or_purge(self):
        retention = self.retention()
        plan = retention.plan(
            "fixture-meeting-v1", RetentionPolicy.whole_meeting(after_days=1)
        )
        retention.apply(plan, confirm=True)
        entry = next(path for path in (self.home / "trash").iterdir() if path.is_dir())
        shutil.rmtree(entry)
        with self.assertRaises(RetentionError):
            retention.list_trash()

    def test_symlinked_bundle_or_track_is_rejected_without_following_it(self):
        outside = self.home / "outside"
        outside.mkdir()
        linked = self.meetings / "linked-meeting"
        try:
            os.symlink(outside, linked, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable on this Windows runtime")
        retention = self.retention()
        with self.assertRaises(RetentionSafetyError):
            retention.plan("linked-meeting", RetentionPolicy.whole_meeting(after_days=0))


if __name__ == "__main__":
    unittest.main()
