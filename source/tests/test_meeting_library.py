import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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


if __name__ == "__main__":
    unittest.main()
