import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from voice_support import VoiceController, STATE_UNAVAILABLE
from voice_runtime import VoiceRuntimeError


class InlineRunner:
    def start(self, fn, *args, name=None):
        fn(*args)


class MeetingReservationTests(unittest.TestCase):
    def setUp(self):
        directory = Path(__file__).resolve().parent / "tmp"
        directory.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=directory)
        self.addCleanup(self.tmp.cleanup)
        self.provider = mock.Mock()
        self.persist = mock.Mock()
        self.voice = VoiceController({"voice_enabled": False}, task_runner=InlineRunner(),
            insert_text=mock.Mock(), expand_trigger=mock.Mock(), notify=mock.Mock(),
            logger=None, provider=self.provider, persist_settings=self.persist,
            history_dir=os.path.join(self.tmp.name, "history"))

    def test_reservation_preserves_settings_and_closes_admission(self):
        token = self.voice.reserve_for_meeting()
        self.assertFalse(self.voice.settings.enabled)
        self.assertFalse(self.voice.handle_hotkey_press("dictation"))
        self.assertFalse(self.voice.download_profile("balanced"))
        self.assertFalse(self.voice.delete_active_model())
        self.persist.assert_not_called()
        self.voice.release_meeting(token)
        self.assertEqual(self.voice.state, STATE_UNAVAILABLE)

    def test_second_owner_and_wrong_release_are_rejected(self):
        token = self.voice.reserve_for_meeting()
        with self.assertRaises(VoiceRuntimeError):
            self.voice.reserve_for_meeting()
        self.voice.release_meeting(object())
        self.assertIn("ocupada", self.voice.status_label())
        self.voice.release_meeting(token)

    def test_unload_failure_does_not_grant_reservation(self):
        self.provider.unload.side_effect = RuntimeError("failed unload")
        with self.assertRaises(RuntimeError):
            self.voice.reserve_for_meeting()
        self.assertIsNone(self.voice._meeting_token)
