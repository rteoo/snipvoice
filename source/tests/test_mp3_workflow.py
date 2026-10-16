"""Codec integration checks; run with the provisioned clean audio runtime."""

import importlib.util
from pathlib import Path
import struct
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from meeting_files import export_highlight_clip, import_audio
from meeting_mixdown import export_mixdown
from meeting_storage import compact_capture, verify_final_audio, verify_mp3_file
from meeting_store import MeetingStore, TrackUnavailableError
from meeting_transcription import MeetingTranscriber


class SampleProvider:
    def __init__(self):
        self.samples = 0

    def prepare(self, *_args, **_kwargs):
        pass

    def transcribe(self, samples, **_kwargs):
        self.samples += len(samples)
        return "Synthetic codec fixture"

    def unload(self):
        pass


@unittest.skipUnless(importlib.util.find_spec("av"), "provisioned MP3 runtime unavailable")
class Mp3WorkflowTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).parent / "tmp"
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = MeetingStore(self.root / "meetings")
        self.cancel = threading.Event()

    def recording(self, rate=48000, channels=2):
        sid = self.store.begin({}, "Synthetic audio")
        # Nonzero asymmetric channels catch accidental channel cancellation.
        payload = struct.pack("<" + "f" * channels, *([.15, .05][:channels])) * rate
        for sequence in range(2):
            self.store.append_audio(sid, {
                "type": "audio", "generation": 1, "track": "system", "rate": rate,
                "channels": channels, "frames": rate, "timestamp": sequence, "sequence": sequence,
            }, payload)
        self.store.finish(sid)
        path = self.root / (sid + ".mp3")
        proof = {}

        def validate(temporary, duration):
            proof.update(verify_mp3_file(temporary, duration, self.cancel))
        export_mixdown(self.store, sid, path, cancel_event=self.cancel, validate_output=validate)
        proof["path"] = str(path)
        self.store.save_final_audio(sid, path, verification=proof)
        return sid, path, proof

    def test_mp3_survives_compaction_and_feeds_transcription_and_retry(self):
        for rate, channels in ((48000, 2), (16000, 1), (8000, 1)):
            with self.subTest(rate=rate, channels=channels):
                sid, path, proof = self.recording(rate, channels)
                self.assertAlmostEqual(proof["duration"], 2, delta=.05)
                compact_capture(self.store, sid, proof)
                self.assertTrue(path.is_file())
                with self.assertRaises(TrackUnavailableError):
                    list(self.store.iter_audio(sid))
                provider = SampleProvider()
                transcriber = MeetingTranscriber(self.store, provider)
                revision = transcriber.transcribe(sid, "fixture", "en", self.cancel)
                segments = list(self.store.get_transcript(sid, revision))
                self.assertEqual({item["track"] for item in segments}, {"final"})
                self.assertGreater(provider.samples, 30000)
                retry = transcriber.transcribe(sid, "fixture", "en", self.cancel)
                repeated = list(self.store.get_transcript(sid, retry))
                self.assertNotEqual(retry, revision)
                self.assertEqual([(s["id"], s["text"]) for s in segments],
                                 [(s["id"], s["text"]) for s in repeated])
                self.assertAlmostEqual(verify_final_audio(self.store, sid)["duration"], 2, delta=.05)

    def test_mp3_only_highlight_and_import(self):
        sid, _path, proof = self.recording()
        compact_capture(self.store, sid, proof)
        clip = self.root / "clip.mp3"
        export_highlight_clip(self.store, sid, {"track": "final", "start": .5, "end": 1.5}, clip, self.cancel)
        verified = verify_mp3_file(clip, 1, self.cancel)
        self.assertAlmostEqual(verified["duration"], 1, delta=.05)
        imported = import_audio(self.store, clip, {}, self.cancel)
        events = list(self.store.iter_audio(imported))
        self.assertAlmostEqual(sum(event["frames"] / event["rate"] for event, _ in events), 1, delta=.05)
        with self.assertRaises(FileExistsError):
            export_highlight_clip(self.store, sid, {"track": "final", "start": .5, "end": 1.5}, clip, self.cancel)

    def test_device_format_handoff_preserves_mp3_duration_and_stereo(self):
        sid = self.store.begin({}, "Synthetic format handoff")
        for sequence, (rate, channels) in enumerate(((48000, 1), (16000, 2))):
            payload = struct.pack("<" + "f" * channels, *([.15, .05][:channels])) * rate
            self.store.append_audio(sid, {
                "type": "audio", "generation": 1, "track": "system", "rate": rate,
                "channels": channels, "frames": rate, "timestamp": sequence, "sequence": sequence,
            }, payload)
        self.store.finish(sid)
        path = self.root / "handoff.mp3"
        proof = {}
        export_mixdown(self.store, sid, path,
                       validate_output=lambda temporary, duration:
                       proof.update(verify_mp3_file(temporary, duration, self.cancel)))
        self.assertAlmostEqual(proof["duration"], 2, delta=.05)
        import av
        with av.open(str(path)) as container:
            stream = container.streams.audio[0]
            self.assertEqual((stream.codec_context.sample_rate, stream.codec_context.channels), (48000, 2))
            self.assertEqual(sum(frame.samples for frame in container.decode(stream)), 96000)

    def test_six_channel_capture_downmixes_to_stereo_and_preserves_duration(self):
        sid = self.store.begin({}, "Synthetic multichannel capture")
        values = (0.0, 0.0, 0.6, 0.0, 0.0, 0.0)
        payload = struct.pack("<6f", *values) * 48000
        for sequence in range(2):
            self.store.append_audio(sid, {
                "type": "audio", "generation": 1, "track": "system", "rate": 48000,
                "channels": 6, "frames": 48000, "timestamp": sequence, "sequence": sequence,
            }, payload)
        self.store.finish(sid)
        path = self.root / "multichannel.mp3"
        proof = {}
        export_mixdown(self.store, sid, path,
                       validate_output=lambda temporary, duration:
                       proof.update(verify_mp3_file(temporary, duration, self.cancel)))
        self.assertAlmostEqual(proof["duration"], 2, delta=.05)
        import av
        with av.open(str(path)) as container:
            stream = container.streams.audio[0]
            self.assertEqual((stream.codec_context.sample_rate, stream.codec_context.channels), (48000, 2))
            self.assertAlmostEqual(sum(frame.samples for frame in container.decode(stream)), 96000, delta=1152)

    def test_validation_failure_preserves_previous_mp3_and_capture(self):
        sid, path, _proof = self.recording()
        previous = path.read_bytes()
        def reject(_path, _duration):
            raise ValueError("decode validation failed")
        with self.assertRaisesRegex(ValueError, "validation failed"):
            export_mixdown(self.store, sid, path, validate_output=reject)
        self.assertEqual(path.read_bytes(), previous)
        self.assertTrue(list(self.store.iter_audio(sid)))
        self.assertFalse(list(self.root.glob("*.tmp")))
