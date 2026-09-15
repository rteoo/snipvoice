import os
import struct
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from meeting_store import MeetingStore
from meeting_transcription import MeetingTranscriber


class FakeProvider:
    def __init__(self):
        self.prepare_calls = []
        self.calls = []
        self.cancelled = 0
        self.unloaded = 0

    def prepare(self, profile, language, **kwargs):
        self.prepare_calls.append((profile, language, kwargs))

    def transcribe(self, samples, cancel_event=None):
        self.calls.append(list(samples))
        return "texto local"

    def cancel(self):
        self.cancelled += 1

    def unload(self):
        self.unloaded += 1


class MeetingTranscriptionTests(unittest.TestCase):
    def setUp(self):
        directory = Path(__file__).parent / "tmp"
        directory.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=directory)
        self.store = MeetingStore(self.temp.name)
        self.session = self.store.begin({"meeting_sources": "both"})

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def event(track="microphone", timestamp=0.0, sequence=0, frames=160, rate=16000, channels=1):
        return {"type": "audio", "generation": 1, "track": track, "rate": rate,
                "channels": channels, "frames": frames, "timestamp": timestamp,
                "sequence": sequence}

    @staticmethod
    def payload(frames=160, channels=1, value=0.1):
        return b"".join(struct.pack("<f", value) * channels for _ in range(frames))

    def test_prepares_installed_only_and_writes_timed_source_segments(self):
        self.store.append_audio(self.session, self.event(), self.payload())
        provider = FakeProvider()
        revision = MeetingTranscriber(self.store, provider).transcribe(self.session, "balanced", "pt-BR")
        segments = list(self.store.get_transcript(self.session, revision))
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0]["track"], "microphone")
        self.assertEqual(segments[0]["timing_precision"], "chunk")
        self.assertEqual(segments[0]["text"], "texto local")
        self.assertFalse(provider.prepare_calls[0][2]["allow_download"])

    def test_stereo_is_downmixed_and_silence_does_not_call_provider(self):
        event = self.event(channels=2)
        stereo = b"".join(struct.pack("<ff", 0.2, -0.2) for _ in range(event["frames"]))
        self.store.append_audio(self.session, event, stereo)
        provider = FakeProvider()
        revision = MeetingTranscriber(self.store, provider).transcribe(self.session, "balanced", "auto")
        self.assertEqual(provider.calls, [])
        self.assertEqual(list(self.store.get_transcript(self.session, revision))[0]["text"], "")

    def test_failed_revision_can_resume_without_duplicate_completed_chunks(self):
        self.store.append_audio(self.session, self.event(), self.payload())
        provider = FakeProvider()
        transcriber = MeetingTranscriber(self.store, provider)
        revision = self.store.begin_revision(self.session, "balanced", "auto")
        self.store.add_transcript(self.session, revision, {"id": "microphone:0.000000:0.010000", "track": "microphone", "start": 0.0, "end": 0.01, "text": "prévio"})
        transcriber.transcribe(self.session, "balanced", "auto", revision=revision)
        segments = list(self.store.get_transcript(self.session, revision))
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0]["text"], "prévio")

    def test_cancellation_marks_revision_and_unloads_provider(self):
        self.store.append_audio(self.session, self.event(), self.payload(value=0.2))
        provider = FakeProvider()
        cancelled = threading.Event()
        cancelled.set()
        revision = MeetingTranscriber(self.store, provider).transcribe(self.session, "balanced", "auto", cancelled)
        item = self.store.get(self.session)["revisions"][0]
        self.assertEqual(item["id"], revision)
        self.assertEqual(item["status"], "cancelled")
        self.assertEqual(provider.unloaded, 1)

    def test_incomplete_revision_tail_is_preserved_and_resume_uses_fresh_file(self):
        self.store.append_audio(self.session, self.event(), self.payload())
        revision = self.store.begin_revision(self.session, "balanced", "auto")
        path = Path(self.temp.name) / self.session / "transcripts" / (revision + ".jsonl")
        path.write_bytes(b'{"partial":')
        provider = FakeProvider()
        resumed = MeetingTranscriber(self.store, provider).transcribe(
            self.session, "balanced", "auto", revision=revision)
        self.assertNotEqual(resumed, revision)
        self.assertEqual(path.read_bytes(), b'{"partial":')
        self.assertEqual(len(list(self.store.get_transcript(self.session, resumed))), 1)

    def test_unload_error_signals_live_resources(self):
        from meeting_transcription import MeetingTranscriptionError
        provider = FakeProvider()
        with mock.patch.object(provider, "unload", side_effect=RuntimeError("still live")):
            with self.assertRaises(MeetingTranscriptionError) as caught:
                MeetingTranscriber(self.store, provider).transcribe(self.session, "balanced", "auto")
        self.assertTrue(caught.exception.resource_live)


if __name__ == "__main__":
    unittest.main()
