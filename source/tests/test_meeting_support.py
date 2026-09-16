import struct
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from meeting_settings import MeetingSettings, validate_hotkey_conflicts
from meeting_library import AnnotationConflict, MeetingLibrary
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
        self.assertRegex(
            metadata["title"],
            r"^\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-Gravacao-sem-transcricao$",
        )
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

    def test_delete_is_rejected_while_another_recording_operation_is_active(self):
        store = Mock()
        self.controller._store = store
        self.controller._processing = True
        try:
            with self.assertRaisesRegex(RuntimeError, "processamento"):
                self.controller.delete_session("finished-session")
        finally:
            self.controller._processing = False
        store.delete.assert_not_called()

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

    def test_manual_transcription_refines_only_an_automatic_title(self):
        store = self.controller.store
        session = store.begin({}, "2026-09-15-14-07-Gravacao-sem-transcricao")
        store.finish(session)
        revision = store.begin_revision(session, "balanced", "auto")
        store.add_transcript(session, revision, {
            "id": "microphone:0:1", "track": "microphone", "start": 0,
            "end": 1, "text": "Revisão do planejamento financeiro anual",
        })
        store.finish_revision(session, revision)
        with patch("meeting_transcription.transcribe_meeting"):
            self.assertTrue(self.controller.transcribe(session, "balanced", "auto"))
            self.controller._processing_thread.join(2)
        self.assertEqual(
            store.get(session)["title"],
            "2026-09-15-14-07-Revisão-Planejamento-Financeiro-Anual",
        )

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


class MeetingLibraryControllerWiringTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).parent / "tmp"
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.home = Path(self.temp.name)
        self.meeting_root = self.home / "meetings"
        self.voice = Mock(cache_dir=self.temp.name)
        self.voice.reserve_for_meeting.return_value = "lease"
        self.capture = FakeCapture()
        self.library = MeetingLibrary(self.meeting_root, workspace_root=self.home)
        self.controller = MeetingController(
            self.meeting_root,
            self.voice,
            capture_factory=lambda: self.capture,
            library=self.library,
        )

    def tearDown(self):
        if self.controller.snapshot()["state"] != "unavailable":
            self.controller.shutdown()
        self.temp.cleanup()

    def test_controller_and_library_share_one_lazy_store_and_sidecar_owns_updates(self):
        self.assertIs(self.controller.store, self.library.store)
        session = self.controller.store.begin({}, "Legacy")
        self.controller.store.finish(session)
        self.assertTrue(self.controller.update_notes(session, "Edited", "Canonical notes"))
        self.assertEqual(self.controller.get_session(session)["title"], "Edited")
        self.assertEqual(self.controller.get_session(session)["notes"], "Canonical notes")
        self.assertTrue((self.meeting_root / session / "annotations.json").is_file())

    def test_capture_finalization_does_not_open_missing_sqlite_catalog(self):
        self.assertEqual(self.controller.list_sessions(), [])
        self.assertTrue(self.controller.start(MeetingSettings()))
        self.assertTrue(self.capture.started.wait(2))
        self.controller.stop()
        self.controller._thread.join(3)
        self.assertFalse((self.home / "library.sqlite").exists())

    def test_controller_generation_cas_rejects_an_external_annotation_edit(self):
        session = self.controller.store.begin({}, "Legacy")
        self.controller.store.finish(session)
        self.controller.get_session(session)
        self.library.update_annotations(session, {"notes": "external"}, expected_generation=0)
        with self.assertRaises(AnnotationConflict) as raised:
            self.controller.update_notes(session, "Edited", "stale")
        self.assertIn("mudou", str(raised.exception))

    def test_automatic_title_does_not_overwrite_sidecar_human_title(self):
        session = self.controller.store.begin({}, "Legacy")
        self.controller.store.finish(session)
        self.library.update_annotations(session, {"title": "Human title"}, expected_generation=0)
        with patch("meeting_support.refine_recording_title", return_value="Generated title"):
            self.controller._refine_automatic_title(session)
        self.assertEqual(self.library.get_session(session)["title"], "Human title")

    def test_queued_projection_coalesces_updates_and_publishes_latest_snapshot(self):
        class ControlledIndex:
            state = "ready"

            def __init__(self, store):
                self.store = store
                self.started = threading.Event()
                self.release = threading.Event()
                self.published = []

            def index_store_session(self, _store, session_id, **_kwargs):
                title = self.store.get(session_id)["title"]
                self.published.append(title)
                if len(self.published) == 1:
                    self.started.set()
                    self.release.wait(2)
                return True

            def mark_stale(self, *_args, **_kwargs):
                return True

        session = self.controller.store.begin({}, "first")
        self.controller.store.finish(session)
        controlled = ControlledIndex(self.controller.store)
        self.library._index = controlled
        worker = self.library.queue_index_session(session)
        self.assertTrue(controlled.started.wait(2))
        self.controller.store.update(session, title="latest")
        self.assertIs(self.library.queue_index_session(session), worker)
        controlled.release.set()
        worker.join(3)
        self.assertEqual(controlled.published[-1], "latest")

    def test_delete_waits_for_projection_and_cannot_resurrect_deleted_session(self):
        class ControlledIndex:
            state = "ready"

            def __init__(self, store):
                self.store = store
                self.started = threading.Event()
                self.release = threading.Event()
                self.rows = set()

            def index_store_session(self, _store, session_id, **_kwargs):
                self.store.get(session_id)
                self.started.set()
                self.release.wait(2)
                self.rows.add(session_id)
                return True

            def remove_session(self, session_id):
                self.rows.discard(session_id)
                return True

            def mark_stale(self, *_args, **_kwargs):
                return True

        session = self.controller.store.begin({}, "to delete")
        self.controller.store.finish(session)
        controlled = ControlledIndex(self.controller.store)
        self.library._index = controlled
        worker = self.library.queue_index_session(session)
        self.assertTrue(controlled.started.wait(2))
        deleted = []
        delete_thread = threading.Thread(target=lambda: deleted.append(self.library.delete(session)))
        delete_thread.start()
        threading.Event().wait(0.05)
        controlled.release.set()
        worker.join(3)
        delete_thread.join(3)
        self.assertEqual(deleted, [True])
        self.assertNotIn(session, controlled.rows)
