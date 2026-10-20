import hashlib
import json
import struct
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from meeting_settings import MeetingSettings
from meeting_audio import NativeCapture
from meeting_library import AnnotationConflict, MeetingLibrary
from meeting_retention import ConfirmationRequired, OperationRecoveryError
from meeting_store import MeetingStore, TrackUnavailableError
from meeting_mixdown import MixdownCancelled, export_mixdown
from meeting_storage import compact_capture
from meeting_support import (
    JOB_LABELS, MeetingController, REPORT_HISTORY_LIMIT, WAVEFORM_POINTS, _amplitude_envelope,
    _final_audio_path, processing_job_label, start_blocker,
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
        def synthetic_verification(path, expected_duration=None, **_options):
            path = Path(path)
            value = path.stat()
            return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "size": value.st_size, "mtime_ns": value.st_mtime_ns,
                    "duration": expected_duration or .000125, "frames": 2, "tolerance": .05}
        verifier = patch("meeting_support.verify_mp3_file", side_effect=synthetic_verification)
        verifier.start()
        self.addCleanup(verifier.stop)

    def tearDown(self):
        if self.controller.snapshot()["state"] != "unavailable":
            self.controller.shutdown()
        self.temp.cleanup()

    def test_generate_report_forwards_focus_and_session_target(self):
        intelligence = Mock()
        intelligence.generate_report.return_value = {"summary": "ok"}
        with patch.object(self.controller, "_intelligence", return_value=intelligence):
            self.controller.generate_report(
                "session", "model", profile="meeting_notes", focus="Priorize decisões.",
            )
        self.assertEqual(intelligence.generate_report.call_args.kwargs["focus"], "Priorize decisões.")

    def test_final_export_reaches_100_only_after_file_and_metadata_are_saved(self):
        session = self.controller.store.begin({})
        self.controller.store.finish(session)
        seen = []

        def encode(_store, _session, path, *, progress, **_kwargs):
            progress("preparing", 0, 60)
            progress("encoding", 30, 60)
            snap = self.controller.snapshot()
            self.assertEqual(snap["audio_export"]["percent"], 50)
            snap["audio_export"]["percent"] = -1
            self.assertEqual(self.controller.snapshot()["audio_export"]["percent"], 50)
            progress("encoding", 60, 60)
            self.assertEqual(self.controller.snapshot()["audio_export"]["percent"], 99)
            Path(path).write_bytes(b"synthetic encoded audio")
            progress("completed", 60, 60)
            return str(path)

        original = self.controller.store.save_final_audio

        def save(*args, **kwargs):
            seen.append(self.controller.snapshot()["audio_export"])
            self.assertTrue(Path(args[1]).exists())
            self.assertEqual(seen[-1]["stage"], "finalizing")
            self.assertEqual(seen[-1]["percent"], 99)
            return original(*args, **kwargs)

        with tempfile.TemporaryDirectory(dir=Path(self.temp.name).parent) as folder, \
                patch("meeting_support.export_mixdown", side_effect=encode), \
                patch.object(self.controller.store, "save_final_audio", side_effect=save):
            output = self.controller._export_audio(session, Path(folder) / "final.mp3", save_final=True)
            snap = self.controller.snapshot()
            self.assertEqual(snap["audio_export"]["stage"], "completed")
            self.assertEqual(snap["audio_export"]["percent"], 100)
            self.assertEqual(snap["final_audio"], output)
            self.assertEqual(self.controller.store.get(session)["final_audio"]["path"], output)
        self.assertEqual(len(seen), 1)

    def test_final_mixed_over_unreadable_audio_is_saved_but_never_compacted(self):
        session = self.controller.store.begin({})
        self.controller.store.finish(session)

        def encode(_store, _session, path, *, on_missing, **_kwargs):
            on_missing({"type": "audio", "track": "system"})
            Path(path).write_bytes(b"synthetic encoded audio")
            return str(path)

        with tempfile.TemporaryDirectory(dir=Path(self.temp.name).parent) as folder, \
                patch("meeting_support.export_mixdown", side_effect=encode):
            output = self.controller._export_audio(session, Path(folder) / "final.mp3", save_final=True)
            final = self.controller.store.get(session)["final_audio"]
            self.assertEqual(final["path"], output)
            self.assertIs(final["verification"]["sources_complete"], False)
            with patch("meeting_support.compact_capture") as compact:
                self.controller._compact_capture(session)
            evidence = compact.call_args.args[2]
            self.assertIs(evidence["sources_complete"], False)

    def test_failed_and_cancelled_exports_never_leave_active_or_completed_progress(self):
        for error, stage in ((OSError("disk write failed"), "failed"),
                             (MixdownCancelled("cancelled"), "cancelled")):
            with self.subTest(stage=stage), patch("meeting_support.export_mixdown", side_effect=error):
                with self.assertRaises(type(error)):
                    self.controller.export_mixdown("session", "final.mp3")
                snap = self.controller.snapshot()
                self.assertEqual(snap["audio_export"]["stage"], stage)
                self.assertIsNone(snap["audio_export"]["percent"])
                self.assertFalse(snap["processing"])

    def test_metadata_save_failure_never_claims_final_audio_is_saved(self):
        session = self.controller.store.begin({})
        self.controller.store.finish(session)
        with patch.object(self.controller.store, "save_final_audio", side_effect=OSError("metadata failed")):
            with self.assertRaisesRegex(OSError, "metadata failed"):
                self.controller._export_audio(session, Path(self.temp.name) / "final.mp3", save_final=True)
        self.assertEqual(self.controller.snapshot()["audio_export"]["stage"], "failed")
        self.assertEqual(self.controller.snapshot()["final_audio"], "")

    def test_processing_state_write_failure_stops_export_progress(self):
        session = self.controller.store.begin({})
        self.controller.store.finish(session)
        with patch.object(self.controller.store, "save_processing_state", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.controller._export_audio(session, Path(self.temp.name) / "final.mp3", save_final=True)
        snapshot = self.controller.snapshot()
        self.assertEqual(snapshot["audio_export"]["stage"], "failed")
        self.assertIsNone(snapshot["audio_export"]["percent"])

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

    def test_repeated_adjustment_replaces_the_mp3_final_instead_of_copying_it(self):
        controller, store, session, previous, _payload = self._quiet_recording()
        for _ in range(3):
            self.assertTrue(controller.regenerate_final_audio(session))
            controller._processing_thread.join(3)
            self.assertFalse(controller.snapshot()["error"])
        final = Path(store.get(session)["final_audio"]["path"])
        self.assertEqual(final.suffix, ".mp3")
        # The legacy WAV stays; the MP3 is rebuilt in place, never copied.
        self.assertEqual(sorted(path.name for path in previous.parent.iterdir() if path.is_file()),
                         sorted([previous.name, final.name]))
        self.assertEqual(self.final_export.call_args.args[2], final)

    def test_final_audio_download_copies_the_saved_file_without_raw_tracks(self):
        controller, store, session, previous, _payload = self._quiet_recording()
        target = Path(self.temp.name) / "downloaded.wav"
        self.assertEqual(controller.export_final_audio(session, target), str(target.absolute()))
        self.assertEqual(target.read_bytes(), previous.read_bytes())
        with self.assertRaises(ValueError):
            controller.export_final_audio(session, Path(self.temp.name) / "wrong.mp3")
        with self.assertRaises(ValueError):
            controller.export_final_audio(session, Path(store.root) / "inside.wav")

    def test_saved_audio_download_preserves_running_inference_state_and_cancellation(self):
        controller, _store, session, previous, _payload = self._quiet_recording()
        controller._processing = True
        controller._processing_session = session
        progress = {"session_id": session, "stage": "completed", "percent": 100}
        controller._audio_export_progress = progress
        controller._cancel.set()
        target = Path(self.temp.name) / "during-summary.wav"
        try:
            self.assertEqual(controller.export_final_audio(session, target), str(target.absolute()))
            self.assertEqual(target.read_bytes(), previous.read_bytes())
            self.assertTrue(controller._processing)
            self.assertEqual(controller._processing_session, session)
            self.assertIs(controller._audio_export_progress, progress)
            self.assertTrue(controller._cancel.is_set())
            self.assertFalse(controller._final_copy_active)
            self.assertTrue(controller._final_copy_done.is_set())
        finally:
            controller._processing = False

    def test_saved_audio_download_is_admitted_during_automatic_postprocessing(self):
        controller, _store, session, previous, _payload = self._quiet_recording()
        controller._state = "postprocessing"
        controller._processing = True
        controller._audio_export_progress = {"session_id": session, "stage": "completed", "percent": 100}
        target = Path(self.temp.name) / "during-postprocessing.wav"
        try:
            self.assertEqual(controller.export_final_audio(session, target), str(target.absolute()))
            self.assertEqual(target.read_bytes(), previous.read_bytes())
            self.assertEqual(controller._state, "postprocessing")
            controller._audio_export_progress = {"session_id": session, "stage": "encoding", "percent": 10}
            with self.assertRaises(RuntimeError):
                controller.export_final_audio(session, target)
            controller._state = "recording"
            controller._audio_export_progress = {}
            with self.assertRaises(RuntimeError):
                controller.export_final_audio(session, target)
        finally:
            controller._state = "idle"
            controller._processing = False

    def test_final_copy_lease_blocks_mutation_and_shutdown_waits_for_copy(self):
        controller, _store, session, previous, _payload = self._quiet_recording()
        target = Path(self.temp.name) / "leased-copy.wav"
        entered, release, shutdown_done = threading.Event(), threading.Event(), threading.Event()
        errors = []
        def controlled_copy(source, destination):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("synthetic copy was not released")
            Path(destination).write_bytes(Path(source).read_bytes())
        def copy():
            try:
                controller.export_final_audio(session, target)
            except BaseException as error:
                errors.append(error)
        def close():
            try:
                controller.shutdown()
            except BaseException as error:
                errors.append(error)
            finally:
                shutdown_done.set()
        copier = threading.Thread(target=copy)
        closer = threading.Thread(target=close)
        with patch("meeting_support.shutil.copyfile", side_effect=controlled_copy):
            try:
                copier.start()
                self.assertTrue(entered.wait(2))
                self.assertTrue(controller.is_busy())
                self.assertFalse(controller.start({"microphone": False, "system": True}))
                self.assertFalse(controller.regenerate_final_audio(session))
                with self.assertRaises(RuntimeError):
                    controller._begin_retention()
                with self.assertRaises(RuntimeError):
                    controller.export_final_audio(session, Path(self.temp.name) / "second.wav")
                closer.start()
                self.assertFalse(shutdown_done.wait(.05))
            finally:
                release.set()
                copier.join(5)
                if closer.ident is not None:
                    closer.join(5)
        self.assertFalse(copier.is_alive())
        self.assertFalse(closer.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(target.read_bytes(), previous.read_bytes())
        self.assertFalse(controller._final_copy_active)
        self.assertTrue(controller._final_copy_done.is_set())

    def test_final_copy_failure_releases_lease_and_keeps_existing_destination(self):
        controller, _store, session, previous, _payload = self._quiet_recording()
        target = Path(self.temp.name) / "preserved-copy.wav"
        target.write_bytes(b"prior destination")
        with patch("meeting_support.shutil.copyfile", side_effect=OSError("synthetic copy failure")):
            with self.assertRaises(OSError):
                controller.export_final_audio(session, target)
        self.assertEqual(target.read_bytes(), b"prior destination")
        self.assertFalse(controller._final_copy_active)
        self.assertTrue(controller._final_copy_done.is_set())
        self.assertEqual(list(target.parent.glob(".preserved-copy.wav-*.tmp")), [])
        self.assertEqual(controller.export_final_audio(session, target), str(target.absolute()))
        self.assertEqual(target.read_bytes(), previous.read_bytes())

    def test_final_copy_rejects_audio_export_before_saved_completion(self):
        controller, _store, session, _previous, _payload = self._quiet_recording()
        target = Path(self.temp.name) / "too-early.wav"
        for stage in ("preparing", "encoding", "finalizing"):
            controller._audio_export_progress = {"session_id": session, "stage": stage}
            with self.subTest(stage=stage), self.assertRaises(RuntimeError):
                controller.export_final_audio(session, target)
        self.assertFalse(target.exists())
        self.assertFalse(controller._final_copy_active)
        self.assertTrue(controller._final_copy_done.is_set())

    def test_queued_audio_writer_waits_for_already_admitted_copy(self):
        controller, _store, session, previous, _payload = self._quiet_recording()
        target = Path(self.temp.name) / "queued-reader.wav"
        entered, release, preparing = threading.Event(), threading.Event(), threading.Event()
        errors = []
        def controlled_copy(source, destination):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("synthetic copy was not released")
            Path(destination).write_bytes(Path(source).read_bytes())
        original_wait = controller._final_copy_done.wait
        def observed_wait(timeout):
            preparing.set()
            return original_wait(timeout)
        def copy():
            try:
                controller.export_final_audio(session, target)
            except BaseException as error:
                errors.append(error)
        def write():
            try:
                controller._export_audio(session, Path(previous).with_suffix(".mp3"), save_final=True)
            except BaseException as error:
                errors.append(error)
        copier, writer = threading.Thread(target=copy), threading.Thread(target=write)
        self.final_export.reset_mock()
        with patch("meeting_support.shutil.copyfile", side_effect=controlled_copy), \
                patch.object(controller._final_copy_done, "wait", side_effect=observed_wait):
            try:
                copier.start()
                self.assertTrue(entered.wait(2))
                writer.start()
                self.assertTrue(preparing.wait(2))
                self.assertEqual(controller._audio_export_progress["stage"], "preparing")
                self.final_export.assert_not_called()
            finally:
                release.set()
                copier.join(5)
                if writer.ident is not None:
                    writer.join(5)
        self.assertFalse(copier.is_alive())
        self.assertFalse(writer.is_alive())
        self.assertEqual(errors, [])
        self.final_export.assert_called_once()
        self.assertEqual(target.read_bytes(), previous.read_bytes())

    def test_queued_audio_writer_timeout_preserves_saved_audio_without_encoding(self):
        controller, store, session, previous, _payload = self._quiet_recording()
        previous_bytes = previous.read_bytes()
        self.final_export.reset_mock()
        with patch.object(controller._final_copy_done, "wait", return_value=False):
            with self.assertRaises(RuntimeError):
                controller._export_audio(session, previous.with_suffix(".mp3"), save_final=True)
        self.final_export.assert_not_called()
        self.assertEqual(store.get(session)["final_audio"]["path"], str(previous))
        self.assertEqual(previous.read_bytes(), previous_bytes)

    def test_track_download_exports_only_that_source(self):
        controller, _store, session, _previous, _payload = self._quiet_recording()
        target = Path(self.temp.name) / "microphone.mp3"
        controller.export_track(session, "microphone", target)
        self.assertEqual(self.final_export.call_args.kwargs["tracks"], ("microphone",))
        with self.assertRaises(ValueError):
            controller.export_track(session, "final", target)
        with self.assertRaises(ValueError):
            controller.export_track(session, "microphone", Path(self.temp.name) / "microphone.wav")

    def test_audio_inventory_reports_final_and_raw_sizes(self):
        controller, _store, session, previous, payload = self._quiet_recording()
        inventory = controller.audio_inventory(session)
        self.assertEqual(inventory["final"], previous.stat().st_size)
        self.assertEqual(inventory["final_format"], "WAV")
        self.assertGreaterEqual(inventory["microphone"], len(payload))
        self.assertIsNone(inventory["system"])

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
        with self.assertRaises(TrackUnavailableError):
            list(store.iter_audio(session))
        self.assertEqual(metadata["capture_storage"]["state"], "compacted")
        self.assertEqual(metadata["processing"]["audio"]["status"], "completed")
        self.assertEqual(next(store.iter_events(session))["type"], "source_changed")
        self.assertTrue(Path(metadata["final_audio"]["path"]).is_file())
        self.assertEqual(Path(metadata["final_audio"]["path"]).suffix, ".mp3")

    def test_capture_health_is_visible_and_recovery_evidence_survives_saved_mp3(self):
        pending = [
            {"type": "capture_health", "track": "system", "state": "recovering",
             "reason": "synthetic same-device reconnect", "packets": 0, "frames": 0,
             "last_packet_age": 3.0, "recovery_attempt": 1, "timestamp": 3.0},
            {"type": "gap", "track": "system", "timestamp": 3.0,
             "reason": "capture_interruption_reopening"},
            {"type": "capture_health", "track": "system", "state": "receiving",
             "reason": "packet_watchdog", "packets": 1, "frames": 2,
             "last_packet_age": 0.0, "recovery_attempt": 1, "timestamp": 3.1},
        ]
        original = self.capture.read_event
        def read_event(timeout):
            if pending:
                return dict(pending.pop(0), generation=self.capture.generation), b""
            return original(timeout)
        self.capture.read_event = read_event
        self.assertTrue(self.controller.start(MeetingSettings(sources="system")))
        self.assertTrue(self.capture.started.wait(2))
        deadline = time.monotonic() + 2
        while self.controller.snapshot()["capture_health"].get("system", {}).get("state") != "receiving":
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.005)
        snapshot = self.controller.snapshot()
        snapshot["capture_health"]["system"]["state"] = "mutated"
        self.assertEqual(self.controller.snapshot()["capture_health"]["system"]["state"], "receiving")
        self.controller.stop()
        self.controller._thread.join(3)
        self.assertFalse(self.controller._thread.is_alive())
        session = self.controller.snapshot()["session_id"]
        store = MeetingStore(self.temp.name)
        events = list(store.iter_events(session))
        self.assertEqual([event["state"] for event in events if event["type"] == "capture_health"],
                         ["recovering", "receiving"])
        self.assertTrue(any(event.get("reason") == "capture_interruption_reopening" for event in events))
        self.assertTrue(Path(store.get(session)["final_audio"]["path"]).is_file())
        self.assertTrue(self.controller.snapshot()["partial"], "Never erase evidence of an actual interruption")

    def test_disconnected_source_alerts_once_per_outage_and_recording_continues(self):
        def health(state, attempt=1):
            return {"type": "capture_health", "track": "microphone", "state": state,
                    "reason": "synthetic", "packets": 0, "frames": 0, "last_packet_age": 3.0,
                    "recovery_attempt": attempt, "timestamp": 3.0}
        pending = [
            {"type": "gap", "track": "microphone", "timestamp": 3.0,
             "reason": "capture_interruption_reopening"},
            health("disconnected"), health("disconnected", 2), health("disconnected", 3),
            health("receiving"),
            health("recovering", 6),
            health("receiving"),
        ]
        original = self.capture.read_event
        def read_event(timeout):
            if pending:
                return dict(pending.pop(0), generation=self.capture.generation), b""
            return original(timeout)
        self.capture.read_event = read_event
        self.controller.notify = Mock()
        self.assertTrue(self.controller.start(MeetingSettings(sources="microphone")))
        self.assertTrue(self.capture.started.wait(2))
        deadline = time.monotonic() + 2
        while pending:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.005)
        self.assertEqual(self.controller.snapshot()["state"], "recording")
        self.controller.stop()
        self.controller._thread.join(3)
        messages = [call.args[0] for call in self.controller.notify.call_args_list]
        alerts = [message for message in messages if "não está recebendo áudio do microfone" in message]
        self.assertEqual(len(alerts), 2)
        self.assertFalse(any("Verifique a captura" in message for message in messages))
        self.assertTrue(self.controller.snapshot()["partial"])

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

    def test_persistence_failure_during_postprocess_settles_capture_admission(self):
        from meeting_transcription import MeetingTranscriptionError
        original = self.controller.store.save_processing_state

        def persist(sid, stage, status, error=None):
            if stage == "transcription" and status == "failed":
                raise OSError("disk full")
            return original(sid, stage, status, error)

        with patch.object(self.controller, "_installed_voice_profile", return_value="balanced"), \
                patch("meeting_transcription.transcribe_meeting",
                      side_effect=MeetingTranscriptionError("resource still live", resource_live=True)), \
                patch.object(self.controller.store, "save_processing_state", side_effect=persist):
            self.assertTrue(self.controller.start(MeetingSettings()))
            self.assertTrue(self.capture.started.wait(2))
            self.controller.stop()
            self.controller._thread.join(3)
        snapshot = self.controller.snapshot()
        self.assertTrue(self.capture.closed)
        self.assertFalse(snapshot["processing"])
        self.assertEqual(snapshot["state"], "unavailable")
        self.assertIn("disk full", snapshot["error"])
        self.voice.release_meeting.assert_not_called()
        metadata = self.controller.store.get(snapshot["session_id"])
        self.assertTrue(Path(metadata["final_audio"]["path"]).is_file())
        self.assertNotIn("capture_storage", metadata)
        self.assertTrue(list(self.controller.store.iter_audio(snapshot["session_id"])))

    def test_failed_mp3_still_transcribes_raw_and_keeps_it(self):
        self.final_export.side_effect = RuntimeError("MP3 encoder unavailable")
        sources = []

        def transcribe(store, session, *_args, **_kwargs):
            sources.append(len(list(store.iter_audio(session))))
            store.save_processing_state(session, "transcription", "processing")

        with patch.object(self.controller, "_installed_voice_profile", return_value="balanced"), \
                patch.object(self.controller, "_installed_summary_model", return_value=None), \
                patch("meeting_transcription.transcribe_meeting", side_effect=transcribe):
            self.assertTrue(self.controller.start(MeetingSettings()))
            self.assertTrue(self.capture.started.wait(2))
            self.controller.stop()
            self.controller._thread.join(3)
        snapshot = self.controller.snapshot()
        metadata = self.controller.store.get(snapshot["session_id"])
        self.assertEqual(sources, [1])
        self.assertEqual(metadata["processing"]["transcription"]["status"], "completed")
        self.assertIsNone(metadata["final_audio"])
        self.assertNotIn("capture_storage", metadata)
        self.assertIn("MP3 encoder unavailable", snapshot["error"])

    def test_raw_capture_is_cleaned_only_after_every_step_succeeds(self):
        for failure, compacted in ((None, True), (RuntimeError("synthetic ASR failure"), False)):
            self.capture = FakeCapture()
            with self.subTest(failure=failure), \
                    patch.object(self.controller, "_installed_voice_profile", return_value="balanced"), \
                    patch.object(self.controller, "_installed_summary_model", return_value=None), \
                    patch("meeting_transcription.transcribe_meeting", side_effect=failure):
                self.assertTrue(self.controller.start(MeetingSettings()))
                self.assertTrue(self.capture.started.wait(2))
                self.controller.stop()
                self.controller._thread.join(3)
                session = self.controller.snapshot()["session_id"]
                metadata = self.controller.store.get(session)
                self.assertTrue(Path(metadata["final_audio"]["path"]).is_file())
                if compacted:
                    self.assertEqual(metadata["capture_storage"]["state"], "compacted")
                    self.assertEqual(metadata["processing"]["capture_cleanup"]["status"], "completed")
                else:
                    self.assertNotIn("capture_storage", metadata)
                    self.assertEqual(metadata["processing"]["capture_cleanup"]["status"], "skipped")
                    self.assertTrue(list(self.controller.store.iter_audio(session)))

    @unittest.skipUnless(sys.platform in ("win32", "darwin"), "supported capture host required")
    def test_stop_racing_helper_exit_still_journals_buffered_audio(self):
        script = (
            "import json,struct,sys\n"
            "generation=int(sys.argv[1])\n"
            "def emit(event,payload=b''):\n"
            " event['generation']=generation; data=json.dumps(event).encode(); "
            "sys.stdout.buffer.write(struct.pack('<I',len(data))+data+struct.pack('<I',len(payload))+payload); sys.stdout.buffer.flush()\n"
            "emit({'type':'ready','version':1})\n"
            "sys.stdin.readline()\n"
            "emit({'type':'audio','track':'system','rate':16000,'channels':1,'frames':2,"
            "'timestamp':0,'sequence':0},struct.pack('<2f',.25,-.5))\n"
            "emit({'type':'gap','track':'system','timestamp':.01,'reason':'transport_overflow'})\n"
            "emit({'type':'stopped','reason':'transport_overflow'})\n"
            "sys.exit(3)\n"
        )
        capture = NativeCapture(helper_path=sys.executable, popen=lambda argv, **options:
            subprocess.Popen([sys.executable, "-u", "-c", script, argv[-1]], **options))
        self.controller.capture_factory = lambda: capture
        start = capture.start

        def stop_after_exit(settings, generation):
            start(settings, generation)
            capture._process.stdin.write(b"exit\n")
            capture._process.stdin.flush()
            capture._process.wait(timeout=3)
            self.controller.stop()

        with patch.object(capture, "start", side_effect=stop_after_exit):
            self.assertTrue(self.controller.start(MeetingSettings()))
            self.controller._thread.join(4)
        self.assertFalse(self.controller._thread.is_alive())
        snapshot = self.controller.snapshot()
        audio = list(self.controller.store.iter_audio(snapshot["session_id"]))
        self.assertEqual([payload for _event, payload in audio], [struct.pack("<2f", .25, -.5)])
        self.assertEqual(self.controller.store.get(snapshot["session_id"])["status"], "failed")
        self.assertIn("disco", snapshot["error"])
        self.voice.release_meeting.assert_called_once_with("lease")
        self.assertFalse(capture._reader.is_alive())

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

    def test_stop_during_a_slow_pause_command_keeps_the_stopping_state(self):
        entered = threading.Event()
        proceed = threading.Event()
        pause_returned = threading.Event()
        original_command = self.capture.command
        original_read = self.capture.read_event

        def command(value):
            if value == "pause":
                entered.set()
                if not proceed.wait(3):
                    raise RuntimeError("fixture pause timed out")
            original_command(value)

        def read(timeout):
            if "pause" in self.capture.commands and "stop" not in self.capture.commands:
                pause_returned.set()
                self.controller._stop.wait(2)
                if not proceed.wait(3):
                    raise RuntimeError("fixture read timed out")
                self.observed_state = self.controller.snapshot()["state"]
            return original_read(timeout)

        self.observed_state = None
        with patch.object(self.capture, "command", side_effect=command), \
                patch.object(self.capture, "read_event", side_effect=read):
            try:
                self.assertTrue(self.controller.start(MeetingSettings()))
                self.assertTrue(self.capture.started.wait(2))
                self.assertTrue(self.controller.pause())
                self.assertTrue(entered.wait(2))
                self.assertTrue(self.controller.stop())
                proceed.set()
                self.assertTrue(pause_returned.wait(2))
                self.controller._thread.join(3)
                self.assertFalse(self.controller._thread.is_alive())
                self.assertEqual(self.observed_state, "stopping")
                self.assertEqual(self.controller.snapshot()["state"], "idle")
            finally:
                proceed.set()
                self.controller.stop()
                self.controller._thread.join(3)

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
        self.assertTrue(self.controller._launch_processing(work, kind="transcription"))
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

    def test_failed_inference_unload_keeps_reservation(self):
        from meeting_transcription import MeetingTranscriptionError
        session = self.controller.store.begin({})
        self.controller.store.finish(session)
        with patch("meeting_transcription.transcribe_meeting",
                   side_effect=MeetingTranscriptionError("still live", resource_live=True)):
            self.assertTrue(self.controller.transcribe(session, "balanced", "auto"))
            self.controller._processing_thread.join(2)
        self.assertEqual(self.controller.snapshot()["state"], "unavailable")
        self.voice.release_meeting.assert_not_called()

    def test_inference_resource_failure_survives_error_persistence_failure(self):
        from meeting_transcription import MeetingTranscriptionError
        session = self.controller.store.begin({})
        self.controller.store.finish(session)
        original = self.controller.store.save_processing_state

        def persist(sid, stage, status, error=None):
            if status == "failed":
                raise OSError("disk full")
            return original(sid, stage, status, error)

        with patch("meeting_transcription.transcribe_meeting",
                   side_effect=MeetingTranscriptionError("resource still live", resource_live=True)), \
                patch.object(self.controller.store, "save_processing_state", side_effect=persist):
            self.assertTrue(self.controller.transcribe(session, "balanced", "auto"))
            self.controller._processing_thread.join(2)
        self.assertEqual(self.controller.snapshot()["state"], "unavailable")
        self.assertFalse(self.controller.snapshot()["processing"])
        self.voice.release_meeting.assert_not_called()

    def test_installed_models_run_automatically_when_legacy_switches_are_false(self):
        settings = MeetingSettings(voice_boost=True)
        with patch.object(self.controller, "_installed_voice_profile", return_value="balanced"), \
                patch.object(self.controller, "_installed_summary_model", return_value="local-summary"), \
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
        self.assertEqual(transcribe.call_args.args[1:3], (session, "balanced"))
        self.assertEqual(summarize.call_args.args[1:3], (session, "local-summary"))
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
        session = self.controller.store.begin({})
        self.controller.store.finish(session)
        with patch.object(self.controller, "_installed_summary_model", return_value="fallback"), \
                patch("meeting_summary.summarize_meeting") as summarize:
            self.assertTrue(self.controller.summarize(session, "preferred"))
            self.controller._processing_thread.join(2)
        summarize.assert_called_once()
        with patch.object(self.controller, "_installed_summary_model", return_value=None):
            self.assertTrue(self.controller.summarize(session, "preferred"))
            self.controller._processing_thread.join(2)
        self.assertIn("Nenhum modelo de resumo instalado", self.controller.snapshot()["error"])

    def test_imported_audio_enters_automatic_transcription_workflow(self):
        settings = MeetingSettings()
        with patch("meeting_files.import_audio", return_value="imported"), \
                patch.object(self.controller, "_prepare_mp3") as prepare, \
                patch.object(self.controller, "_installed_voice_profile", return_value="balanced"), \
                patch.object(self.controller, "_transcribe_and_summarize") as process:
            self.assertEqual(self.controller.import_audio("recording.wav", settings), "imported")
        process.assert_called_once_with("imported", "balanced", settings.language, settings.summary_model)
        prepare.assert_called_once_with("imported", settings=settings)

    def test_imported_audio_without_installed_voice_model_skips_inference(self):
        settings = MeetingSettings()
        with patch("meeting_files.import_audio", return_value="imported"), \
                patch.object(self.controller, "_prepare_mp3") as prepare, \
                patch.object(self.controller, "_installed_voice_profile", return_value=None), \
                patch.object(self.controller, "_transcribe_and_summarize") as process:
            self.assertEqual(self.controller.import_audio("recording.wav", settings), "imported")
        process.assert_not_called()
        prepare.assert_called_once_with("imported", settings=settings)

    def test_import_wav_publishes_and_compacts_before_transcription(self):
        wav_path = Path(self.temp.name) / "import.wav"
        with wave.open(str(wav_path), "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(8000)
            writer.writeframes(struct.pack("<4h", 100, -100, 200, -200))
        settings = MeetingSettings()
        order = []
        def process(*args):
            self.assertEqual(self.controller.store.get(args[0])["capture_storage"]["state"], "compacted")
            order.append("asr")
        with patch.object(self.controller, "_installed_voice_profile", return_value="balanced"), \
                patch.object(self.controller, "_transcribe_and_summarize", side_effect=process), \
                patch.object(self.controller, "_prepare_mp3", wraps=self.controller._prepare_mp3) as prepare:
            session = self.controller.import_audio(str(wav_path), settings)
        metadata = self.controller.store.get(session, include_events=False)
        self.assertEqual(metadata["capture_storage"]["state"], "compacted")
        self.assertEqual(metadata["processing"]["audio"]["status"], "completed")
        self.assertTrue(metadata["final_audio"]["path"].endswith(".mp3"))
        self.assertTrue(order)
        prepare.assert_called_once()

    def test_import_export_failure_persists_audio_failure_and_keeps_source(self):
        wav_path = Path(self.temp.name) / "failed-import.wav"
        with wave.open(str(wav_path), "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(8000)
            writer.writeframes(struct.pack("<4h", 100, -100, 200, -200))
        self.final_export.side_effect = OSError("encoder failed")
        with self.assertRaises(OSError):
            self.controller.import_audio(str(wav_path), MeetingSettings())
        sessions = self.controller.store.list_sessions(limit=10)
        self.assertEqual(len(sessions), 1)
        session = sessions[0]["id"]
        metadata = self.controller.store.get(session, include_events=False)
        self.assertEqual(metadata["processing"]["audio"]["status"], "failed")
        self.assertIsNone(metadata["final_audio"])
        self.assertTrue(list(self.controller.store.iter_audio(session, "microphone")))

    def test_startup_recovery_reconciles_staged_capture_after_restart(self):
        store = self.controller.store
        session = store.begin({})
        store.append_audio(session, {"type": "audio", "track": "microphone", "rate": 4,
                                     "channels": 1, "frames": 8, "timestamp": 0.0,
                                     "sequence": 0, "generation": 0}, struct.pack("<8f", *([.2] * 8)))
        store.finish(session)
        final = Path(self.temp.name) / "restart.mp3"
        final.write_bytes(b"verified")
        evidence = {"path": str(final), "sha256": hashlib.sha256(final.read_bytes()).hexdigest(),
                    "size": final.stat().st_size, "mtime_ns": final.stat().st_mtime_ns,
                    "duration": 2.0, "frames": 8, "source_end": 2.0, "tolerance": .05}
        store.save_final_audio(session, final, verification=evidence)
        class SimulatedCrash(BaseException):
            pass
        with self.assertRaises(SimulatedCrash):
            compact_capture(store, session, evidence,
                            fault_injector=lambda stage: (_ for _ in ()).throw(SimulatedCrash())
                            if stage == "after_stage_microphone" else None)
        restarted = MeetingController(self.temp.name, self.voice)
        self.addCleanup(restarted.shutdown)
        restarted.recover_retention_operations()
        metadata = restarted.store.get(session, include_events=False)
        self.assertEqual(metadata["capture_storage"]["state"], "compacted")
        self.assertTrue(final.is_file())
        with self.assertRaises(TrackUnavailableError):
            list(restarted.store.iter_audio(session, "microphone"))

    def test_startup_recovery_preserves_staged_capture_when_final_changed(self):
        store = self.controller.store
        session = store.begin({})
        store.append_audio(session, {"type": "audio", "track": "microphone", "rate": 4,
                                     "channels": 1, "frames": 8, "timestamp": 0.0,
                                     "sequence": 0, "generation": 0}, struct.pack("<8f", *([.2] * 8)))
        store.finish(session)
        final = Path(self.temp.name) / "corrupt-restart.mp3"
        final.write_bytes(b"verified")
        evidence = {"path": str(final), "sha256": hashlib.sha256(final.read_bytes()).hexdigest(),
                    "size": final.stat().st_size, "mtime_ns": final.stat().st_mtime_ns,
                    "duration": 2.0, "frames": 8, "source_end": 2.0, "tolerance": .05}
        store.save_final_audio(session, final, verification=evidence)
        with self.assertRaises(BaseException):
            compact_capture(store, session, evidence,
                            fault_injector=lambda stage: (_ for _ in ()).throw(KeyboardInterrupt())
                            if stage == "after_commit" else None)
        final.write_bytes(b"tampered")
        restarted = MeetingController(self.temp.name, self.voice)
        self.addCleanup(restarted.shutdown)
        with self.assertRaises(Exception):
            restarted.recover_retention_operations()
        metadata = restarted.store.get(session, include_events=False)
        self.assertEqual(metadata.get("capture_storage", {}).get("state"), "compacted")
        self.assertTrue(list(Path(store._session_dir(session)).glob(".capture-compaction-*")))

    def test_missing_preferred_voice_model_falls_back_to_default_before_catalog_order(self):
        installed = {"whisper-small", "balanced"}
        with patch("voice_models.model_is_installed",
                   side_effect=lambda entry, _cache: entry["profile"] in installed):
            self.assertEqual(self.controller._installed_voice_profile("accuracy"), "balanced")
            installed.discard("balanced")
            self.assertEqual(self.controller._installed_voice_profile("accuracy"), "whisper-small")

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
        self.assertEqual(snapshot["postprocess"], "Processamento interrompido; tente novamente.")
        self.assertIn("cancelado", snapshot["error"])

    def test_cancel_during_capture_never_reaches_the_recordings_post_processing(self):
        # Regression: closing the manager mid-recording called cancel_processing()
        # and the shared event skipped the final MP3, transcript and summary.
        with patch.object(self.controller, "_installed_voice_profile", return_value="balanced"), \
                patch.object(self.controller, "_installed_summary_model", return_value=None), \
                patch("meeting_transcription.transcribe_meeting") as transcribe:
            self.assertTrue(self.controller.start(MeetingSettings(destination=self.temp.name)))
            self.assertTrue(self.capture.started.wait(2))
            self.assertFalse(self.controller.cancel_processing())
            self.assertFalse(self.controller._cancel.is_set())
            self.assertTrue(self.controller.stop())
            self.assertFalse(self.controller.cancel_processing())
            self.controller._thread.join(5)

        snapshot = self.controller.snapshot()
        self.assertEqual(snapshot["state"], "idle")
        self.assertEqual(snapshot["error"], "")
        self.final_export.assert_called_once()
        transcribe.assert_called_once()
        processing = self.controller.store.get(snapshot["session_id"])["processing"]
        self.assertEqual(processing["audio"]["status"], "completed")
        self.assertEqual(processing["transcription"]["status"], "completed")

    def test_post_processing_reports_its_step_and_stays_cancellable(self):
        seen = {}

        def transcribe(*_args, cancel_event=None, **_kwargs):
            snapshot = self.controller.snapshot()
            seen.update(state=snapshot["state"], kind=snapshot["processing_kind"],
                        session=snapshot["processing_session"], blocker=start_blocker(snapshot))
            # A start-blocked person interrupts the job ("Interromper e gravar").
            seen["issued"] = self.controller.cancel_processing()
            seen["event"] = cancel_event.is_set()

        with patch.object(self.controller, "_installed_voice_profile", return_value="balanced"), \
                patch.object(self.controller, "_installed_summary_model", return_value="qwen3.5-2b-q4"), \
                patch("meeting_transcription.transcribe_meeting", side_effect=transcribe), \
                patch("meeting_summary.summarize_meeting") as summarize:
            self.assertTrue(self.controller.start(MeetingSettings(destination=self.temp.name)))
            self.assertTrue(self.capture.started.wait(2))
            self.controller.stop()
            self.controller._thread.join(5)

        session = self.controller.snapshot()["session_id"]
        self.assertEqual(seen, {"state": "postprocessing", "kind": "transcription", "session": session,
                                "blocker": "a transcrição", "issued": True, "event": True})
        summarize.assert_not_called()
        snapshot = self.controller.snapshot()
        self.assertEqual(snapshot["state"], "idle")
        self.assertIsNone(snapshot["processing_kind"])
        self.assertIsNone(snapshot["processing_session"])
        # A cancel belongs to the job it stopped; later default reads are clean.
        self.assertFalse(self.controller._cancel.is_set())
        processing = self.controller.store.get(session)["processing"]
        self.assertEqual(processing["audio"]["status"], "completed")
        self.assertEqual(processing["transcription"]["status"], "cancelled")

    def test_cancel_targets_only_the_named_job_kinds(self):
        entered, release = threading.Event(), threading.Event()

        def work():
            entered.set()
            release.wait(2)

        self.assertTrue(self.controller._launch_processing(work, session_id="s1", kind="report"))
        try:
            self.assertTrue(entered.wait(1))
            snapshot = self.controller.snapshot()
            self.assertEqual((snapshot["processing_kind"], snapshot["processing_session"]), ("report", "s1"))
            self.assertEqual(start_blocker(snapshot), "o resumo")
            self.assertFalse(self.controller.cancel_processing(kinds=frozenset({"ask", "cross_question"})))
            self.assertFalse(self.controller._cancel.is_set())
            self.assertTrue(self.controller.cancel_processing(kinds=frozenset({"report"})))
            self.assertTrue(self.controller._cancel.is_set())
        finally:
            release.set()
            self.controller._processing_thread.join(2)
        self.assertFalse(self.controller._cancel.is_set())

    def test_cancel_without_a_running_job_is_refused(self):
        self.assertFalse(self.controller.cancel_processing())
        self.assertFalse(self.controller._cancel.is_set())

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


class StartBlockerTests(unittest.TestCase):
    def test_capture_and_a_free_recorder_have_no_blocker(self):
        self.assertEqual(start_blocker({"state": "idle"}), "")
        for state in ("starting", "recording", "paused", "stopping", "unavailable"):
            self.assertEqual(start_blocker({"state": state, "processing": True,
                                            "processing_kind": "export"}), "")

    def test_running_jobs_name_their_step(self):
        self.assertEqual(start_blocker({"state": "postprocessing", "processing": True,
                                        "processing_kind": "final_audio"}), "o áudio final")
        self.assertEqual(start_blocker({"state": "idle", "processing": True,
                                        "processing_kind": "ask"}), "a resposta do chat")
        # A snapshot without a kind still names the lane rather than reading free.
        self.assertEqual(start_blocker({"state": "idle", "processing": True}), "o processamento")
        self.assertEqual(start_blocker({"state": "postprocessing"}), "o processamento")
        self.assertEqual(processing_job_label({"state": "idle", "processing": False,
                                               "processing_kind": "ask"}), "")

    def test_leases_without_a_job_block_start_by_name(self):
        self.assertEqual(start_blocker({"state": "idle", "final_copy": True}), "a cópia do áudio final")
        self.assertEqual(start_blocker({"state": "idle", "playback": {"active": True}}), "a reprodução")
        self.assertEqual(start_blocker({"state": "idle", "retention_active": True}), "a retenção local")

    def test_every_job_kind_the_controller_reports_has_a_label(self):
        import ast
        source = Path(__file__).resolve().parents[1] / "meeting_support.py"
        kinds = set()
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                for keyword in node.keywords:
                    if keyword.arg == "kind" and isinstance(keyword.value, ast.Constant):
                        kinds.add(keyword.value.value)
                if (getattr(node.func, "attr", "") == "_processing_step" and node.args
                        and isinstance(node.args[0], ast.Constant)):
                    kinds.add(node.args[0].value)
            if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                    and node.value.value is not None
                    and any(isinstance(target, ast.Attribute) and target.attr == "_processing_kind"
                            for target in node.targets)):
                kinds.add(node.value.value)
        self.assertGreaterEqual(len(kinds), 10)
        self.assertEqual(sorted(kinds - set(JOB_LABELS)), [])


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

    def _with_final_audio(self):
        final = self.home / "recordings" / "final.mp3"
        final.parent.mkdir()
        final.write_bytes(b"final audio")
        metadata_path = self.home / "meetings" / "fixture-meeting-v1" / "metadata.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["final_audio"] = {"path": str(final), "created_at": "2026-09-16T12:04:00Z"}
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    def test_legacy_raw_policy_never_removes_unverified_recovery_data(self):
        self._with_final_audio()
        self.assertEqual(self.controller.sweep_retention()["raw_removed"], 0)
        self.library.update_workspace(
            {"retention_defaults": {"raw_audio": {"mode": "raw_tracks", "after_days": 1, "tracks": []}}},
            expected_generation=0,
        )
        result = self.controller.sweep_retention()
        self.assertEqual((result["raw_removed"], result["errors"]), (0, []))
        tracks = self.controller.store.get("fixture-meeting-v1")["tracks"]
        self.assertTrue(all(value.get("available") is not False for value in tracks.values()))
        self.assertEqual(self.controller.sweep_retention()["raw_removed"], 0)

    def test_retention_sweep_moves_old_recordings_to_restorable_trash(self):
        self.library.update_workspace(
            {"retention_defaults": {"whole_meeting": {"mode": "whole_meeting", "after_days": 1}}},
            expected_generation=0,
        )
        result = self.controller.sweep_retention()
        self.assertEqual((result["trashed"], result["errors"]), (1, []))
        self.assertFalse((self.home / "meetings" / "fixture-meeting-v1").exists())
        self.assertEqual(self.controller.list_trash()[0].session_id, "fixture-meeting-v1")
        self.controller.restore_session("fixture-meeting-v1")
        self.assertTrue((self.home / "meetings" / "fixture-meeting-v1").exists())

    def test_legacy_raw_policy_is_inactive_while_busy(self):
        self._with_final_audio()
        self.library.update_workspace(
            {"retention_defaults": {"raw_audio": {"mode": "raw_tracks", "after_days": 3650, "tracks": []}}},
            expected_generation=0,
        )
        self.assertEqual(self.controller.sweep_retention()["raw_removed"], 0)
        self.controller._processing = True
        try:
            self.assertEqual(self.controller.sweep_retention()["raw_removed"], 0)
        finally:
            self.controller._processing = False

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
