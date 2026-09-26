"""Summary template generation, saved versions, and stale callbacks."""

import unittest
from unittest.mock import Mock

from meeting_gui import MeetingWindow


class Variable:
    def __init__(self, value=""):
        self.value = value

    def get(self, *_args):
        return self.value

    def set(self, value):
        self.value = value


class SummaryFlowTests(unittest.TestCase):
    def setUp(self):
        self.view = view = MeetingWindow.__new__(MeetingWindow)
        view.closed = False
        view.selected = "session-1"
        view.detail_ready = True
        view.transcript_revision = "revision-1"
        view.summary_model = Variable("local-model")
        view.summary_model_installed = {"local-model": True}
        view.summary_focus = Variable("Prioritize confirmed next steps.")
        view.summary_status = Variable()
        view.status = Variable()
        view.summary_pending = None
        view.summary_generation = 0
        view.snapshot = {"state": "idle", "processing": False}
        self.profile = {"id": "meeting_notes", "name": "Notas de reunião", "builtin": True,
                        "sections": ["summary", "key_points", "decisions", "action_items"]}
        view._selected_report_profile = Mock(return_value=self.profile)
        view.controller = Mock()
        view._submit = Mock(return_value=True)
        view._sync_summary_controls = Mock()
        view._show_summary = Mock()
        view._remember_operation_error = Mock()
        view.refresh_reports = Mock()

    def test_generation_captures_template_focus_model_and_revision(self):
        view = self.view
        view.generate_report()
        self.profile["sections"].append("risks")
        view.summary_focus.set("Changed while running")
        view.transcript_revision = "new-revision"
        view._submit.call_args.args[1]()
        args, kwargs = view.controller.generate_report.call_args
        self.assertEqual(args, ("session-1", "local-model"))
        self.assertEqual(kwargs["focus"], "Prioritize confirmed next steps.")
        self.assertEqual(kwargs["revision"], "revision-1")
        self.assertNotIn("risks", kwargs["profile"]["sections"])
        self.assertEqual(view.summary_pending, ("session-1", 1))

    def test_empty_focus_is_optional_and_duplicate_generation_is_blocked(self):
        view = self.view
        view.summary_focus.set("  ")
        view.generate_report()
        view.generate_report()
        view._submit.assert_called_once()
        view._submit.call_args.args[1]()
        self.assertIsNone(view.controller.generate_report.call_args.kwargs["focus"])

    def test_success_selects_new_saved_version_and_displays_complete_result(self):
        view = self.view
        view.generate_report()
        result = {"summary": "Decisions were confirmed.", "key_points": [{"text": "Scope approved"}],
                  "report_id": "report-new"}
        view._submit.call_args.args[2](result, None)
        self.assertIsNone(view.summary_pending)
        view._show_summary.assert_called_once_with(result)
        view.refresh_reports.assert_called_once_with("session-1", preferred_report_id="report-new")

    def test_background_result_does_not_replace_another_recording(self):
        view = self.view
        view.generate_report()
        callback = view._submit.call_args.args[2]
        view.selected = "session-2"
        callback({"summary": "Result", "report_id": "new"}, None)
        self.assertIsNone(view.summary_pending)
        view._show_summary.assert_not_called()
        view.refresh_reports.assert_not_called()

    def test_error_and_queue_refusal_preserve_previous_summary(self):
        view = self.view
        view.generate_report()
        view._submit.call_args.args[2](None, "Model failed")
        self.assertIsNone(view.summary_pending)
        view._show_summary.assert_not_called()
        self.assertIn("preservado", view.summary_status.get())
        view._submit.return_value = False
        view.generate_report()
        self.assertIsNone(view.summary_pending)
        view._show_summary.assert_not_called()

    def test_old_callback_cannot_finish_new_generation(self):
        view = self.view
        view.generate_report()
        callback = view._submit.call_args.args[2]
        callback(None, "First failed")
        view.generate_report()
        callback({"summary": "Old"}, None)
        self.assertEqual(view.summary_pending, ("session-1", 2))
        view._show_summary.assert_not_called()

    def test_unavailable_model_missing_transcript_and_excess_focus_are_actionable(self):
        view = self.view
        view.summary_model_installed = {}
        view.generate_report()
        self.assertIn("Instale", view.summary_status.get())
        view.summary_model_installed = {"local-model": True}
        view.transcript_revision = None
        view.generate_report()
        self.assertIn("Transcreva", view.summary_status.get())
        view.transcript_revision = "revision-1"
        view.summary_focus.set("x" * 601)
        view.generate_report()
        self.assertIn("600", view.summary_status.get())
        view._submit.assert_not_called()

    def test_history_reopens_latest_summary_instead_of_saved_chat_answer(self):
        view = self.view
        view.report_profiles = [self.profile]
        view.report_history_box = Mock()
        view.report_history_choice = Variable()
        view.selected_report = None
        view._load_report = Mock()
        reports = [
            {"id": "old", "kind": "report", "profile_id": "meeting_notes"},
            {"id": "new", "kind": "report", "profile_id": "meeting_notes"},
            {"id": "answer", "kind": "qa"},
        ]
        view._set_report_history(reports)
        view._load_report.assert_called_once_with("new")
        view.selected_report = {"id": "old", "kind": "report"}
        view._load_report.reset_mock()
        view._set_report_history(reports, preferred_report_id="new")
        view._load_report.assert_called_once_with("new")

    def test_reviewed_and_specialized_sections_reach_primary_summary(self):
        view = self.view
        view.report_profiles = [self.profile]
        view.report_section_box = Mock()
        view.report_section_choice = Variable()
        view.report_provenance = Variable()
        view._render_report_section = Mock()
        view._render_report_citations = Mock()
        report = {"id": "saved", "kind": "report", "profile_id": "meeting_notes",
                  "generated": {"summary": {"text": "Generated"}, "feedback": [{"text": "Helpful"}]},
                  "reviewed_artifact": {"sections": {"summary": "Reviewed"}}}
        view._show_report(report)
        view._show_summary.assert_called_once_with({"summary": "Reviewed", "feedback": [{"text": "Helpful"}]})
        view._show_summary.reset_mock()
        view._show_report({"kind": "qa", "generated": {"answer": {"answer": "Saved chat"}}})
        view._show_summary.assert_not_called()

    def test_custom_format_selection_uses_personalization_hint(self):
        view = self.view
        view._selected_report_profile.return_value = {"id": "custom", "name": "Custom", "builtin": False}
        view.summary_format_hint = Variable()
        view._sync_report_profile_actions = Mock()
        view._report_profile_changed()
        self.assertIn("personalizado", view.summary_format_hint.get())


if __name__ == "__main__":
    unittest.main()
