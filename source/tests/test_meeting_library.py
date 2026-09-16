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


if __name__ == "__main__":
    unittest.main()
