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
        self.speech_gate = None

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

    def test_quiet_chunk_reaches_provider_when_speech_gate_is_owned_by_provider(self):
        self.store.append_audio(self.session, self.event(), self.payload(value=1e-6))
        provider = FakeProvider()
        provider.speech_gate = object()
        revision = MeetingTranscriber(self.store, provider).transcribe(
            self.session, "balanced", "auto"
        )
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(list(self.store.get_transcript(self.session, revision))[0]["text"], "texto local")

    def test_gate_cancellation_marks_revision_cancelled(self):
        from voice_activity import VoiceActivityError
        self.store.append_audio(self.session, self.event(), self.payload(value=0.1))
        provider = FakeProvider()
        provider.speech_gate = object()
        cancelled = threading.Event()

        def cancel_on_transcribe(_samples, cancel_event=None):
            cancelled.set()
            raise VoiceActivityError("Speech detection was cancelled.")

        provider.transcribe = cancel_on_transcribe
        revision = MeetingTranscriber(self.store, provider).transcribe(
            self.session, "balanced", "auto", cancelled
        )
        self.assertEqual(self.store.get(self.session)["revisions"][0]["status"], "cancelled")
        self.assertEqual(revision, self.store.get(self.session)["revisions"][0]["id"])

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

    def test_saved_final_audio_is_transcribed_without_raw_pcm(self):
        final = Path(self.temp.name) / "final.mp3"
        final.write_bytes(b"synthetic")
        self.store.save_final_audio(self.session, final)
        metadata = self.store.get(self.session, include_events=False)
        self.store.get(self.session)
        provider = FakeProvider()
        payload = self.payload(frames=160, value=0.2)

        def decode(saved, start, cancel_event):
            self.assertEqual(saved["final_audio"]["path"], str(final.absolute()))
            self.assertEqual(start, 0.0)
            self.assertFalse(cancel_event.is_set())
            yield 16000, 1, payload

        revision = MeetingTranscriber(self.store, provider, final_audio_decoder=decode).transcribe(
            self.session, "balanced", "auto")
        segments = list(self.store.get_transcript(self.session, revision))
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0]["track"], "final")
        self.assertTrue(segments[0]["id"].startswith("final:"))
        self.assertEqual(provider.calls, [list(struct.unpack("<160f", payload))])

    def test_final_source_switch_starts_clean_revision(self):
        self.store.append_audio(self.session, self.event(), self.payload())
        previous = self.store.begin_revision(self.session, "balanced", "auto")
        self.store.add_transcript(self.session, previous, {
            "id": "microphone:0.000000:0.010000", "track": "microphone",
            "start": 0.0, "end": 0.01, "text": "raw result",
        })
        final = Path(self.temp.name) / "final.mp3"
        final.write_bytes(b"synthetic")
        self.store.save_final_audio(self.session, final)
        payload = self.payload(frames=160, value=0.2)
        provider = FakeProvider()
        transcriber = MeetingTranscriber(
            self.store, provider,
            final_audio_decoder=lambda *_args: iter(((16000, 1, payload),)),
        )
        revision = transcriber.transcribe(self.session, "balanced", "auto", revision=previous)
        self.assertNotEqual(revision, previous)
        self.assertEqual(self.store.get(self.session)["revisions"][0]["status"], "superseded")
        segments = list(self.store.get_transcript(self.session, revision))
        self.assertEqual([segment["track"] for segment in segments], ["final"])

    def test_empty_final_decode_fails_revision_instead_of_completing(self):
        final = Path(self.temp.name) / "final.mp3"
        final.write_bytes(b"synthetic")
        self.store.save_final_audio(self.session, final)
        provider = FakeProvider()
        with self.assertRaisesRegex(Exception, "áudio final"):
            MeetingTranscriber(
                self.store, provider, final_audio_decoder=lambda *_args: iter(())
            ).transcribe(self.session, "balanced", "auto")
        self.assertEqual(self.store.get(self.session)["revisions"][0]["status"], "failed")

    def _save_final_with_decoder(self, decoder):
        final = Path(self.temp.name) / "final.mp3"
        final.write_bytes(b"synthetic")
        self.store.save_final_audio(self.session, final)
        return decoder

    def test_final_source_resume_reuses_completed_segments_without_duplication(self):
        payload = self.payload(frames=160, value=0.2)
        decoder = self._save_final_with_decoder(
            lambda *_args: iter(((16000, 1, payload),))
        )
        provider = FakeProvider()
        transcriber = MeetingTranscriber(self.store, provider, final_audio_decoder=decoder)
        revision = self.store.begin_revision(self.session, "balanced", "auto")
        first = transcriber.transcribe(self.session, "balanced", "auto", revision=revision)
        self.assertEqual(len(list(self.store.get_transcript(self.session, first))), 1)
        self.store.finish_revision(self.session, first, "failed", "synthetic interruption")
        resumed = transcriber.transcribe(self.session, "balanced", "auto", revision=first)
        self.assertEqual(len(list(self.store.get_transcript(self.session, resumed))), 1)
        self.assertEqual(len(provider.calls), 1)

    def test_final_source_cancellation_is_cancelled_not_failed(self):
        payload = self.payload(frames=160, value=0.2)
        cancelled = threading.Event()

        def decode(*_args):
            yield 16000, 1, payload
            cancelled.set()
            raise RuntimeError("decoder stopped")

        self._save_final_with_decoder(decode)
        provider = FakeProvider()
        revision = MeetingTranscriber(self.store, provider, final_audio_decoder=decode).transcribe(
            self.session, "balanced", "auto", cancelled)
        self.assertEqual(self.store.get(self.session)["revisions"][0]["status"], "cancelled")
        self.assertEqual(revision, self.store.get(self.session)["revisions"][0]["id"])
        self.assertEqual(provider.cancelled, 1)

    def test_final_frames_are_bounded_by_transcription_chunk_limit(self):
        frames = 1152
        payload = self.payload(frames=frames, value=0.2)
        decoder = self._save_final_with_decoder(
            lambda *_args: iter((
                (16000, 1, payload), (16000, 1, payload), (16000, 1, payload),
            ))
        )
        provider = FakeProvider()
        MeetingTranscriber(
            self.store, provider, chunk_seconds=0.1, final_audio_decoder=decoder,
        ).transcribe(self.session, "balanced", "auto")
        self.assertTrue(provider.calls)
        self.assertLessEqual(max(map(len, provider.calls)), 1600)


if __name__ == "__main__":
    unittest.main()
