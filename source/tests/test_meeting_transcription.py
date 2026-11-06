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
from meeting_transcription import MeetingTranscriber, MeetingTranscriptionError, _pcm_to_mono


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


def _reference_mono(payload, channels):
    """The original per-sample conversion, kept as the exactness oracle."""
    values = []
    for offset in range(0, len(payload), 4 * channels):
        total = 0.0
        for channel in range(channels):
            total += struct.unpack_from("<f", payload, offset + channel * 4)[0]
        values.append(total / channels)
    return values


class PcmToMonoTests(unittest.TestCase):
    def test_matches_the_per_sample_reference_exactly(self):
        samples = [((index * 7919) % 2001 - 1000) / 997.0 for index in range(6 * 1201)]
        samples[:6] = [1e-38, -1e-38, 3.4e38, -3.4e38, 0.1, -0.0]
        samples[6:12] = [-0.0] * 6  # zero sign follows the 0.0 start value
        for channels in (1, 2, 6):
            payload = struct.pack("<%df" % len(samples), *samples)
            with self.subTest(channels=channels):
                self.assertEqual(list(_pcm_to_mono(payload, channels)), _reference_mono(payload, channels))

    def test_rejects_partial_frames(self):
        with self.assertRaises(MeetingTranscriptionError):
            _pcm_to_mono(bytes(12), 2)


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

    def _append_blocks(self, levels, block_seconds=0.1, rate=16000):
        frames = int(block_seconds * rate)
        for index, value in enumerate(levels):
            self.store.append_audio(
                self.session, self.event(timestamp=index * block_seconds, sequence=index, frames=frames),
                self.payload(frames=frames, value=value))

    def _chunk_ranges(self):
        revision = MeetingTranscriber(self.store, FakeProvider()).transcribe(self.session, "balanced", "pt-BR")
        return [(round(item["start"], 1), round(item["end"], 1))
                for item in self.store.get_transcript(self.session, revision)]

    def test_chunks_end_inside_a_pause_instead_of_cutting_a_word(self):
        # 24 s of speech-level audio, a 0.5 s pause, then 10 s more.
        self._append_blocks([0.1] * 240 + [0.0005] * 5 + [0.1] * 100)
        ranges = self._chunk_ranges()
        self.assertEqual(ranges[0][0], 0.0)
        self.assertGreater(ranges[0][1], 24.2)
        self.assertLessEqual(ranges[0][1], 24.5)
        self.assertEqual(ranges[-1][1], 34.5)

    def test_pause_before_two_thirds_of_a_chunk_does_not_split(self):
        self._append_blocks([0.1] * 100 + [0.0] * 5 + [0.1] * 250)
        # The hard limit is unchanged: 300 x 0.1 s sums just past 30.0 s.
        self.assertEqual(self._chunk_ranges()[0], (0.0, 29.9))

    def test_continuous_speech_and_silence_keep_the_hard_limit(self):
        self._append_blocks([0.1] * 350)
        self.assertEqual(self._chunk_ranges(), [(0.0, 29.9), (29.9, 35.0)])
        self.tearDown()
        self.setUp()
        self._append_blocks([0.0] * 350)
        self.assertEqual(self._chunk_ranges(), [(0.0, 29.9), (29.9, 35.0)])

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
