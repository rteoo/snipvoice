import struct
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from meeting_settings import MeetingSettings, validate_hotkey_conflicts
from meeting_store import MeetingStore
from meeting_support import (
    MeetingController, WAVEFORM_POINTS, _amplitude_envelope, _final_audio_path,
)


class FakeCapture:
    def __init__(self, fail_stop=False):
        self.commands = []
        self.closed = False
        self.fail_stop = fail_stop
        self.sent_audio = False
        self.started = threading.Event()

    def start(self, settings, generation):
        self.generation = generation
        self.started.set()

    def command(self, command):
        self.commands.append(command)

    def read_event(self, timeout):
        if not self.sent_audio:
            self.sent_audio = True
            return {"type": "source_changed", "track": "system", "endpoint_id": "test-output",
                    "timestamp": 0.0, "generation": self.generation}, b""
        if self.sent_audio is True:
            self.sent_audio = "done"
            return {"type": "audio", "track": "system", "rate": 16000,
                    "channels": 1, "frames": 2, "timestamp": 0.0,
                    "sequence": 0, "generation": self.generation}, struct.pack("<2f", .25, -.5)
        if "stop" in self.commands:
            return {"type": "stopped", "generation": self.generation}, b""
        threading.Event().wait(.01)
        return None

    def stop(self, force=False):
        if self.fail_stop:
            raise RuntimeError("teardown pending")
        self.closed = True


class MeetingControllerTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).parent / "tmp"
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.voice = Mock(cache_dir=self.temp.name)
        self.voice.reserve_for_meeting.return_value = "lease"
        self.capture = FakeCapture()
        self.controller = MeetingController(self.temp.name, self.voice,
                                            capture_factory=lambda: self.capture)

    def tearDown(self):
        if self.controller.snapshot()["state"] != "unavailable":
            self.controller.shutdown()
        self.temp.cleanup()

    def test_stop_drains_audio_before_releasing_dictation(self):
        self.assertTrue(self.controller.start(MeetingSettings()))
        self.assertTrue(self.capture.started.wait(2))
        self.assertFalse(self.controller.start(MeetingSettings()))
        self.controller.stop()
        self.controller._thread.join(3)
        self.assertFalse(self.controller._thread.is_alive())
        self.assertTrue(self.capture.closed)
        self.voice.release_meeting.assert_called_once_with("lease")
        session = self.controller.snapshot()["session_id"]
        store = MeetingStore(self.temp.name)
        metadata = store.get(session)
        self.assertEqual(metadata["status"], "completed")
        self.assertEqual(len(list(store.iter_audio(session))), 1)
        self.assertEqual(next(store.iter_events(session))["type"], "source_changed")
        self.assertTrue(Path(metadata["final_audio"]["path"]).is_file())

    def test_snapshot_exposes_bounded_real_waveform_envelopes(self):
        values = [0.0, 0.25, -0.5, 0.1, 0.75, -0.2]
        self.assertEqual(_amplitude_envelope(values, 2, points=2), [0.5, 0.75])
        self.assertEqual(_amplitude_envelope([float("nan"), 2.0], 1), [0.0, 1.0])

        self.controller._waveforms["microphone"].extend([0.1] * (WAVEFORM_POINTS + 10))
        snapshot = self.controller.snapshot()
        self.assertEqual(len(snapshot["waveforms"]["microphone"]), WAVEFORM_POINTS)
        snapshot["waveforms"]["microphone"].append(1.0)
        self.assertEqual(len(self.controller.snapshot()["waveforms"]["microphone"]), WAVEFORM_POINTS)

    def test_final_audio_path_uses_local_default_and_never_overwrites(self):
        settings = MeetingSettings()
        first = _final_audio_path(Path(self.temp.name) / "meetings", settings,
                                  "session", ' Review: Q3 / next? ')
        self.assertEqual(first.name, "session - Review- Q3 - next.wav")
        self.assertEqual(first.parent, Path(self.temp.name) / "recordings")
        first.write_bytes(b"existing")
        second = _final_audio_path(Path(self.temp.name) / "meetings", settings,
                                   "session", ' Review: Q3 / next? ')
        self.assertEqual(second.name, "session - Review- Q3 - next (2).wav")

    def test_configured_final_audio_destination_must_exist_and_be_absolute(self):
        settings = MeetingSettings(destination="relative")
        with self.assertRaisesRegex(ValueError, "absoluta"):
            _final_audio_path(Path(self.temp.name) / "meetings", settings, "session")

    def test_unproven_teardown_keeps_lease_and_blocks_restart(self):
        self.capture.fail_stop = True
        self.controller.start(MeetingSettings())
        self.assertTrue(self.capture.started.wait(2))
        self.controller.stop()
        self.controller._thread.join(3)
        self.assertEqual(self.controller.snapshot()["state"], "unavailable")
        self.voice.release_meeting.assert_not_called()
        self.assertFalse(self.controller.start(MeetingSettings()))

    def test_reservation_failure_never_opens_capture(self):
        self.voice.reserve_for_meeting.side_effect = RuntimeError("busy")
        self.controller.start(MeetingSettings())
        self.controller._thread.join(2)
        self.assertFalse(self.capture.started.is_set())
        self.assertEqual(self.controller.snapshot()["error"], "busy")

    def test_cancellation_blocks_competing_capture_until_worker_finishes(self):
        entered = threading.Event()
        def work():
            entered.set()
            self.controller._cancel.wait(2)
        self.assertTrue(self.controller._launch_processing(work))
        self.assertTrue(entered.wait(1))
        self.assertFalse(self.controller.start(MeetingSettings()))
        self.controller.cancel_processing()
        self.controller._processing_thread.join(2)
        self.assertFalse(self.controller.snapshot()["processing"])

    def test_hotkeys_reject_subset_overlap_and_allow_independent_keys(self):
        with self.assertRaises(ValueError):
            validate_hotkey_conflicts({"meeting_hotkey": "ctrl+alt+shift+space"})
        validate_hotkey_conflicts({"meeting_hotkey": "ctrl+alt+r"})

    def test_failed_inference_unload_keeps_reservation(self):
        from meeting_transcription import MeetingTranscriptionError
        with patch("meeting_transcription.transcribe_meeting",
                   side_effect=MeetingTranscriptionError("still live", resource_live=True)):
            self.assertTrue(self.controller.transcribe("test-session", "balanced", "auto"))
            self.controller._processing_thread.join(2)
        self.assertEqual(self.controller.snapshot()["state"], "unavailable")
        self.voice.release_meeting.assert_not_called()

    def test_enabled_automatic_transcription_then_summary_run_after_mixdown(self):
        settings = MeetingSettings(auto_transcribe=True, auto_summary=True, voice_boost=True)
        with patch("meeting_transcription.transcribe_meeting") as transcribe, \
                patch("meeting_summary.summarize_meeting") as summarize:
            self.assertTrue(self.controller.start(settings, title="Planning"))
            self.assertTrue(self.capture.started.wait(2))
            self.controller.stop()
            self.controller._thread.join(3)

        session = self.controller.snapshot()["session_id"]
        metadata = MeetingStore(self.temp.name).get(session)
        self.assertTrue(metadata["final_audio"]["voice_boost"])
        transcribe.assert_called_once()
        summarize.assert_called_once()
        self.assertEqual(transcribe.call_args.args[1], session)
        self.assertEqual(summarize.call_args.args[1], session)
        self.assertEqual(self.controller.snapshot()["postprocess"], "Pós-processamento concluído")

    def test_auto_transcription_resource_failure_keeps_lease_and_source_audio(self):
        from meeting_transcription import MeetingTranscriptionError
        settings = MeetingSettings(auto_transcribe=True)
        with patch("meeting_transcription.transcribe_meeting",
                   side_effect=MeetingTranscriptionError("still live", resource_live=True)):
            self.controller.start(settings)
            self.assertTrue(self.capture.started.wait(2))
            self.controller.stop()
            self.controller._thread.join(3)

        session = self.controller.snapshot()["session_id"]
        self.assertEqual(MeetingStore(self.temp.name).get(session)["status"], "completed")
        self.assertEqual(self.controller.snapshot()["state"], "unavailable")
        self.voice.release_meeting.assert_not_called()

    def test_cancelled_auto_transcription_does_not_run_summary_or_report_complete(self):
        settings = MeetingSettings(auto_transcribe=True, auto_summary=True)

        def cancel_during_transcription(*_args, **_kwargs):
            self.controller._cancel.set()

        with patch("meeting_transcription.transcribe_meeting",
                   side_effect=cancel_during_transcription), \
                patch("meeting_summary.summarize_meeting") as summarize:
            self.controller.start(settings)
            self.assertTrue(self.capture.started.wait(2))
            self.controller.stop()
            self.controller._thread.join(3)

        summarize.assert_not_called()
        snapshot = self.controller.snapshot()
        self.assertEqual(snapshot["postprocess"], "Pós-processamento parcial")
        self.assertIn("cancelado", snapshot["error"])
