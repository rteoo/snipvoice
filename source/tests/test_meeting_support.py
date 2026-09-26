import struct
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from meeting_settings import MeetingSettings, validate_hotkey_conflicts
from meeting_library import AnnotationConflict, MeetingLibrary
from meeting_retention import ConfirmationRequired, OperationRecoveryError
from meeting_store import MeetingStore
from meeting_mixdown import MixdownCancelled, export_mixdown
from meeting_support import (
    MeetingController, REPORT_HISTORY_LIMIT, WAVEFORM_POINTS, _amplitude_envelope,
    _final_audio_path,
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


class FakeClock:
    def __init__(self, value=0.0):
        self.value = value

    def __call__(self):
        return self.value


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
        # Controller tests exercise orchestration and metadata independently
        # of the native codec. Real encoding is covered by the codec suite.
        def synthetic_mixdown(_store, _session, path, **_options):
            Path(path).write_bytes(b"synthetic encoded audio")
            return str(path)
        encoder = patch("meeting_support.export_mixdown", side_effect=synthetic_mixdown)
        self.final_export = encoder.start()
        self.addCleanup(encoder.stop)

    def tearDown(self):
        if self.controller.snapshot()["state"] != "unavailable":
            self.controller.shutdown()
        self.temp.cleanup()

    def _quiet_recording(self):
        root = Path(self.temp.name) / "adjustment"
        store = MeetingStore(root / "meetings")
        session = store.begin({"meeting_voice_boost": False}, "Quiet recording")
        payload = struct.pack("<1000f", *([0.005] * 1000))
        store.append_audio(session, {
            "type": "audio", "generation": 0, "track": "microphone", "sequence": 0,
            "timestamp": 0.0, "rate": 8000, "channels": 1, "frames": 1000,
        }, payload)
        store.finish(session)
        previous = root / "previous.wav"
        export_mixdown(store, session, previous)
        store.save_final_audio(session, previous)
        controller = MeetingController(str(root / "meetings"), self.voice, store=store)
        self.addCleanup(controller.shutdown)
        controller._queue_projection = Mock()
        return controller, store, session, previous, payload

    def test_adjust_final_audio_preserves_originals_and_publishes_adjusted_copy(self):
        controller, store, session, previous, payload = self._quiet_recording()
        previous_bytes = previous.read_bytes()
        self.assertTrue(controller.regenerate_final_audio(session))
        controller._processing_thread.join(3)
        self.assertFalse(controller._processing_thread.is_alive())
        self.assertFalse(controller.snapshot()["error"])
        final = store.get(session)["final_audio"]
        self.assertTrue(final["voice_boost"])
        self.assertNotEqual(Path(final["path"]), previous)
        self.assertEqual(Path(final["path"]).parent, previous.parent)
        self.assertEqual(previous.read_bytes(), previous_bytes)
        self.assertEqual(next(store.iter_audio(session, "microphone"))[1], payload)
        self.assertEqual(Path(final["path"]).suffix, ".mp3")
        self.assertTrue(self.final_export.call_args.kwargs["enhance_microphone"])
        controller._queue_projection.assert_called_once_with(session)

    def test_adjust_final_audio_cancellation_keeps_previous_selection(self):
        controller, store, session, previous, _ = self._quiet_recording()
        before = store.get(session)["final_audio"]
        previous_bytes = previous.read_bytes()
        with patch("meeting_support.export_mixdown", side_effect=MixdownCancelled("cancelled")):
            self.assertTrue(controller.regenerate_final_audio(session))
            controller._processing_thread.join(3)
        self.assertEqual(store.get(session)["final_audio"], before)
        self.assertEqual(previous.read_bytes(), previous_bytes)
        self.assertIn("cancelled", controller.snapshot()["error"])

    def test_adjust_final_audio_obeys_recording_playback_and_retention_reservations(self):
        for name, value in (("_state", "recording"), ("_processing", True),
                            ("_playback_active", True), ("_retention_active", True)):
            with self.subTest(reservation=name), patch.object(self.controller, name, value):
                self.assertFalse(self.controller.regenerate_final_audio("synthetic"))
        with patch.object(self.controller, "_play_thread", Mock(is_alive=Mock(return_value=True))):
            self.assertFalse(self.controller.regenerate_final_audio("synthetic"))

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
        self.assertEqual(Path(metadata["final_audio"]["path"]).suffix, ".mp3")

    def test_missing_mp3_encoder_preserves_recording_and_releases_capture(self):
        self.final_export.side_effect = RuntimeError("MP3 encoder unavailable")
        self.assertTrue(self.controller.start(MeetingSettings()))
        self.assertTrue(self.capture.started.wait(2))
        self.controller.stop()
        self.controller._thread.join(3)
        snapshot = self.controller.snapshot()
        self.assertEqual(snapshot["state"], "idle")
        self.assertIn("MP3 encoder unavailable", snapshot["error"])
        metadata = self.controller.store.get(snapshot["session_id"])
        self.assertEqual(metadata["status"], "completed")
        self.assertIsNone(metadata["final_audio"])
        self.assertEqual(len(list(self.controller.store.iter_audio(snapshot["session_id"]))), 1)
        self.voice.release_meeting.assert_called_once_with("lease")

    def test_snapshot_exposes_bounded_real_waveform_envelopes(self):
        values = [0.0, 0.25, -0.5, 0.1, 0.75, -0.2]
        self.assertEqual(_amplitude_envelope(values, 2, points=2), [0.5, 0.75])
        self.assertEqual(_amplitude_envelope([float("nan"), 2.0], 1), [0.0, 1.0])

        self.controller._waveforms["microphone"].extend([0.1] * (WAVEFORM_POINTS + 10))
        snapshot = self.controller.snapshot()
        self.assertEqual(len(snapshot["waveforms"]["microphone"]), WAVEFORM_POINTS)
        snapshot["waveforms"]["microphone"].append(1.0)
        self.assertEqual(len(self.controller.snapshot()["waveforms"]["microphone"]), WAVEFORM_POINTS)

    def test_source_preview_measures_audio_without_saving_a_meeting(self):
        result = self.controller.preview_sources(MeetingSettings(), seconds=0.03)
        self.assertEqual(result["enabled"], ("microphone", "system"))
        self.assertEqual(result["peaks"], {"microphone": 0.0, "system": 0.5})
        self.assertEqual(result["errors"], ())
        self.assertTrue(self.capture.closed)
        self.assertIsNone(self.controller._store)
        self.assertEqual(self.controller.snapshot()["state"], "idle")
        self.assertFalse(self.controller.snapshot()["previewing"])
        self.assertTrue(self.controller.snapshot()["waveforms"]["system"])
        self.voice.release_meeting.assert_called_once_with("lease")

    def test_source_preview_blocks_recording_until_capture_stops(self):
        errors = []

        def preview():
            try:
                self.controller.preview_sources(MeetingSettings(), seconds=0.2)
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=preview)
        worker.start()
        self.assertTrue(self.capture.started.wait(1))
        self.assertTrue(self.controller.snapshot()["previewing"])
        self.assertFalse(self.controller.start(MeetingSettings()))
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])

    def test_source_preview_keeps_reservation_if_teardown_is_unproven(self):
        self.capture.fail_stop = True
        with self.assertRaisesRegex(RuntimeError, "teardown pending"):
            self.controller.preview_sources(MeetingSettings(), seconds=0.03)
        self.assertEqual(self.controller.snapshot()["state"], "unavailable")
        self.voice.release_meeting.assert_not_called()

    def test_final_audio_path_uses_local_default_and_never_overwrites(self):
        settings = MeetingSettings()
        first = _final_audio_path(Path(self.temp.name) / "meetings", settings,
                                  "session", ' Review: Q3 / next? ')
        self.assertEqual(first.name, "session - Review- Q3 - next.mp3")
        self.assertEqual(first.parent, Path(self.temp.name) / "recordings")
        first.write_bytes(b"existing")
        second = _final_audio_path(Path(self.temp.name) / "meetings", settings,
                                   "session", ' Review: Q3 / next? ')
        self.assertEqual(second.name, "session - Review- Q3 - next (2).mp3")

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
        with patch.object(self.controller, "_installed_voice_profile", return_value="balanced"), \
                patch.object(self.controller, "_installed_summary_model", return_value="qwen3.5-2b-q4"), \
                patch("meeting_transcription.transcribe_meeting") as transcribe, \
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

    def test_installed_models_run_automatically_when_legacy_switches_are_false(self):
        settings = MeetingSettings()
        with patch.object(self.controller, "_installed_voice_profile", return_value="balanced"), \
                patch.object(self.controller, "_installed_summary_model", return_value="local-summary"), \
                patch("meeting_transcription.transcribe_meeting") as transcribe, \
                patch("meeting_summary.summarize_meeting") as summarize:
            self.assertTrue(self.controller.start(settings, title="Planning"))
            self.assertTrue(self.capture.started.wait(2))
            self.controller.stop()
            self.controller._thread.join(3)

        session = self.controller.snapshot()["session_id"]
        transcribe.assert_called_once()
        summarize.assert_called_once()
        self.assertEqual(transcribe.call_args.args[1:3], (session, "balanced"))
        self.assertEqual(summarize.call_args.args[1:3], (session, "local-summary"))

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

    def test_manual_transcription_runs_installed_summary_for_existing_recording(self):
        store = self.controller.store
        session = store.begin({}, "Existing")
        store.finish(session)
        with patch("meeting_transcription.transcribe_meeting"), \
                patch.object(self.controller, "_installed_summary_model", return_value="local-summary"), \
                patch("meeting_summary.summarize_meeting") as summarize:
            self.assertTrue(self.controller.transcribe(session, "balanced", "auto"))
            self.controller._processing_thread.join(2)
        summarize.assert_called_once_with(
            store, session, "local-summary", cancel_event=self.controller._cancel,
        )

    def test_direct_summary_uses_installed_fallback_and_rejects_missing_models(self):
        with patch.object(self.controller, "_installed_summary_model", return_value="fallback"), \
                patch("meeting_summary.summarize_meeting") as summarize:
            self.assertTrue(self.controller.summarize("session", "preferred"))
            self.controller._processing_thread.join(2)
        summarize.assert_called_once()
        with patch.object(self.controller, "_installed_summary_model", return_value=None):
            self.assertTrue(self.controller.summarize("session", "preferred"))
            self.controller._processing_thread.join(2)
        self.assertIn("Nenhum modelo de resumo instalado", self.controller.snapshot()["error"])

    def test_imported_audio_enters_automatic_transcription_workflow(self):
        settings = MeetingSettings()
        with patch("meeting_files.import_audio", return_value="imported"), \
                patch.object(self.controller, "_installed_voice_profile", return_value="balanced"), \
                patch.object(self.controller, "_transcribe_and_summarize") as process:
            self.assertEqual(self.controller.import_audio("recording.wav", settings), "imported")
        process.assert_called_once_with("imported", "balanced", settings.language, settings.summary_model)

    def test_imported_audio_without_installed_voice_model_skips_inference(self):
        settings = MeetingSettings()
        with patch("meeting_files.import_audio", return_value="imported"), \
                patch.object(self.controller, "_installed_voice_profile", return_value=None), \
                patch.object(self.controller, "_transcribe_and_summarize") as process:
            self.assertEqual(self.controller.import_audio("recording.wav", settings), "imported")
        process.assert_not_called()

    def test_auto_transcription_resource_failure_keeps_lease_and_source_audio(self):
        from meeting_transcription import MeetingTranscriptionError
        settings = MeetingSettings(auto_transcribe=True)
        with patch.object(self.controller, "_installed_voice_profile", return_value="balanced"), \
                patch("meeting_transcription.transcribe_meeting",
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

        with patch.object(self.controller, "_installed_voice_profile", return_value="balanced"), \
                patch.object(self.controller, "_installed_summary_model", return_value="qwen3.5-2b-q4"), \
                patch("meeting_transcription.transcribe_meeting",
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

    def test_transcript_pages_are_bounded_and_navigable_past_the_legacy_limit(self):
        session = self.controller.store.begin(
            {"tracks": {"microphone": {"segments": []}}, "duration": 600}, "Long"
        )
        self.controller.store.finish(session)
        revision = self.controller.store.begin_revision(session, "balanced", "auto")
        for index in range(505):
            self.controller.store.add_transcript(session, revision, {
                "id": f"microphone:{index}:1", "track": "microphone",
                "start": float(index), "end": float(index + 1), "text": str(index),
            })
        self.controller.store.finish_revision(session, revision)

        first = self.controller.get_transcript_page(session, revision, 0, 100)
        last = self.controller.get_transcript_page(session, revision, 500, 100)
        self.assertEqual(len(first["segments"]), 100)
        self.assertTrue(first["has_more"])
        self.assertEqual(first["segments"][0]["id"], "microphone:0:1")
        self.assertEqual(len(last["segments"]), 5)
        self.assertFalse(last["has_more"])
        self.assertEqual(last["segments"][-1]["id"], "microphone:504:1")

    def test_transcript_preview_formats_complete_revision_without_page_limit(self):
        session = self.controller.store.begin({}, "Preview")
        self.controller.store.finish(session)
        revision = self.controller.store.begin_revision(session, "balanced", "auto")
        self.controller.store.add_transcript(session, revision, {
            "id": "microphone:0:1", "track": "microphone", "start": 0,
            "end": 1, "text": "First sentence.",
        })
        self.controller.store.add_transcript(session, revision, {
            "id": "microphone:1:1", "track": "microphone", "start": 1,
            "end": 2, "text": "Second sentence.",
        })
        self.controller.store.finish_revision(session, revision)

        readable = self.controller.get_transcript_preview(session)
        timestamped = self.controller.get_transcript_preview(session, style="timestamped")
        self.assertEqual(readable, {"text": "First sentence. Second sentence.", "truncated": False})
        self.assertIn("00:00:00 Microfone: First sentence.", timestamped["text"])

    def test_playback_progress_is_controller_owned_and_stale_completion_is_ignored(self):
        clock = FakeClock()
        first_started = threading.Event()
        release_first = threading.Event()
        second_started = threading.Event()
        calls = []

        def fake_play(_store, _session, _track, _start, stop_event):
            calls.append(len(calls) + 1)
            if calls[-1] == 1:
                first_started.set()
                release_first.wait(2)
            else:
                second_started.set()
                stop_event.wait(2)

        with patch("meeting_files.play_audio", side_effect=fake_play):
            playback = MeetingController(
                self.temp.name, self.voice, capture_factory=lambda: self.capture, clock=clock,
            )
            self.assertTrue(playback.play("one", "microphone", start=4.0))
            self.assertTrue(first_started.wait(1))
            clock.value = 3.0
            self.assertEqual(playback.snapshot()["playback"]["position"], 7.0)
            first_generation = playback.snapshot()["playback"]["generation"]
            playback.stop_playback()
            release_first.set()
            playback._play_thread.join(2)
            self.assertFalse(playback.snapshot()["playback"]["active"])
            self.assertGreater(playback.snapshot()["playback"]["generation"], first_generation)

            self.assertTrue(playback.play("two", "system", start=10.0))
            self.assertTrue(second_started.wait(1))
            clock.value = 4.0
            state = playback.snapshot()["playback"]
            self.assertEqual(state["session_id"], "two")
            self.assertEqual(state["track"], "system")
            self.assertEqual(state["position"], 11.0)
            playback.stop_playback()
            playback._play_thread.join(2)
            playback.shutdown()

    def test_seek_replaces_active_playback_after_old_stream_stops(self):
        first_started = threading.Event()
        second_started = threading.Event()
        calls = []

        def fake_play(_store, session, track, start, stop_event):
            calls.append((session, track, start))
            (first_started if len(calls) == 1 else second_started).set()
            stop_event.wait(2)

        with patch("meeting_files.play_audio", side_effect=fake_play):
            self.assertTrue(self.controller.play("meeting", "microphone", start=2.0))
            self.assertTrue(first_started.wait(1))
            before = self.controller.snapshot()["playback"]["generation"]
            with self.assertRaisesRegex(ValueError, "instante de reprodução válido"):
                self.controller.seek_playback("meeting", "system", start=-1.0)
            self.assertTrue(self.controller.snapshot()["playback"]["active"])
            self.assertTrue(self.controller.seek_playback("meeting", "system", start=18.0))
            self.assertTrue(second_started.wait(1))
            current = self.controller.snapshot()["playback"]
            self.assertGreater(current["generation"], before)
            self.assertEqual((current["track"], current["start"]), ("system", 18.0))
            self.assertEqual(calls, [
                ("meeting", "microphone", 2.0), ("meeting", "system", 18.0),
            ])
            self.controller.stop_playback()
            self.controller._play_thread.join(2)


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

    def test_library_page_and_search_seams_forward_bounded_filters(self):
        page = {"items": [], "next_cursor": None, "cursor_reset": False}
        with patch.object(self.library, "list_sessions_page", return_value=page) as reader, \
                patch.object(self.library, "search", return_value=[]) as search:
            self.assertIs(
                self.controller.list_sessions_page(
                    limit=10, cursor="c1", query="Friday", status="completed",
                    collection="project-1", tag="planning", person="Alice",
                    series="weekly", date_from="2026-09-01", date_to="2026-09-30",
                ),
                page,
            )
            self.assertEqual(self.controller.search_library("Friday", limit=5, tag="planning"), [])
        self.assertEqual(reader.call_args.kwargs["cursor"], "c1")
        self.assertEqual(reader.call_args.kwargs["collection"], "project-1")
        self.assertEqual(search.call_args.kwargs, {"limit": 5, "offset": 0, "tag": "planning"})

    def test_rename_preserves_legacy_notes_and_bookmarks(self):
        session = self.controller.store.begin({}, "Legacy")
        self.controller.store.finish(session)
        bookmarks = [{"timestamp": 1.0, "label": "Keep this bookmark"}]
        self.controller.update_notes(session, "Old title", "Keep these notes", bookmarks)

        self.assertTrue(self.controller.rename_session(session, "New title"))

        metadata = self.controller.get_session(session)
        self.assertEqual(metadata["title"], "New title")
        self.assertEqual(metadata["notes"], "Keep these notes")
        self.assertEqual(metadata["bookmarks"], bookmarks)
        self.assertTrue(self.controller.rename_session(session, "Another title"))

    def test_rename_rejects_stale_annotation_generation(self):
        session = self.controller.store.begin({}, "Original")
        self.controller.store.finish(session)
        self.controller.get_session(session)
        self.library.update_annotations(session, {"title": "External edit"}, expected_generation=0)

        with self.assertRaises(AnnotationConflict):
            self.controller.rename_session(session, "Stale edit")
        self.assertEqual(self.library.get_session(session)["title"], "External edit")

    def test_rebuild_index_reports_progress_and_surfaces_cancellation_state(self):
        progress = []

        def cancelled_rebuild(*, cancel_event, progress):
            progress(1, 3)
            cancel_event.set()
            raise RuntimeError("rebuild cancelled")

        cancel = threading.Event()
        with patch.object(self.library, "reconcile", side_effect=cancelled_rebuild):
            with self.assertRaisesRegex(RuntimeError, "rebuild cancelled"):
                self.controller.rebuild_index(cancel_event=cancel, progress=lambda done, total: progress.append((done, total)))
        self.assertEqual(progress, [(1, 3)])
        self.assertEqual(self.controller.rebuild_progress()["state"], "cancelled")

        with patch.object(
            self.library, "reconcile", return_value={"state": "ready", "sessions": 2},
        ) as rebuild:
            result = self.controller.rebuild_index(progress=lambda *_args: None)
        rebuild.assert_called_once()
        self.assertEqual(result["state"], "ready")
        self.assertEqual(self.controller.rebuild_progress(), {
            "done": 2, "total": 2, "state": "ready",
        })

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

    def test_controller_annotation_helpers_keep_revision_scope_and_generation_cas(self):
        session = self.controller.store.begin({}, "Annotated")
        self.controller.store.append_audio(session, {
            "type": "audio", "track": "microphone", "rate": 16000, "channels": 1, "frames": 16000,
            "timestamp": 0.0, "sequence": 0, "generation": 0,
        }, b"\0" * (16000 * 4))
        self.controller.store.finish(session)
        revision = self.controller.store.begin_revision(session, "balanced", "auto")
        self.controller.store.add_transcript(session, revision, {
            "id": "microphone:0:1", "track": "microphone", "start": 0.0,
            "end": 1.0, "text": "hello",
        })
        self.controller.store.finish_revision(session, revision)
        self.controller.get_session(session)

        labeled = self.controller.set_speaker_label(
            session, revision, "microphone:0:1", "Pessoa 1",
        )
        self.assertEqual(labeled["generation"], 1)
        highlighted = self.controller.add_highlight(
            session, revision, 0.1, 0.9, "microphone", ["microphone:0:1"],
            label="Decisão", note="revisar",
        )
        self.assertEqual(highlighted["generation"], 2)
        self.assertEqual(highlighted["highlights"][0]["note"], "revisar")
        self.controller.delete_highlight(session, highlighted["highlights"][0]["id"])
        self.assertEqual(self.controller.get_annotations(session)["generation"], 3)

    def test_report_history_is_bounded_and_drops_full_payloads_before_gui(self):
        rows = [
            {
                "id": f"report-{index}",
                "kind": "report",
                "profile_id": "general",
                "generated": {"summary": "secret " * 2000},
                "payload": {"secret": "secret " * 2000},
                "reviewed_artifact": {"sections": {"summary": "secret"}},
            }
            for index in range(REPORT_HISTORY_LIMIT * 2 + 37)
        ]
        with patch.object(self.library, "list_report_metadata", return_value=rows) as reader:
            result = self.controller.list_reports("session")

        reader.assert_called_once_with(
            "session", include_legacy=True, limit=REPORT_HISTORY_LIMIT,
            cancel_event=self.controller._cancel,
        )
        self.assertEqual(len(result), REPORT_HISTORY_LIMIT)
        self.assertTrue(all("generated" not in item for item in result))
        self.assertTrue(all("payload" not in item for item in result))
        self.assertTrue(all("reviewed_artifact" not in item for item in result))

    def test_controller_ask_result_can_be_saved_without_metadata_leaking_to_answer(self):
        intelligence = Mock()
        intelligence.ask_this_meeting.return_value = {
            "answer": "A decisão foi aprovada.",
            "citations": ["segment-1"],
            "uncertainty": "low",
            "_provenance": {
                "revision": "revision-1",
                "model": {
                    "id": "qwen3.5-2b-q4",
                    "sha256": "a" * 64,
                    "runtime": "llama.cpp",
                    "context_limit": 4096,
                },
            },
        }
        intelligence.save_answer.return_value = {"kind": "qa", "id": "qa-1"}
        with patch.object(self.controller, "_intelligence", return_value=intelligence):
            answer = self.controller.ask_this_meeting(
                "session", "Qual foi a decisão?", "qwen3.5-2b-q4", revision="revision-1",
            )
            self.controller.save_answer(
                "session", answer, "different-current-model", question="Qual foi a decisão?",
            )

        clean_answer = intelligence.save_answer.call_args.args[1]
        self.assertEqual(
            clean_answer,
            {
                "answer": "A decisão foi aprovada.",
                "citations": ["segment-1"],
                "uncertainty": "low",
            },
        )
        self.assertNotIn("revision", clean_answer)
        self.assertEqual(intelligence.save_answer.call_args.kwargs["revision"], "revision-1")
        self.assertEqual(
            intelligence.save_answer.call_args.kwargs["provenance"],
            answer["_provenance"],
        )

    def test_controller_forwards_bounded_follow_up_history(self):
        intelligence = Mock()
        intelligence.ask_this_meeting.return_value = {
            "answer": "A decisão foi aprovada.", "citations": [], "uncertainty": "high",
            "_provenance": {"revision": "revision-1", "model": {}},
        }
        history = [{"question": "O que foi decidido?", "answer": "A aprovação."}]
        with patch.object(self.controller, "_intelligence", return_value=intelligence):
            self.controller.ask_this_meeting(
                "session", "E o prazo?", "qwen3.5-2b-q4", history=history,
            )
        self.assertEqual(intelligence.ask_this_meeting.call_args.kwargs["history"], history)

    def test_highlight_clip_export_uses_file_worker_bridge_operation(self):
        with patch("meeting_files.export_highlight_clip", return_value="clip.wav") as export:
            result = self.controller.export_highlight_clip(
                "session", {"id": "highlight-1", "track": "microphone",
                             "start": 0.0, "end": 1.0}, "clip.wav",
            )
        self.assertEqual(result, "clip.wav")
        export.assert_called_once()
        self.assertIsNotNone(export.call_args.kwargs["cancel_event"])

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

    def test_direct_library_delete_fails_closed_without_touching_canonical_or_index(self):
        session = self.controller.store.begin({}, "to delete")
        self.controller.store.finish(session)
        controlled = Mock()
        self.library._index = controlled
        with self.assertRaisesRegex(RuntimeError, "lixeira recuperável"):
            self.library.delete(session)
        self.assertEqual(self.controller.store.get(session)["id"], session)
        controlled.remove_session.assert_not_called()


class MeetingControllerRetentionPrivacyTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).parent / "tmp"
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.home = Path(self.temp.name)
        shutil.copytree(
            Path(__file__).parent / "fixtures" / "meeting-v1",
            self.home / "meetings" / "fixture-meeting-v1",
        )
        self.voice = Mock(cache_dir=self.temp.name)
        self.library = MeetingLibrary(self.home / "meetings", workspace_root=self.home)
        self.controller = MeetingController(
            self.home / "meetings", self.voice, library=self.library,
        )

    def tearDown(self):
        if self.controller.snapshot()["state"] != "unavailable":
            self.controller.shutdown()
        self.temp.cleanup()

    def test_delete_is_recoverable_and_purge_needs_second_confirmation(self):
        result = self.controller.delete_session("fixture-meeting-v1")
        self.assertEqual(result.state, "trashed")
        self.assertFalse((self.home / "meetings" / "fixture-meeting-v1").exists())
        self.assertEqual(self.controller.list_trash()[0].session_id, "fixture-meeting-v1")
        self.controller.restore_session("fixture-meeting-v1")
        self.assertTrue((self.home / "meetings" / "fixture-meeting-v1").exists())
        self.controller.delete_session("fixture-meeting-v1")
        with self.assertRaises(ConfirmationRequired):
            self.controller.purge_session("fixture-meeting-v1")
        purged = self.controller.purge_session("fixture-meeting-v1", confirm=True)
        self.assertEqual(purged.state, "purged")

    def test_retention_admission_rejects_processing_and_memory_only_refuses_save(self):
        self.controller._processing = True
        try:
            with self.assertRaisesRegex(RuntimeError, "processamento"):
                self.controller.retention_plan("fixture-meeting-v1")
        finally:
            self.controller._processing = False
        self.library.update_workspace(
            {"privacy_defaults": {"qa_mode": "memory_only"}}, expected_generation=0,
        )
        self.controller.refresh_privacy_defaults()
        with self.assertRaisesRegex(ValueError, "memory_only"):
            self.controller.save_answer(
                "fixture-meeting-v1", {"answer": "local"}, "model",
            )

    def test_workspace_adapter_merges_nested_settings_and_refreshes_privacy_cache(self):
        initial = self.controller.read_workspace()
        first = self.controller.update_workspace(
            {
                "privacy_defaults": {
                    "qa_mode": "memory_only",
                    "future_setting": {"enabled": True},
                },
                "retention_defaults": {"whole_meeting": {"after_days": 7}},
            },
            expected_generation=initial["generation"],
        )
        self.assertEqual(first["generation"], initial["generation"] + 1)
        self.assertEqual(self.controller.privacy_defaults()["qa_mode"], "memory_only")
        self.assertTrue(self.controller.privacy_defaults()["future_setting"]["enabled"])

        second = self.controller.update_workspace(
            {"privacy_defaults": {"recording_notice": {"enabled": True}}},
            expected_generation=first["generation"],
        )
        self.assertTrue(second["privacy_defaults"]["recording_notice"]["enabled"])
        self.assertTrue(second["privacy_defaults"]["future_setting"]["enabled"])
        self.assertEqual(second["retention_defaults"]["whole_meeting"]["mode"], "whole_meeting")

    def test_retention_lease_checker_does_not_self_block_its_own_operation(self):
        plan = self.controller.retention_plan("fixture-meeting-v1")
        self.assertTrue(plan.eligible)
        self.assertFalse(self.controller.retention_active())

    def test_raw_only_legacy_workspace_defaults_whole_meeting_policy_to_keep(self):
        workspace = {
            "retention_defaults": {
                "raw_audio": {"mode": "keep", "tracks": []},
                "trash_days": 30,
            },
        }
        with patch.object(self.library, "read_workspace", return_value=workspace):
            plan = self.controller.retention_plan("fixture-meeting-v1")
        self.assertEqual(plan.operation, "keep")
        self.assertTrue(plan.eligible)

    def test_startup_recovery_surfaces_manual_reconciliation_and_clears_admission(self):
        with patch.object(
            self.controller._retention(), "recover_operations",
            side_effect=OperationRecoveryError("manual reconciliation required"),
        ):
            with self.assertRaises(OperationRecoveryError):
                self.controller.recover_retention_operations()
        self.assertFalse(self.controller.retention_active())
