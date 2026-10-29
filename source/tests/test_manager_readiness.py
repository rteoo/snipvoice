"""The Gravação readiness strip and playback for the Tauri manager.

Fakes stand in for the controllers; the rows come from the real
``ops_status.readiness_rows``, so both windows are held to the same text.
"""

import os
import sys
import types
import unittest

SOURCE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, SOURCE_DIR)

import i18n
from manager_bridge import RpcFailure
from manager_library import LibraryActions
from manager_readiness import ReadinessActions


class FakeMeetings:
    def __init__(self):
        self.profile = "balanced"
        self.state = {"state": "idle", "processing": False}
        self.items = []
        self.calls = []
        self.accept = True

    def installed_transcription_profile(self, preferred):
        if self.profile == "error":
            raise RuntimeError("inventory unavailable")
        return self.profile

    def list_sessions_page(self, **_kwargs):
        return {"items": self.items, "next_cursor": None}

    def snapshot(self):
        return dict(self.state)

    def seek_playback(self, session_id, track, start=0.0):
        self.calls.append(("seek", session_id, track, start))
        return self.accept

    def stop_playback(self):
        self.calls.append(("stop",))


def readiness(meetings, *, installed=True, ready=True, voice=None):
    summary = types.SimpleNamespace(summary_model_is_installed=lambda _model: installed)
    return ReadinessActions(
        meetings=meetings, voice=voice, summary_models=summary,
        settings_getter=lambda: {"meeting_profile": "balanced"},
        startup_status=lambda: (ready, "" if ready else "A recuperação local exige reconciliação manual."),
        voice_status=lambda: {"enabled": False, "state": "disabled", "status": ""},
    )


class ReadinessTests(unittest.TestCase):
    def tearDown(self):
        i18n.set_language("pt-BR")

    def test_a_ready_computer_says_so(self):
        view = readiness(FakeMeetings()).readiness({})
        self.assertEqual([row["key"] for row in view["rows"]], ["healthy"])

    def test_missing_models_and_failed_checks_name_their_action(self):
        meetings = FakeMeetings()
        meetings.profile = None
        view = readiness(meetings, installed=False).readiness({})
        self.assertEqual([row["key"] for row in view["rows"]], ["transcription", "summary"])
        self.assertTrue(all(row["action"] for row in view["rows"]))
        self.assertEqual(view["rows"][0]["tone"], "warning")
        meetings.profile = "error"
        self.assertEqual(readiness(meetings).readiness({})["rows"][0]["key"], "inventory")

    def test_startup_job_attention_and_dictation_rows(self):
        meetings = FakeMeetings()
        meetings.state = {"state": "idle", "processing": True, "processing_kind": "transcription"}
        meetings.items = [{"id": "s1", "status": "completed"}, {"id": "s2", "status": "failed"}]
        view = readiness(meetings, ready=False, voice=object()).readiness({})
        keys = [row["key"] for row in view["rows"]]
        self.assertEqual(keys, ["startup", "job", "attention", "dictation"])
        self.assertIn("reconciliação manual", view["rows"][0]["text"])
        self.assertEqual(view["attentionId"], "s2")


class PlaybackTests(unittest.TestCase):
    def actions(self, meetings):
        return LibraryActions(meetings=meetings, meeting_settings=lambda: None,
                              summary_model_installed=lambda _model: False,
                              choose_path=lambda *_args: "", startup_status=lambda: (True, ""))

    def test_playback_starts_from_the_requested_second(self):
        meetings = FakeMeetings()
        self.assertEqual(self.actions(meetings).play({"id": "s1", "start": 62}), {"accepted": True})
        self.assertEqual(meetings.calls, [("seek", "s1", "final", 62.0)])

    def test_playback_waits_for_the_capture_and_other_work(self):
        meetings = FakeMeetings()
        meetings.state = {"state": "recording"}
        with self.assertRaises(RpcFailure):
            self.actions(meetings).play({"id": "s1", "start": 0})
        meetings.state = {"state": "idle"}
        meetings.accept = False
        with self.assertRaises(RpcFailure):
            self.actions(meetings).play({"id": "s1", "start": 0})
        for start in (-1, "10", True):
            with self.subTest(start=start), self.assertRaises(RpcFailure):
                self.actions(meetings).play({"id": "s1", "start": start})

    def test_stop_asks_the_engine(self):
        meetings = FakeMeetings()
        self.assertEqual(self.actions(meetings).stop_playback({}), {"stopped": True})
        self.assertEqual(meetings.calls, [("stop",)])


if __name__ == "__main__":
    unittest.main()
