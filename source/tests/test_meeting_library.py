import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from meeting_library import (
    AnnotationConflict,
    MeetingLibrary,
    PathSafetyError,
    SchemaError,
    UnsupportedSchemaError,
)
from meeting_store import MeetingStore


FIXTURE = Path(__file__).parent / "fixtures" / "meeting-v1"
TMP_ROOT = Path(__file__).parent / "tmp"


class MeetingLibraryFixtureTests(unittest.TestCase):
    def setUp(self):
        TMP_ROOT.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TMP_ROOT)
        self.root = Path(self.temp.name) / "meetings"
        shutil.copytree(FIXTURE, self.root / "fixture-meeting-v1")

    def tearDown(self):
        self.temp.cleanup()

    def test_schema_one_fixture_opens_without_mutating_fixture_bytes(self):
        before = {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
        }
        store = MeetingStore(self.root)
        metadata = store.get("fixture-meeting-v1", include_events=False)
        self.assertEqual(metadata["schema_version"], 1)
        self.assertEqual(metadata["summary"]["revision"], "revision-1")
        self.assertEqual(metadata["reviewed_summary"], "Revisar o marco na próxima reunião.")
        self.assertEqual(
            list(store.get_transcript("fixture-meeting-v1", "revision-1"))[0]["text"],
            "A equipe confirmou o próximo marco.",
        )
        after = {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
        }
        self.assertEqual(before, after)

    def test_fixture_metadata_is_json_schema_one(self):
        data = json.loads((self.root / "fixture-meeting-v1" / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(set(data["tracks"]), {"microphone", "system"})


class MeetingLibrarySidecarTests(unittest.TestCase):
    def setUp(self):
        TMP_ROOT.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TMP_ROOT)
        self.home = Path(self.temp.name)
        self.meetings = self.home / "meetings"
        shutil.copytree(FIXTURE, self.meetings / "fixture-meeting-v1")
        self.store = MeetingStore(self.meetings)
        self.library = MeetingLibrary(self.store, workspace_root=self.home)

    def tearDown(self):
        self.temp.cleanup()

    @property
    def session_dir(self):
        return self.meetings / "fixture-meeting-v1"

    def test_legacy_read_projects_fields_without_eager_sidecar(self):
        self.assertFalse((self.session_dir / "annotations.json").exists())
        annotations = self.library.read_annotations("fixture-meeting-v1")
        self.assertEqual(annotations["generation"], 0)
        self.assertEqual(annotations["title"], "Weekly product review")
        self.assertEqual(annotations["bookmarks"][0]["time"], 1.25)
        self.assertEqual(annotations["reviewed_summary"], "Revisar o marco na próxima reunião.")
        self.assertFalse((self.session_dir / "annotations.json").exists())
        self.assertEqual(self.library.get_session("fixture-meeting-v1")["title"], "Weekly product review")

    def test_first_mutation_writes_one_atomic_sidecar_and_mirrors_legacy_fields(self):
        result = self.library.update_annotations(
            "fixture-meeting-v1",
            {"title": "Weekly product review — edited", "tags": ["planning"]},
            expected_generation=0,
        )
        self.assertEqual(result["generation"], 1)
        self.assertEqual(json.loads((self.session_dir / "annotations.json").read_text(encoding="utf-8"))["tags"], ["planning"])
        self.assertEqual(self.library.get_session("fixture-meeting-v1")["title"], "Weekly product review — edited")
        self.assertEqual(self.store.get("fixture-meeting-v1")["title"], "Weekly product review — edited")
        self.assertEqual((self.home / "workspace.json").exists(), False)

    def test_two_library_instances_reject_stale_compare_and_swap(self):
        first = MeetingLibrary(MeetingStore(self.meetings), workspace_root=self.home)
        second = MeetingLibrary(MeetingStore(self.meetings), workspace_root=self.home)
        first.update_annotations("fixture-meeting-v1", {"title": "first"}, expected_generation=0)
        with self.assertRaises(AnnotationConflict) as raised:
            second.update_annotations("fixture-meeting-v1", {"title": "stale"}, expected_generation=0)
        self.assertEqual((raised.exception.expected, raised.exception.actual), (0, 1))
        self.assertEqual(first.read_annotations("fixture-meeting-v1")["title"], "first")

    def test_replace_failure_preserves_previous_sidecar_and_cleans_atomic_temp(self):
        self.library.update_annotations("fixture-meeting-v1", {"notes": "before"}, expected_generation=0)
        path = self.session_dir / "annotations.json"
        before = path.read_bytes()
        with mock.patch("meeting_library.write_json_atomic", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.library.update_annotations("fixture-meeting-v1", {"notes": "after"}, expected_generation=1)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(self.session_dir.glob("annotations.json.*.tmp")), [])

    def test_unknown_fields_survive_known_schema_update(self):
        path = self.session_dir / "annotations.json"
        value = self.library.read_annotations("fixture-meeting-v1")
        value["future_field"] = {"keep": [1, 2, 3]}
        value["generation"] = 3
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        self.library.update_annotations("fixture-meeting-v1", {"notes": "changed"}, expected_generation=3)
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["future_field"], {"keep": [1, 2, 3]})
        self.assertEqual(saved["generation"], 4)

    def test_future_or_malformed_sidecar_is_read_only_and_never_falls_back(self):
        path = self.session_dir / "annotations.json"
        future = self.library.read_annotations("fixture-meeting-v1")
        future["schema_version"] = 99
        future["title"] = "future title"
        path.write_text(json.dumps(future), encoding="utf-8")
        before = path.read_bytes()
        with self.assertRaises(UnsupportedSchemaError):
            self.library.get_session("fixture-meeting-v1")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.store.get("fixture-meeting-v1")["title"], "Weekly product review")

        path.write_text("{not-json", encoding="utf-8")
        with self.assertRaises(SchemaError):
            self.library.read_annotations("fixture-meeting-v1")

    def test_generation_rejects_bool_negative_and_float(self):
        for value in (True, -1, 1.0):
            with self.assertRaises(SchemaError):
                self.library.update_annotations("fixture-meeting-v1", {"title": "x"}, expected_generation=value)

    def test_workspace_unknown_fields_and_collection_references_are_versioned(self):
        workspace = self.library.update_workspace(
            {"collections": [{"id": "project-1", "name": "Project"}], "future": {"keep": True}},
            expected_generation=0,
        )
        self.assertEqual(workspace["generation"], 1)
        self.library.update_annotations("fixture-meeting-v1", {"collection_ids": ["project-1"]}, expected_generation=0)
        self.assertEqual(self.library.read_annotations("fixture-meeting-v1")["collection_ids"], ["project-1"])
        self.assertEqual(self.library.read_workspace()["future"], {"keep": True})

    def test_traversal_and_bundle_symlink_cannot_redirect_sidecar_write(self):
        with self.assertRaises(ValueError):
            self.library.read_annotations("../outside")
        outside = self.home / "outside"
        outside.mkdir()
        link = self.meetings / "linked-meeting"
        try:
            os.symlink(outside, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable on this Windows runtime")
        with self.assertRaises(PathSafetyError):
            self.library.update_annotations("linked-meeting", {"title": "escape"})

    def test_canonical_annotation_commit_wins_when_index_projection_fails(self):
        with mock.patch.object(self.library.index, "index_store_session", side_effect=OSError("sqlite busy")):
            result = self.library.update_annotations(
                "fixture-meeting-v1", {"title": "canonical title"}, expected_generation=0
            )
        self.assertEqual(result["title"], "canonical title")
        self.assertEqual(self.library.get_session("fixture-meeting-v1")["title"], "canonical title")
        self.assertEqual(self.library.index_state, "stale")

    def test_export_uses_sidecar_metadata_without_bypassing_canonical_authority(self):
        self.library.update_annotations(
            "fixture-meeting-v1", {"title": "Exported canonical title", "notes": "Exported notes"},
            expected_generation=0,
        )
        destination = self.home / "outside.md"
        self.library.export("fixture-meeting-v1", destination, "markdown")
        text = destination.read_text(encoding="utf-8")
        self.assertIn("# Exported canonical title", text)
        self.assertIn("Exported notes", text)

    def test_projection_success_clears_stale_fallback(self):
        class FakeIndex:
            state = "ready"

            def index_store_session(self, *_args, **_kwargs):
                return True

            def list_sessions(self, **_kwargs):
                return [{"id": "indexed", "title": "from-index"}]

            def mark_stale(self, *_args, **_kwargs):
                return True

        self.library._index = FakeIndex()
        self.library._index_stale = True
        self.assertTrue(self.library.project_session("fixture-meeting-v1"))
        self.assertEqual(self.library.list_sessions(), [{"id": "indexed", "title": "from-index"}])

    def test_canonical_fallback_projects_sidecar_title(self):
        class UnavailableIndex:
            state = "unavailable"

        self.library._index = UnavailableIndex()
        self.library.update_annotations(
            "fixture-meeting-v1", {"title": "Sidecar title"}, expected_generation=0,
        )
        self.library._index_stale = True
        self.assertEqual(self.library.list_sessions()[0]["title"], "Sidecar title")

    def test_future_sidecar_is_not_silently_omitted_by_reconcile(self):
        path = self.session_dir / "annotations.json"
        value = self.library.read_annotations("fixture-meeting-v1")
        value["schema_version"] = 99
        path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(UnsupportedSchemaError):
            self.library.reconcile()

    def test_automatic_projection_scans_beyond_legacy_list_offset(self):
        class FakeStore:
            def __init__(self, root):
                self.root = str(root)

            def get(self, session_id, include_events=False):
                return {
                    "schema_version": 1,
                    "id": session_id,
                    "title": session_id,
                    "notes": "",
                    "status": "completed",
                    "created_at": "2026-09-16T12:00:00Z",
                    "updated_at": "2026-09-16T12:00:00Z",
                    "duration": 0.0,
                    "error": None,
                    "revisions": [],
                }

            def get_transcript(self, *_args):
                return iter(())

        class FakeIndex:
            state = "ready"

            def rebuild(self, sessions, **_kwargs):
                self.sessions = list(sessions)
                return {"state": "ready", "sessions": len(self.sessions)}

        root = self.home / "many-meetings"
        root.mkdir()
        for index in range(10_501):
            (root / f"meeting-{index:05d}").mkdir()
        fake = FakeStore(root)
        index = FakeIndex()
        library = MeetingLibrary(root, store=fake, index=index, workspace_root=self.home)
        result = library.reconcile()
        self.assertEqual(result["sessions"], 10_501)
        self.assertEqual(len(index.sessions), 10_501)

    def test_report_revisions_are_immutable_and_listed_with_legacy_projection(self):
        legacy = self.library.list_reports("fixture-meeting-v1")
        self.assertEqual(len(legacy), 1)
        self.assertEqual(legacy[0]["kind"], "legacy-summary")
        self.assertTrue(legacy[0]["virtual"])

        envelope = {
            "schema_version": 1,
            "id": "report-1",
            "kind": "report",
            "profile_id": "general",
            "profile_version": 1,
            "session_id": "fixture-meeting-v1",
            "transcript_revision": "revision-1",
            "model": {"id": "local-model", "sha256": "a" * 64, "runtime": "llama.cpp"},
            "generated": {
                "summary": {
                    "text": "Confirmed milestone",
                    "citations": ["microphone:0.000000:3.000000"],
                },
            },
            "created_at": "2026-09-16T12:30:00Z",
        }
        saved = self.library.save_report("fixture-meeting-v1", envelope)
        self.assertEqual(saved["id"], "report-1")
        path = self.session_dir / "reports" / "report-1.json"
        before = path.read_bytes()
        with self.assertRaises(FileExistsError):
            self.library.save_report("fixture-meeting-v1", envelope)
        self.assertEqual(path.read_bytes(), before)
        reports = self.library.list_reports("fixture-meeting-v1")
        self.assertEqual([item["id"] for item in reports], ["legacy-summary", "report-1"])
        self.assertEqual(self.library.get_report("fixture-meeting-v1", "report-1"), saved)

    def test_reviewed_report_is_separate_and_generation_checked(self):
        envelope = {
            "schema_version": 1,
            "id": "report-1",
            "kind": "report",
            "profile_id": "general",
            "profile_version": 1,
            "session_id": "fixture-meeting-v1",
            "transcript_revision": "revision-1",
            "model": {"id": "local-model", "sha256": "b" * 64, "runtime": "llama.cpp"},
            "generated": {
                "summary": {
                    "text": "Generated",
                    "citations": ["microphone:0.000000:3.000000"],
                },
            },
            "created_at": "2026-09-16T12:30:00Z",
        }
        self.library.save_report("fixture-meeting-v1", envelope)
        review = self.library.review_report(
            "fixture-meeting-v1", "report-1", {"summary": "Reviewed"},
            expected_generation=0,
        )
        self.assertEqual(review["generation"], 1)
        self.assertEqual(review["sections"], {"summary": "Reviewed"})
        with self.assertRaises(AnnotationConflict):
            self.library.review_report(
                "fixture-meeting-v1", "report-1", {"summary": "Stale"},
                expected_generation=0,
            )
        self.assertEqual(
            self.library.get_report("fixture-meeting-v1", "report-1")["generated"],
            envelope["generated"],
        )

    def test_report_validation_rejects_unknown_revision_and_absolute_paths(self):
        envelope = {
            "schema_version": 1,
            "id": "report-1",
            "kind": "report",
            "profile_id": "general",
            "profile_version": 1,
            "session_id": "fixture-meeting-v1",
            "transcript_revision": "missing-revision",
            "model": {"id": "local-model", "sha256": "c" * 64, "runtime": "llama.cpp"},
            "generated": {"summary": {"text": str(self.home), "citations": ["segment-1"]}},
            "created_at": "2026-09-16T12:30:00Z",
            "source_path": str(self.home),
        }
        with self.assertRaises(SchemaError):
            self.library.save_report("fixture-meeting-v1", envelope)
        self.assertFalse((self.session_dir / "reports").exists())


if __name__ == "__main__":
    unittest.main()
