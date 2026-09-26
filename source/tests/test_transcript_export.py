"""Complete transcript exports use canonical revisions, not GUI preview pages."""

from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from meeting_files import export_transcript
from meeting_store import MeetingStore


class TranscriptExportTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).resolve().parent / "tmp"
        scratch.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = MeetingStore(self.root / "meetings")
        self.session = self.store.begin({}, "Synthetic recording")
        self.store.finish(self.session)

    def revision(self, texts):
        revision = self.store.begin_revision(self.session, "synthetic", "pt")
        for index, text in enumerate(texts):
            self.store.add_transcript(self.session, revision, {
                "id": f"s{index}", "track": "microphone", "start": index * 5,
                "end": index * 5 + 1, "text": text,
            })
        self.store.finish_revision(self.session, revision)
        return revision

    def test_full_export_contains_all_segments_beyond_preview_and_page_limits(self):
        texts = [f"Segment {index:04d}: " + "word " * 300 for index in range(750)]
        revision = self.revision(texts)
        destination = self.root / "complete.txt"
        export_transcript(self.store, self.session, destination, revision=revision)
        result = destination.read_text(encoding="utf-8")
        self.assertGreater(len(result), 1 << 20)
        self.assertEqual(result, "\n\n".join(text.strip() for text in texts))
        self.assertNotIn("00:00:00", result)

    def test_timestamped_export_uses_selected_revision(self):
        original = self.revision(["First text", "Second text"])
        self.revision(["Replacement text"])
        destination = self.root / "timed.txt"
        export_transcript(self.store, self.session, destination,
                          revision=original, style="timestamped")
        self.assertEqual(destination.read_text(encoding="utf-8"),
                         "00:00:00 Microfone: First text\n\n00:00:05 Microfone: Second text")

    def test_cancel_during_export_preserves_existing_destination(self):
        cancel = threading.Event()
        destination = self.root / "existing.txt"
        destination.write_text("Previous export", encoding="utf-8")

        def segments(*_args):
            yield {"text": "One", "start": 0}
            cancel.set()
            yield {"text": "Two", "start": 5}

        with mock.patch.object(self.store, "get_transcript", side_effect=segments):
            with self.assertRaisesRegex(RuntimeError, "cancelada"):
                export_transcript(self.store, self.session, destination,
                                  style="timestamped", cancel_event=cancel)
        self.assertEqual(destination.read_text(encoding="utf-8"), "Previous export")
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_invalid_format_and_library_destination_are_rejected(self):
        with self.assertRaises(ValueError):
            export_transcript(self.store, self.session, self.root / "invalid.txt", style="json")
        with self.assertRaisesRegex(ValueError, "fora da biblioteca"):
            export_transcript(self.store, self.session, Path(self.store.root) / "unsafe.txt")


if __name__ == "__main__":
    unittest.main()
