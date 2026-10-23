"""Readiness probes, bounded attention, and honest selected-recording progress."""
import types
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from meeting_gui import MeetingWindow
from meeting_support import MeetingController
import voice_models
from ops_status import (
    ReadinessProbe, completion_can_select, processing_view, readiness_rows, recent_attention,
)


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.probe = ReadinessProbe()

    def rows(self, **changes):
        snapshot = changes.pop("snapshot", {})
        options = dict(summary_state="ready", voice_available=True, startup_ready=True,
                       startup_text="", attention_count=0)
        options.update(changes)
        return readiness_rows(snapshot, self.probe, **options)

    def test_pending_failed_and_missing_inventory_never_claim_healthy(self):
        self.assertEqual([row.key for row in self.rows()], ["inventory"])
        self.probe.settle({"profile": "actual-fallback", "voice": {"enabled": True}}, None)
        self.assertEqual([row.key for row in self.rows()], ["healthy"])
        self.probe.settle(None, "synthetic disk error")
        self.assertEqual(self.probe.state, "unknown")
        self.assertEqual(self.rows()[0].tone, "warning")
        self.assertNotIn("healthy", [row.key for row in self.rows()])
        self.probe.settle({"profile": None, "voice": {"enabled": True}}, None)
        self.assertEqual([row.key for row in self.rows()], ["transcription"])

    def test_model_readiness_does_not_claim_capture_is_available(self):
        self.probe.settle({"profile": "local", "voice": {"enabled": True, "state": "ready"}}, None)
        row, = self.rows(snapshot={"state": "unavailable"})
        self.assertEqual((row.key, row.tone, row.text),
                         ("healthy", "text_muted", "Modelos locais prontos."))

    def test_summary_pending_and_failed_inventory_are_distinct_from_absent(self):
        self.probe.settle({"profile": "local", "voice": {"enabled": True}}, None)
        for state in ("pending", "unknown", "missing"):
            with self.subTest(state=state):
                row = self.rows(summary_state=state)[0]
                self.assertEqual(row.key, "summary")
                self.assertEqual(bool(row.action), state != "pending")
        self.assertNotEqual(self.rows(summary_state="unknown")[0].text,
                            self.rows(summary_state="missing")[0].text)

    def test_each_trouble_has_one_owned_action_and_recent_count_is_explicit(self):
        self.probe.settle({"profile": None, "voice": {"enabled": False}}, None)
        rows = readiness_rows({"processing": True, "processing_kind": "summary"}, self.probe,
            summary_state="missing", voice_available=True, startup_ready=False,
            startup_text="Synthetic recovery pending", attention_count=3)
        self.assertEqual([row.key for row in rows],
                         ["startup", "transcription", "summary", "job", "attention", "dictation"])
        self.assertTrue(all(row.action for row in rows))
        self.assertIn("nesta página", next(row.text for row in rows if row.key == "attention"))

    def test_probe_throttles_reads_and_never_overlaps_a_slow_read(self):
        self.assertTrue(self.probe.admit(0))
        self.assertFalse(self.probe.admit(0.5))
        self.assertFalse(self.probe.admit(100))
        self.probe.settle({"profile": "installed"}, None)
        self.assertFalse(self.probe.admit(0.99))
        self.assertTrue(self.probe.admit(1))

    def test_window_queries_only_on_workers_at_most_once_per_second(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.readiness_probe = self.probe
        view._readiness_next_render, view._readiness_rows = 0, ()
        view._effective_profile = mock.Mock(return_value="preferred")
        view.voice_status_getter = mock.Mock(return_value={"enabled": True})
        reader = mock.Mock(return_value="actual-fallback")
        view.controller = types.SimpleNamespace(installed_transcription_profile=reader)
        view.snapshot, view.session_items = {}, {}
        view._summary_inventory_state, view.retention_ready, view.startup_status = "ready", True, ""
        view._sync_navigation_badges = mock.Mock()
        view.bridge = types.SimpleNamespace(submit=mock.Mock(return_value=True))
        view._fail = mock.Mock()
        with mock.patch("meeting_gui.readiness_rows", return_value=()):
            for now in (0, 0.2, 0.8):
                view._sync_readiness(now)
            self.assertEqual(view.bridge.submit.call_count, 1)
            reader.assert_not_called()
            _key, worker, settle = view.bridge.submit.call_args.args
            settle(worker(), None)
            reader.assert_called_once_with("preferred")
            self.assertEqual(self.probe.profile, "actual-fallback")
            view._sync_readiness(1)
            self.assertEqual(view.bridge.submit.call_count, 2)
            self.probe.settle(worker(), None)
            view.bridge.submit.return_value = False
            view._sync_readiness(2)
            self.assertEqual(self.probe.state, "unknown")
            view._fail.assert_not_called()

    def test_enabled_dictation_exposes_unavailable_and_active_runtime_states(self):
        for state, tone in (("unavailable", "warning"), ("loading", "info"), ("transcribing", "info")):
            with self.subTest(state=state):
                self.probe.settle({"profile": "local", "voice": {
                    "enabled": True, "state": state, "status": "Synthetic runtime status"}}, None)
                row, = self.rows()
                self.assertEqual((row.key, row.tone), ("dictation", tone))
                self.assertTrue(row.action)

    def test_attention_action_routes_through_existing_dirty_selection_guard(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.session_items = {"one": {"id": "one", "status": "failed"}}
        view.library_tab, view.dirty = "library", True
        view.notebook, view.sessions, view.load_session = mock.Mock(), mock.Mock(), mock.Mock()
        view._open_attention_recording()
        view.notebook.select.assert_called_once_with("library")
        view.sessions.selection_set.assert_called_once_with("one")
        view.sessions.see.assert_called_once_with("one")
        view.load_session.assert_not_called()

    def test_attention_projection_is_only_the_supplied_page(self):
        page = [{"id": "one", "status": "failed"}, {"id": "two", "status": "completed"},
                {"id": "three", "status": "interrupted"}]
        self.assertEqual([item["id"] for item in recent_attention(page)], ["one", "three"])

    def test_written_navigation_badges_update_without_changing_selection(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.snapshot = {"state": "recording", "elapsed": 754}
        view.recording_tab, view.library_tab, view.voice_tab = "recording", "library", "dictation"
        view.readiness_probe = self.probe
        self.probe.voice = {"enabled": False}
        labels = dict.fromkeys(("recording", "library", "dictation"), "")
        def tab(key, option=None, **values):
            if "text" in values:
                labels[key] = values["text"]
            return labels[key]
        view.notebook = types.SimpleNamespace(tab=tab, event_generate=mock.Mock(), select=mock.Mock())
        view._sync_navigation_badges([{"id": "one"}])
        self.assertIn("● Gravando · 12:34", labels["recording"])
        self.assertIn("nesta página: 1", labels["library"])
        self.assertIn("Desligado", labels["dictation"])
        view.notebook.select.assert_not_called()
        view._sync_navigation_badges([{"id": "one"}])
        view.notebook.event_generate.assert_called_once_with("<<ManagerStatusChanged>>")

    def test_public_inventory_seam_reuses_pipeline_selection_without_capture(self):
        controller = MeetingController.__new__(MeetingController)
        controller.voice = types.SimpleNamespace(settings=types.SimpleNamespace(profile="preferred"))
        controller._installed_voice_profile = mock.Mock(return_value="installed-fallback")
        self.assertEqual(controller.installed_transcription_profile(), "installed-fallback")
        controller._installed_voice_profile.assert_called_once_with("preferred", refresh_manifest=False)
        controller.voice = None
        self.assertIsNone(controller.installed_transcription_profile())


class ProgressTests(unittest.TestCase):
    def test_banner_requires_exact_selected_session_and_real_export_percentage(self):
        snapshot = {"processing": True, "processing_session": "one", "processing_kind": "summary",
                    "postprocess": "Synthetic summary stage"}
        self.assertIsNone(processing_view(snapshot, "other", 10, 20))
        value = processing_view(snapshot, "one", 10, 25)
        self.assertEqual(value, {"stage": "Synthetic summary stage", "elapsed": 15, "percent": None})
        snapshot["audio_export"] = {"session_id": "other", "stage": "encoding", "percent": 50}
        self.assertIsNone(processing_view(snapshot, "one", 10, 25)["percent"])
        snapshot["audio_export"]["session_id"] = "one"
        self.assertEqual(processing_view(snapshot, "one", 10, 25)["percent"], 50)
        for invalid in (None, float("nan"), float("inf"), True, "50"):
            snapshot["audio_export"]["percent"] = invalid
            self.assertIsNone(processing_view(snapshot, "one", 10, 25)["percent"])

    def test_completion_cannot_move_after_navigation_section_or_selection_changes(self):
        context = ("one", "library", 2, 4, 7)
        expected = dict(session="one", destination="library", navigation=2, section=4, selection=7)
        self.assertTrue(completion_can_select(context, **expected))
        for key, value in (("session", "other"), ("destination", "settings"),
                           ("navigation", 3), ("section", 5), ("selection", 8)):
            changed = {**expected, key: value}
            self.assertFalse(completion_can_select(context, **changed))


class ReadOnlyInventoryTests(unittest.TestCase):
    def fixture(self, cache):
        payload = b"synthetic local model"
        entry = {"id": "fixture", "profile": "fixture", "filename": "fixture.gguf",
                 "sha256": hashlib.sha256(payload).hexdigest(), "size_bytes": len(payload),
                 "license_id": "MIT", "upstream_model": "synthetic"}
        path = Path(voice_models.model_path(cache, entry))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        manifest = Path(voice_models.manifest_path(cache, entry))
        manifest.write_text(json.dumps(entry), encoding="utf-8")
        return entry, path, manifest

    def test_legacy_inventory_never_writes_and_rehashes_only_after_stat_changes(self):
        with tempfile.TemporaryDirectory() as cache:
            entry, path, manifest = self.fixture(cache)
            original = manifest.read_bytes()
            with mock.patch("voice_models._write_manifest") as writer, \
                    mock.patch("voice_models._hash_file", wraps=voice_models._hash_file) as hasher:
                for _ in range(3):
                    self.assertTrue(voice_models.model_is_installed(entry, cache, refresh_manifest=False))
                self.assertEqual(hasher.call_count, 1)
                stat = path.stat()
                os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
                self.assertTrue(voice_models.model_is_installed(entry, cache, refresh_manifest=False))
                self.assertEqual(hasher.call_count, 2)
                manifest.write_bytes(original + b" ")
                self.assertTrue(voice_models.model_is_installed(entry, cache, refresh_manifest=False))
                self.assertEqual(hasher.call_count, 3)
                writer.assert_not_called()
            self.assertEqual(manifest.read_bytes(), original + b" ")

    def test_io_failure_is_unknown_and_selection_still_uses_a_verified_fallback(self):
        controller = MeetingController.__new__(MeetingController)
        controller.voice = types.SimpleNamespace(cache_dir="synthetic", settings=types.SimpleNamespace(profile="accuracy"))
        def installed(entry, _cache, **options):
            self.assertFalse(options["refresh_manifest"])
            if entry["profile"] == "accuracy":
                raise PermissionError("synthetic unreadable preferred model")
            return entry["profile"] == "balanced"
        with mock.patch("voice_models.model_is_installed", side_effect=installed):
            self.assertEqual(controller.installed_transcription_profile(), "balanced")
        with mock.patch("voice_models.model_is_installed", side_effect=PermissionError("synthetic")):
            with self.assertRaises(OSError):
                controller.installed_transcription_profile()


if __name__ == "__main__":
    unittest.main()
