"""Focused streaming and preservation checks for meeting_mixdown."""

import os
from pathlib import Path
import struct
import tempfile
import threading
import unittest
import wave

from meeting_mixdown import MixdownCancelled, export_mixdown, mixdown_tracks


def _event(track, values, *, rate=4, channels=1, timestamp=0.0, sequence=0):
    payload = b"".join(struct.pack("<f", value) for value in values)
    return ({"type": "audio", "track": track, "rate": rate, "channels": channels,
             "frames": len(values) // channels, "timestamp": timestamp, "sequence": sequence}, payload)


def _read(path):
    with wave.open(str(path), "rb") as reader:
        return reader.getframerate(), reader.getnchannels(), reader.readframes(reader.getnframes())


class _Store:
    def __init__(self, sources):
        self.sources = sources
        self.root = os.path.join(tempfile.gettempdir(), "meeting-mixdown-store")
        self.reads = []

    def iter_audio(self, _session, track):
        self.reads.append(track)
        return iter(self.sources.get(track, ()))


class MeetingMixdownTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.destination = Path(self.temp.name) / "mix.wav"

    def tearDown(self):
        self.temp.cleanup()

    def test_timestamp_alignment_and_single_source_output(self):
        mic = [_event("microphone", [0.25, 0.25], timestamp=0.0)]
        system = [_event("system", [0.5, 0.5], timestamp=0.5)]
        mixdown_tracks({"microphone": mic, "system": system}, self.destination, chunk_frames=2)
        rate, channels, raw = _read(self.destination)
        self.assertEqual((rate, channels), (4, 1))
        self.assertEqual(struct.unpack("<4h", raw), (8192, 8192, 16384, 16384))

        single = Path(self.temp.name) / "single.wav"
        mixdown_tracks({"microphone": mic, "system": ()}, single, chunk_frames=1)
        self.assertEqual(struct.unpack("<2h", _read(single)[2]), (8192, 8192))

    def test_stereo_system_and_clipping_are_safe(self):
        mic = [_event("microphone", [0.8, 0.8], timestamp=0.0)]
        system = [_event("system", [0.8, 0.8, 0.8, 0.8], channels=2, timestamp=0.0)]
        mixdown_tracks({"microphone": mic, "system": system}, self.destination)
        _, channels, raw = _read(self.destination)
        self.assertEqual(channels, 2)
        self.assertEqual(struct.unpack("<4h", raw), (32767, 32767, 32767, 32767))

    def test_opt_in_mic_booster_gates_noise_and_raises_voice(self):
        source = [_event("microphone", [0.005, 0.2, -0.2], timestamp=0.0)]
        mixdown_tracks({"microphone": source}, self.destination, enhance_microphone=True)
        raw = _read(self.destination)[2]
        self.assertEqual(struct.unpack("<3h", raw), (0, 9830, -9830))

    def test_cancellation_removes_temp_and_preserves_existing_destination(self):
        self.destination.write_bytes(b"old output")
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaises(MixdownCancelled):
            mixdown_tracks({"microphone": [_event("microphone", [0.1])]}, self.destination,
                           cancel_event=cancelled)
        self.assertEqual(self.destination.read_bytes(), b"old output")
        self.assertEqual(list(Path(self.temp.name).glob(".*.tmp")), [])

    def test_different_rates_use_highest_clock_and_linear_alignment(self):
        microphone = [_event("microphone", [0.0, 1.0], rate=4)]
        system = [_event("system", [0.0, 0.0, 0.0, 0.0], rate=8)]
        mixdown_tracks({"microphone": microphone, "system": system}, self.destination, chunk_frames=2)
        rate, _, raw = _read(self.destination)
        self.assertEqual(rate, 8)
        self.assertEqual(struct.unpack("<4h", raw), (0, 16384, 32767, 32767))

    def test_fractional_native_timestamp_is_preserved(self):
        source = [_event("microphone", [0.25, 0.25], rate=4, timestamp=0.125)]
        mixdown_tracks({"microphone": source}, self.destination, chunk_frames=2)
        rate, _, raw = _read(self.destination)
        self.assertEqual(rate, 4)
        self.assertEqual(len(raw), 3 * 2)
        self.assertEqual(struct.unpack("<3h", raw), (0, 8192, 8192))

    def test_subframe_timestamp_jitter_is_snapped_without_dropping_audio(self):
        source = [
            _event("microphone", [0.1, 0.1], rate=1000, timestamp=0.0),
            _event("microphone", [0.2, 0.2], rate=1000, timestamp=0.001999),
        ]
        mixdown_tracks({"microphone": source}, self.destination, chunk_frames=2)
        rate, _, raw = _read(self.destination)
        self.assertEqual(rate, 1000)
        self.assertEqual(len(raw), 4 * 2)

    def test_material_track_overlap_is_rejected(self):
        source = [
            _event("microphone", [0.1] * 100, rate=1000, timestamp=0.0),
            _event("microphone", [0.2] * 10, rate=1000, timestamp=0.05),
        ]
        with self.assertRaisesRegex(ValueError, "sobrepostos"):
            mixdown_tracks({"microphone": source}, self.destination)

    def test_export_adapter_reads_each_source_lazily(self):
        store = _Store({"microphone": [_event("microphone", [0.1])], "system": ()})
        export_mixdown(store, "session", self.destination, chunk_frames=1)
        self.assertEqual(set(store.reads), {"microphone", "system"})
        self.assertEqual(len(_read(self.destination)[2]), 2)

    def test_large_session_is_processed_in_bounded_output_chunks(self):
        source = [_event("microphone", [0.1] * 20, timestamp=0.0)]
        mixdown_tracks({"microphone": source}, self.destination, chunk_frames=3)
        self.assertEqual(len(_read(self.destination)[2]), 40)
        self.assertEqual(os.path.getsize(self.destination), 84)


if __name__ == "__main__":
    unittest.main()
