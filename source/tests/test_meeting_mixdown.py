"""Focused streaming and preservation checks for meeting_mixdown."""

import os
import itertools
from pathlib import Path
import struct
import tempfile
import threading
import unittest
import warnings
import wave
from unittest import mock

from meeting_mixdown import MixdownCancelled, _adaptive_microphone_gain, export_mixdown, mixdown_tracks


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

    def test_progress_follows_a_long_timeline_and_publication_is_last(self):
        self.destination.write_bytes(b"previous final")
        reports = []
        source = [_event("system", [0.25, 0.25], timestamp=3 * 3600 - 0.5)]

        def progress(stage, done, total):
            reports.append((stage, done, total))
            if stage == "completed":
                self.assertEqual(_read(self.destination)[:2], (4, 1))
            else:
                self.assertEqual(self.destination.read_bytes(), b"previous final")

        with mock.patch("meeting_mixdown.time.monotonic", side_effect=itertools.count()):
            mixdown_tracks({"system": source}, self.destination, duration=10800,
                           progress=progress)
        encoding = [done for stage, done, _ in reports if stage == "encoding"]
        self.assertGreater(len(encoding), 2)
        self.assertEqual(encoding, sorted(encoding))
        self.assertEqual(reports[-2:], [("finalizing", 10800.0, 10800.0),
                                      ("completed", 10800.0, 10800.0)])
        self.assertEqual(len(_read(self.destination)[2]), 10800 * 4 * 2)

    def test_progress_is_throttled_but_phase_boundaries_are_always_reported(self):
        reports = []
        clock = itertools.count(step=0.01)
        with mock.patch("meeting_mixdown.time.monotonic", side_effect=clock):
            mixdown_tracks({"system": [_event("system", [0.25] * 100)]}, self.destination,
                           progress=lambda *values: reports.append(values), chunk_frames=1)
        self.assertLessEqual(sum(stage == "encoding" for stage, _, _ in reports), 5)
        self.assertEqual(reports[0], ("encoding", 0.0, None))
        self.assertEqual(reports[-2:], [("finalizing", 25.0, 25.0), ("completed", 25.0, 25.0)])

    def test_cancel_during_progress_preserves_previous_final_and_never_completes(self):
        self.destination.write_bytes(b"previous final")
        cancel = threading.Event()
        reports = []

        def progress(stage, done, total):
            reports.append(stage)
            if stage == "encoding":
                cancel.set()

        with self.assertRaises(MixdownCancelled):
            mixdown_tracks({"system": [_event("system", [0.25] * 8)]}, self.destination,
                           progress=progress, cancel_event=cancel)
        self.assertNotIn("completed", reports)
        self.assertEqual(self.destination.read_bytes(), b"previous final")
        self.assertEqual(list(Path(self.temp.name).iterdir()), [self.destination])

    def test_publication_failure_never_reports_completion(self):
        self.destination.write_bytes(b"previous final")
        reports = []
        with mock.patch("meeting_mixdown.os.replace", side_effect=OSError("cannot publish")), \
                self.assertRaisesRegex(OSError, "cannot publish"):
            mixdown_tracks({"system": [_event("system", [0.25] * 8)]}, self.destination,
                           progress=lambda *values: reports.append(values))
        self.assertEqual(reports[-1][0], "finalizing")
        self.assertEqual(self.destination.read_bytes(), b"previous final")
        self.assertEqual(list(Path(self.temp.name).iterdir()), [self.destination])

    def test_export_progress_uses_only_selected_track_end_not_session_pause(self):
        store = _Store({"system": [_event("system", [0.25, 0.25], timestamp=2.5)]})
        store.get = mock.Mock(return_value={
            "duration": 1000,
            "tracks": {"system": {"segments": [{"end": 3.0}]},
                       "microphone": {"segments": [{"end": 100.0}]}},
        })
        reports = []
        export_mixdown(store, "session", self.destination, tracks=("system",),
                       progress=lambda *values: reports.append(values))
        self.assertEqual(reports[0], ("preparing", 0.0, 3.0))
        self.assertEqual(reports[-1], ("completed", 3.0, 3.0))

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

    def test_export_can_mix_a_single_source(self):
        store = _Store({
            "microphone": [_event("microphone", [0.25, 0.25], timestamp=0.0)],
            "system": [_event("system", [0.5, 0.5], timestamp=0.0)],
        })
        export_mixdown(store, "session", self.destination, tracks=("system",), chunk_frames=2)
        self.assertEqual(store.reads, ["system"])
        self.assertEqual(struct.unpack("<2h", _read(self.destination)[2]), (16384, 16384))
        with self.assertRaises(ValueError):
            export_mixdown(store, "session", self.destination, tracks=())

    def test_summed_sources_are_limited_instead_of_clipped(self):
        mic = [_event("microphone", [0.8, 0.8], timestamp=0.0)]
        system = [_event("system", [0.8, 0.8, 0.8, 0.8], channels=2, timestamp=0.0)]
        mixdown_tracks({"microphone": mic, "system": system}, self.destination)
        _, channels, raw = _read(self.destination)
        self.assertEqual(channels, 2)
        self.assertEqual(struct.unpack("<4h", raw), (29204, 29204, 29204, 29204))

    def test_single_untouched_source_is_not_limited(self):
        mixdown_tracks({"system": [_event("system", [0.99, -1.0])]}, self.destination)
        self.assertEqual(struct.unpack("<2h", _read(self.destination)[2]), (32439, -32768))

    def test_limiter_recovers_after_a_peak(self):
        # One loud frame, then steady speech: gain drops for the peak and comes
        # back at 80 dB/s (8 dB per frame at 10 Hz) instead of snapping back.
        values = [0.5, 8.0] + [0.5] * 4
        mixdown_tracks({"microphone": [_event("microphone", values, rate=10)],
                        "system": [_event("system", [0.0] * 6, rate=10)]}, self.destination)
        samples = struct.unpack("<6h", _read(self.destination)[2])
        self.assertEqual(samples[:2], (16384, 29204))
        self.assertLess(samples[2], samples[3])
        self.assertLess(samples[3], 16384)
        self.assertEqual(samples[4:], (16384, 16384))

    def test_non_finite_samples_are_silence_and_boosted_peaks_are_limited(self):
        # A damaged sample is silence and leaves its neighbours alone, with or
        # without the boost; boosted peaks meet the limiter, not a clip.
        values = [0.25, 0.1, float("nan"), 0.1, 0.1, float("inf"), 0.1, float("-inf"),
                  2.0, -3.0, -1.0, 0.2]
        cases = (
            (False, (8192, 3277, 0, 3277, 3277, 0, 3277, 0, 32767, -32768, -32768, 6553)),
            (True, (16384, 6553, 0, 6553, 6553, 0, 6553, 0, 29204, -29204, -29204, 13107)),
        )
        for boost, expected in cases:
            with self.subTest(boost=boost), warnings.catch_warnings():
                warnings.simplefilter("error")
                mixdown_tracks({"microphone": [_event("microphone", values)]}, self.destination,
                               enhance_microphone=boost, microphone_gain=2.0)
                self.assertEqual(struct.unpack("<12h", _read(self.destination)[2]), expected)

    def test_opt_in_mic_booster_preserves_quiet_speech(self):
        source = [_event("microphone", [0.005, 0.2, -0.2], timestamp=0.0)]
        mixdown_tracks({"microphone": source}, self.destination, enhance_microphone=True)
        raw = _read(self.destination)[2]
        self.assertEqual(struct.unpack("<3h", raw), (246, 9830, -9830))

    def test_export_adapts_quiet_mic_without_clipping_a_loud_one(self):
        quiet = _Store({"microphone": [_event("microphone", [0.005] * 1000, rate=1000)]})
        export_mixdown(quiet, "session", self.destination, enhance_microphone=True)
        samples = struct.unpack("<1000h", _read(self.destination)[2])
        self.assertGreater(samples[0], 1000)
        self.assertLess(samples[0], 1500)
        self.assertEqual(quiet.reads.count("microphone"), 2)

        loud = _Store({"microphone": [_event("microphone", [0.5] * 1000, rate=1000)]})
        export_mixdown(loud, "session", self.destination, enhance_microphone=True)
        self.assertEqual(struct.unpack("<h", _read(self.destination)[2][:2])[0], 16384)

    def test_boost_survives_a_transient_and_the_transient_is_limited(self):
        # One knock no longer cancels the boost for the whole recording.
        transient = _Store({"microphone": [_event("microphone", [0.005] * 999 + [0.9], rate=1000)]})
        export_mixdown(transient, "session", self.destination, enhance_microphone=True)
        samples = struct.unpack("<1000h", _read(self.destination)[2])
        self.assertGreater(samples[0], 1000)
        self.assertLess(samples[0], 1500)
        self.assertEqual(samples[-1], 29204)

    def test_boost_raises_your_voice_toward_a_louder_call(self):
        mic = [_event("microphone", [0.05] * 1000, rate=1000)]
        loud_call = [_event("system", [0.2] * 2000, rate=1000, channels=2)]
        quiet_call = [_event("system", [0.01] * 2000, rate=1000, channels=2)]
        alone = _adaptive_microphone_gain(iter(mic), None)
        balanced = _adaptive_microphone_gain(iter(mic), None, system_source=iter(loud_call))
        # About 1.3x toward -24 dBFS alone; about 4.5x to meet the call's level.
        self.assertAlmostEqual(alone, 1.26, places=2)
        self.assertAlmostEqual(balanced, 4.47, places=2)
        # A quieter call never pulls your voice below the usual target.
        self.assertEqual(_adaptive_microphone_gain(iter(mic), None, system_source=iter(quiet_call)), alone)

    def test_export_balances_the_boost_against_the_call(self):
        store = _Store({
            "microphone": [_event("microphone", [0.05] * 1000, rate=1000)],
            "system": [_event("system", [0.2] * 1000, rate=1000)],
        })
        export_mixdown(store, "session", self.destination, enhance_microphone=True,
                       tracks=("microphone",))
        mic_only = struct.unpack("<h", _read(self.destination)[2][:2])[0]
        store.reads.clear()
        export_mixdown(store, "session", self.destination, enhance_microphone=True)
        self.assertIn("system", store.reads)
        # Both sources sum; the boosted mic is well above its unbalanced level.
        mixed = struct.unpack("<h", _read(self.destination)[2][:2])[0]
        self.assertGreater(mixed - round(0.2 * 32767), 2 * mic_only)

    def test_export_leaves_near_silence_unboosted(self):
        silent = _Store({"microphone": [_event("microphone", [0.001] * 1000, rate=1000)]})
        export_mixdown(silent, "session", self.destination, enhance_microphone=True)
        self.assertLess(struct.unpack("<h", _read(self.destination)[2][:2])[0], 100)

    def test_adjustment_keeps_quiet_speech_audible_in_a_mostly_silent_track(self):
        source = _Store({"microphone": [
            _event("microphone", [0.0] * 1900 + [0.005] * 100, rate=1000),
        ]})
        export_mixdown(source, "session", self.destination, enhance_microphone=True)
        samples = struct.unpack("<2000h", _read(self.destination)[2])
        self.assertEqual(samples[:1900], (0,) * 1900)
        self.assertGreater(samples[-1], 1000)
        self.assertLess(samples[-1], 1500)

    def test_adjustment_is_independent_of_native_packet_sizes(self):
        values = [0.0] * 90 + [0.02] * 10
        contiguous = _Store({"microphone": [_event("microphone", values, rate=1000)]})
        fragmented = _Store({"microphone": [
            _event("microphone", values[index:index + 10], rate=1000,
                   timestamp=index / 1000, sequence=index // 10)
            for index in range(0, len(values), 10)
        ]})
        export_mixdown(contiguous, "session", self.destination, enhance_microphone=True)
        expected = _read(self.destination)[2]
        export_mixdown(fragmented, "session", self.destination, enhance_microphone=True)
        self.assertEqual(_read(self.destination)[2], expected)

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
        microphone = [_event("microphone", [0.0, 0.5], rate=4)]
        system = [_event("system", [0.0, 0.0, 0.0, 0.0], rate=8)]
        mixdown_tracks({"microphone": microphone, "system": system}, self.destination, chunk_frames=2)
        rate, _, raw = _read(self.destination)
        self.assertEqual(rate, 8)
        self.assertEqual(struct.unpack("<4h", raw), (0, 8192, 16384, 16384))

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


if __name__ == "__main__":
    unittest.main()
