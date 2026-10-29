"""Configurações for the Tauri manager (``manager_settings``).

Controllers and the summary model module are fakes; folder moves validate
against real temporary folders under ``tests/tmp``, removed in ``tearDown``.
The download test waits on events with timeouts, never bare sleeps.
"""

import os
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

SOURCE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, SOURCE_DIR)

import i18n
from manager_bridge import RpcFailure
from manager_settings import SettingsActions
from voice_catalog import available_languages, selectable_catalog

WAIT = 5.0
TMP_ROOT = os.path.join(os.path.dirname(__file__), "tmp")
ONE_LANGUAGE = next(entry["profile"] for entry in selectable_catalog() if len(available_languages(entry["profile"])) == 1)
MANY_LANGUAGES = next(entry["profile"] for entry in selectable_catalog() if len(available_languages(entry["profile"])) > 1)


class FakeMeetings:
    def __init__(self):
        self.workspace = {"generation": 4, "privacy_defaults": {"qa_mode": "explicit_save", "other": 1},
                          "retention_defaults": {"trash_days": 14, "raw_audio": {"mode": "delete", "tracks": ["system"]}}}
        self.updates = []

    def read_workspace(self):
        return self.workspace

    def update_workspace(self, patch, *, expected_generation):
        self.updates.append((patch, expected_generation))
        if expected_generation != self.workspace["generation"]:
            raise RuntimeError("generation conflict")
        self.workspace = dict(self.workspace, generation=expected_generation + 1, **patch)
        return self.workspace


class FakeVoice:
    def __init__(self, cache_dir):
        self.cache_dir = cache_dir
        self.downloads = []
        self.cancelled = []
        self.accept = True

    def status_snapshot(self):
        return {"download_active": False}

    def download_profile(self, profile):
        self.downloads.append(profile)
        return self.accept

    def cancel_profile_download(self, profile):
        self.cancelled.append(profile)
        return True


class FakeSummaryModels:
    def __init__(self):
        self.installed = set()
        self.started = threading.Event()
        self.block = False
        self.deleted = []

    def summary_model_is_installed(self, model_id):
        return model_id in self.installed

    def download_summary_model(self, model_id, *, cancel_event, progress):
        self.started.set()
        for done in (10, 20, 30):
            progress(done, 30)
        if self.block:
            cancel_event.wait(WAIT)
            raise RuntimeError("cancelled")
        self.installed.add(model_id)

    def delete_summary_model(self, model_id):
        self.deleted.append(model_id)
        self.installed.discard(model_id)


class SettingsTestCase(unittest.TestCase):
    def setUp(self):
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.tmp = tempfile.mkdtemp(dir=TMP_ROOT)
        self.saved = {"voice_hotkey": "ctrl+alt+space", "voice_command_hotkey": "ctrl+alt+shift+space",
                      "voice_profile": MANY_LANGUAGES}
        self.persisted = []
        self.changed = []
        self.notified = []
        self.finished = threading.Event()
        self.meetings = FakeMeetings()
        self.voice = FakeVoice(os.path.join(self.tmp, "voice-cache"))
        self.summary = FakeSummaryModels()
        self.requests = []
        self.data = {"path": os.path.join(self.tmp, "data"), "default": os.path.join(self.tmp, "data"), "env_locked": False}
        self.models_dir = {"path": os.path.join(self.tmp, "models"), "default": os.path.join(self.tmp, "models-default"),
                           "env_locked": False}
        os.makedirs(self.data["path"])
        os.makedirs(self.models_dir["path"])
        self.chosen = ""

        def persist(payload):
            self.persisted.append(payload)
            self.saved.update(payload)
            return True

        def notify(method, params):
            self.notified.append((method, params))
            if method == "models.summaryFinished":
                self.finished.set()

        self.actions = SettingsActions(
            meetings=self.meetings, voice=self.voice, settings_getter=lambda: dict(self.saved),
            persist_settings=persist, on_settings_changed=lambda: self.changed.append(True),
            summary_models=self.summary, choose_directory=lambda title, initial: self.chosen, notify=notify,
            data_location=lambda: self.data, models_location=lambda: self.models_dir,
            relocate_data=lambda target: self.requests.append(("data", target)) or "",
            relocate_models=lambda target: self.requests.append(("models", target)) or "",
        )
        self.env = mock.patch.dict(os.environ)
        self.env.start()
        for name in ("SNIPVOICE_HOME", "SNIPVOICE_VOICE_CACHE", "SNIPVOICE_SUMMARY_CACHE"):
            os.environ.pop(name, None)

    def tearDown(self):
        self.env.stop()
        i18n.set_language("pt-BR")
        shutil.rmtree(self.tmp, ignore_errors=True)


class RecordingTests(SettingsTestCase):
    def values(self, **overrides):
        values = dict(self.actions.recording({})["values"])
        values.update(overrides)
        return values

    def test_each_model_offers_only_the_languages_it_honors(self):
        view = self.actions.recording({})
        self.assertEqual([item["id"] for item in view["languages"][ONE_LANGUAGE]], ["auto"])
        self.assertGreater(len(view["languages"][MANY_LANGUAGES]), 1)
        self.assertEqual(view["profiles"][0]["id"], "dictation")

    def test_an_unhonored_language_is_refused_before_saving(self):
        with self.assertRaises(RpcFailure) as caught:
            self.actions.save_recording({"values": self.values(profile=ONE_LANGUAGE, language="pt-BR")})
        self.assertIn("Selecione auto", caught.exception.message)
        self.assertEqual(self.persisted, [])

    def test_the_destination_must_be_an_existing_absolute_folder(self):
        for path in ("relative/folder", os.path.join(self.tmp, "missing")):
            with self.subTest(path=path), self.assertRaises(RpcFailure):
                self.actions.save_recording({"values": self.values(destination=path)})
        self.assertEqual(self.persisted, [])

    def test_a_hotkey_that_collides_with_dictation_is_refused(self):
        with self.assertRaises(RpcFailure):
            self.actions.save_recording({"values": self.values(hotkey="ctrl+alt+space")})
        self.assertEqual(self.persisted, [])

    def test_saving_persists_the_meeting_keys_and_rebuilds_the_hotkey(self):
        view = self.actions.save_recording({"values": self.values(
            profile=MANY_LANGUAGES, language="en-US", hotkey="Ctrl+Alt+R", destination=self.tmp, voiceBoost=False)})
        self.assertEqual(self.persisted[0]["meeting_hotkey"], "ctrl+alt+r")
        self.assertEqual(self.persisted[0]["meeting_destination"], self.tmp)
        self.assertIs(self.persisted[0]["meeting_voice_boost"], False)
        self.assertEqual(self.changed, [True])
        self.assertEqual(view["values"]["language"], "en-US")

    def test_a_refused_save_says_so(self):
        self.actions.persist_settings = lambda _payload: False
        with self.assertRaises(RpcFailure):
            self.actions.save_recording({"values": self.values()})
        self.assertEqual(self.changed, [])


class PrivacyTests(SettingsTestCase):
    def form(self, **overrides):
        view = self.actions.privacy({})
        view.update(overrides)
        return view

    def test_defaults_read_from_the_workspace(self):
        view = self.actions.privacy({})
        self.assertEqual((view["generation"], view["trashDays"], view["autoTrash"]), (4, "14", False))
        self.assertEqual(view["autoTrashDays"], "90")

    def test_day_counts_are_checked(self):
        cases = [{"autoTrash": True, "autoTrashDays": ""}, {"trashDays": "abc"}, {"trashDays": "40000"},
                 {"qaMode": "forever"}, {"noticeLanguage": "fr-FR"}]
        for overrides in cases:
            with self.subTest(overrides=overrides), self.assertRaises(RpcFailure):
                self.actions.save_privacy(self.form(**overrides))
        self.assertEqual(self.meetings.updates, [])

    def test_saving_keeps_other_keys_and_disarms_raw_track_retention(self):
        view = self.actions.save_privacy(self.form(noticeEnabled=True, noticeLanguage="en-US", qaMode="memory_only",
                                                   autoTrash=True, autoTrashDays="60", trashDays=""))
        patch, generation = self.meetings.updates[0]
        self.assertEqual(generation, 4)
        self.assertEqual(patch["privacy_defaults"]["other"], 1)
        self.assertEqual(patch["privacy_defaults"]["recording_notice"], {"enabled": True, "language": "en-US"})
        self.assertEqual(patch["retention_defaults"]["whole_meeting"], {"mode": "whole_meeting", "after_days": 60.0})
        self.assertEqual(patch["retention_defaults"]["raw_audio"], {"mode": "keep", "tracks": []})
        self.assertEqual(patch["retention_defaults"]["trash_days"], 30.0)
        self.assertEqual((view["generation"], view["qaMode"]), (5, "memory_only"))
        self.assertEqual(self.changed, [True])

    def test_a_stale_generation_keeps_the_previous_options(self):
        with self.assertRaises(RpcFailure):
            self.actions.save_privacy(self.form(generation=2))
        self.assertEqual(self.changed, [])


class ModelTests(SettingsTestCase):
    def test_voice_downloads_ask_first_then_start(self):
        profile = selectable_catalog()[0]["profile"]
        first = self.actions.download_voice_model({"id": profile})
        self.assertEqual(first["status"], "confirm")
        self.assertEqual(self.voice.downloads, [])
        self.assertEqual(self.actions.download_voice_model({"id": profile, "confirmed": True}), {"status": "started"})
        self.voice.accept = False
        with self.assertRaises(RpcFailure):
            self.actions.download_voice_model({"id": profile, "confirmed": True})

    def test_a_summary_download_reports_progress_then_finishes(self):
        model_id = self.actions.models({})["summary"][0]["id"]
        self.assertEqual(self.actions.download_summary_model({"id": model_id})["status"], "confirm")
        self.actions.download_summary_model({"id": model_id, "confirmed": True})
        self.assertTrue(self.finished.wait(WAIT))
        methods = [method for method, _params in self.notified]
        self.assertEqual(methods[0], "models.summaryProgress")
        self.assertLess(methods.count("models.summaryProgress"), 3)
        self.assertTrue(self.notified[-1][1]["ok"])
        self.assertTrue(self.actions.models({})["summary"][0]["installed"])

    def test_a_cancelled_summary_download_says_its_bytes_are_kept(self):
        model_id = self.actions.models({})["summary"][0]["id"]
        self.summary.block = True
        self.actions.download_summary_model({"id": model_id, "confirmed": True})
        self.assertTrue(self.summary.started.wait(WAIT))
        with self.assertRaises(RpcFailure):
            self.actions.download_summary_model({"id": model_id, "confirmed": True})
        self.assertEqual(self.actions.models({})["summaryProgress"]["id"], model_id)
        self.actions.cancel_summary_model({})
        self.assertTrue(self.finished.wait(WAIT))
        outcome = self.notified[-1][1]
        self.assertTrue(outcome["cancelled"])
        self.assertIn("bytes parciais", outcome["message"])
        self.assertIsNone(self.actions.models({})["summaryProgress"])

    def test_removal_asks_first(self):
        model_id = self.actions.models({})["summary"][0]["id"]
        self.assertEqual(self.actions.delete_summary_model({"id": model_id})["status"], "confirm")
        self.assertEqual(self.summary.deleted, [])
        self.assertEqual(self.actions.delete_summary_model({"id": model_id, "confirmed": True})["status"], "removed")
        self.assertEqual(self.summary.deleted, [model_id])


class LocationTests(SettingsTestCase):
    def test_a_cancelled_dialog_plans_nothing(self):
        self.assertEqual(self.actions.plan_move({"kind": "data"}), {"target": None})

    def test_moving_data_into_itself_is_refused(self):
        self.chosen = os.path.join(self.data["path"], "inner")
        with self.assertRaises(RpcFailure):
            self.actions.plan_move({"kind": "data"})

    def test_a_planned_move_is_confirmed_then_requested(self):
        self.chosen = os.path.join(self.tmp, "elsewhere")
        plan = self.actions.plan_move({"kind": "data"})
        self.assertEqual(plan["target"], self.chosen)
        self.assertIn(self.chosen, plan["question"])
        self.assertEqual(self.requests, [])
        self.assertEqual(self.actions.move({"kind": "data", "target": plan["target"]}), {"moving": True})
        self.assertEqual(self.requests, [("data", self.chosen)])

    def test_restoring_the_default_models_folder_needs_no_dialog(self):
        plan = self.actions.plan_move({"kind": "models", "restoreDefault": True})
        self.assertEqual(plan["target"], self.models_dir["default"])

    def test_an_environment_locked_folder_cannot_move(self):
        self.data["env_locked"] = True
        with self.assertRaises(RpcFailure):
            self.actions.plan_move({"kind": "data", "restoreDefault": True})
        self.assertTrue(self.actions.locations_view({})["data"]["locked"])

    def test_the_engine_refusal_reaches_the_window(self):
        self.actions.locations["data"]["request"] = lambda _target: "Aguarde a gravação terminar."
        with self.assertRaises(RpcFailure) as caught:
            self.actions.move({"kind": "data", "target": os.path.join(self.tmp, "elsewhere")})
        self.assertEqual(caught.exception.message, "Aguarde a gravação terminar.")


if __name__ == "__main__":
    unittest.main()
