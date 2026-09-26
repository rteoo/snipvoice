"""Conversation isolation, async replies, and per-answer provenance in the GUI."""

import json
import unittest
from unittest.mock import Mock

from meeting_gui import MAX_CHAT_TURNS, MeetingWindow


class Variable:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class MeetingChatFlowTests(unittest.TestCase):
    def setUp(self):
        self.view = view = MeetingWindow.__new__(MeetingWindow)
        view.closed = False
        view.selected = "recording-1"
        view.detail_ready = True
        view.transcript_revision = "revision-1"
        view.summary_model = Variable("local-model")
        view.summary_model_installed = {"local-model": True}
        view.snapshot = {"state": "idle", "processing": False}
        view.ask_conversations = {}
        view.ask_pending = None
        view.ask_request = 0
        view.qa_mode = Variable("explicit_save")
        view.ask_status = Variable()
        view.status = Variable()
        view.controller = Mock()
        view.refresh_reports = Mock()
        view._remember_operation_error = Mock()
        view._submit = Mock(return_value=True)
        self.draft = Variable("What was decided?")
        view.meeting_chat = Mock()
        view.meeting_chat.get_question.side_effect = self.draft.get
        view.meeting_chat.set_question.side_effect = self.draft.set

    def answer(self, text="A review on Friday."):
        view = self.view
        callback = view._submit.call_args.args[2]
        callback({"answer": text, "citations": ["segment-1"], "uncertainty": "low",
                  "revision": "revision-1", "_provenance": {"model": {"id": "local-model"}}}, None)

    def test_followup_keeps_messages_and_sends_previous_exchange(self):
        view = self.view
        view.ask_this_meeting()
        self.assertEqual(self.draft.get(), "")
        self.assertEqual(view.ask_conversations[view.selected][0]["status"], "pending")
        view._submit.call_args.args[1]()
        self.assertEqual(view.controller.ask_this_meeting.call_args.kwargs["history"], [])
        self.answer()
        self.draft.set("Who owns that review?")
        view.ask_this_meeting()
        view._submit.call_args.args[1]()
        self.assertEqual(view.controller.ask_this_meeting.call_args.kwargs["history"], [
            {"question": "What was decided?", "answer": "A review on Friday."},
        ])
        self.answer("The owner was not specified.")
        turns = view.ask_conversations[view.selected]
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[0]["answer"], "A review on Friday.")
        self.assertEqual(turns[1]["answer"], "The owner was not specified.")
        view.controller.save_answer.assert_not_called()

    def test_pending_send_cannot_duplicate_question(self):
        self.view.ask_this_meeting()
        self.draft.set("Another question")
        self.view.ask_this_meeting()
        self.assertEqual(self.view._submit.call_count, 1)
        self.assertEqual(len(self.view.ask_conversations[self.view.selected]), 1)

    def test_background_reply_updates_only_its_own_recording(self):
        view = self.view
        view.ask_this_meeting()
        view.selected = "recording-2"
        view.meeting_chat.render.reset_mock()
        self.answer()
        self.assertEqual(view.ask_conversations["recording-1"][0]["answer"], "A review on Friday.")
        self.assertNotIn("recording-2", view.ask_conversations)
        view.meeting_chat.render.assert_not_called()
        self.assertIsNone(view.ask_pending)

    def test_failed_reply_keeps_question_available_for_retry(self):
        view = self.view
        view.ask_this_meeting()
        view._submit.call_args.args[2](None, "Local model failed")
        self.assertEqual(view.ask_conversations[view.selected][0]["status"], "error")
        self.assertEqual(self.draft.get(), "What was decided?")
        self.assertIsNone(view.ask_pending)

    def test_queue_rejection_recovers_send_state(self):
        self.view._submit.return_value = False
        self.view.ask_this_meeting()
        self.assertIsNone(self.view.ask_pending)
        self.assertEqual(self.draft.get(), "What was decided?")

    def test_each_answer_saves_with_its_original_model_question_and_revision(self):
        view = self.view
        view.ask_this_meeting()
        self.answer()
        view.summary_model.set("another-model")
        view.transcript_revision = "revision-2"
        view.save_answer(1)
        operation, callback = view._submit.call_args.args[1:]
        operation()
        args = view.controller.save_answer.call_args
        self.assertEqual(args.args[2], "local-model")
        self.assertEqual(args.kwargs, {"question": "What was decided?", "revision": "revision-1"})
        self.assertIn("_provenance", args.args[1])
        callback({"id": "saved-report"}, None)
        self.assertTrue(view.ask_conversations[view.selected][0]["saved"])
        self.assertEqual(view.ask_conversations[view.selected][0]["answer"], "A review on Friday.")

    def test_memory_only_blocks_per_answer_save(self):
        view = self.view
        view.ask_this_meeting()
        self.answer()
        view.qa_mode.set("memory_only")
        view._submit.reset_mock()
        view.save_answer(1)
        view._submit.assert_not_called()

    def test_citation_uses_own_answer_revision_not_selected_report(self):
        view = self.view
        view.ask_this_meeting()
        self.answer()
        view.selected_report = {"transcript_revision": "unrelated-revision"}
        view.transcript_revision = "newer-revision"
        view._jump_to_citation = Mock()
        view.jump_to_ask_citation(1, "segment-1")
        view._jump_to_citation.assert_called_once_with("segment-1", "revision-1")
        view.jump_to_ask_citation(1, "invented-source")
        self.assertEqual(view._jump_to_citation.call_count, 1)

    def test_new_chat_ignores_stale_reply_and_preserves_other_recordings(self):
        view = self.view
        view.ask_this_meeting()
        old_callback = view._submit.call_args.args[2]
        self.answer()
        view.ask_conversations["recording-2"] = [{"id": 99, "status": "complete"}]
        view.new_chat()
        old_callback({"answer": "late"}, None)
        self.assertNotIn("recording-1", view.ask_conversations)
        self.assertIn("recording-2", view.ask_conversations)

    def test_long_conversation_is_bounded_without_discarding_visible_answers(self):
        view = self.view
        view.ask_conversations[view.selected] = [{"id": index, "status": "complete"}
                                                  for index in range(MAX_CHAT_TURNS)]
        view.ask_this_meeting()
        view._submit.assert_not_called()
        self.assertEqual(len(view.ask_conversations[view.selected]), MAX_CHAT_TURNS)
        self.assertIn("Nova conversa", view.ask_status.get())

    def test_history_is_bounded_and_excludes_failed_answers(self):
        turns = [{"question": "🙂" * 2000, "answer": "🙂" * 4000, "status": "complete"}]
        history = MeetingWindow._chat_history(turns)
        self.assertEqual(len(history), 1)
        self.assertLessEqual(len(json.dumps(history, ensure_ascii=False).encode("utf-8")), 12_000)
        self.assertEqual(MeetingWindow._chat_history([{"status": "error"}]), [])


if __name__ == "__main__":
    unittest.main()
