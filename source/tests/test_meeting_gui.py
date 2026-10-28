"""Workspace concurrency, selection, persistence, and shared-root smoke checks."""

from datetime import datetime, timedelta, timezone
import gc
import os
import sys
import threading
import time
import tkinter as tk
import types
import unittest
from tkinter import ttk
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from message_support import new_view, said
from meeting_gui import (
    APPEARANCE_LABELS, BackgroundBridge, INDEX_STATE_LABELS, MAX_MESSAGE_DETAILS,
    MAX_MESSAGE_TEXT, MAX_PAGE_BACKSTACK, MeetingWindow, Messages, PROFILE_LABELS,
    RECORDING_HINT, SOURCE_TEST_HINT, TRANSCRIPT_PAGE_SIZE,
    add_meeting_tabs, destination_display, endpoint_options, format_recording_status,
    format_silence, format_time, hotkey_display, lost_sources, meter_value,
    open_meeting_window, playback_sources, recording_tone, retention_plan_projection,
    ManagerViewState, manager_poll_interval,
    source_health, validated_settings, _append_detail_once, meter_decibels,
    _processing_issue, _stage_status,
)
from meeting_index import INDEX_STATES
import meeting_text
from meeting_text import format_local_timestamp
from meeting_settings import EndpointSelection, resolve_meeting_settings
import ui_theme


def wait_for(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Timed out waiting for background GUI work")
        time.sleep(0.005)


def descendants(widget):
    result = []
    for child in widget.winfo_children():
        result.append(child)
        result.extend(descendants(child))
    return result


class Variable:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class SettingsPersistenceLogicTests(unittest.TestCase):
    def test_trash_confirmation_withdraws_before_prepare_and_focuses_only_viewable_cancel(self):
        for already_viewable in (False, True):
            with self.subTest(already_viewable=already_viewable):
                view = self.make_view()
                view._trash_confirmation_text, view._wrap_label = mock.Mock(return_value="Synthetic"), mock.Mock()
                dialog, cancel = mock.Mock(), mock.Mock()
                events = []
                dialog.withdraw.side_effect = lambda: events.append("withdraw")
                dialog.deiconify.side_effect = lambda: events.append("present")
                dialog.grab_set.side_effect = lambda: events.append("grab")
                dialog.wait_window.side_effect = lambda: events.append("wait")
                cancel.winfo_viewable.return_value = already_viewable
                def visible():
                    events.append("cancel visible")
                    cancel.winfo_viewable.return_value = True
                def focus():
                    self.assertTrue(cancel.winfo_viewable())
                    events.append("focus")
                cancel.wait_visibility.side_effect, cancel.focus_set.side_effect = visible, focus
                view._button = mock.Mock(side_effect=lambda _parent, label, _command:
                                         cancel if label == "Cancelar" else mock.Mock())
                def prepare(*args, **kwargs):
                    self.assertEqual(events, ["withdraw"])
                    events.append("prepare")
                with mock.patch("meeting_gui.tk.Toplevel", return_value=dialog), \
                        mock.patch("meeting_gui.tk.Frame"), mock.patch("meeting_gui.tk.Text"), \
                        mock.patch("meeting_gui.ui_theme.prepare_dialog", side_effect=prepare):
                    self.assertFalse(view._confirm_trash({}, "Synthetic"))
                self.assertEqual(events[:3], ["withdraw", "prepare", "present"])
                self.assertEqual(events[-3:], ["grab", "focus", "wait"])
                dialog.wait_visibility.assert_not_called()
                self.assertEqual(cancel.wait_visibility.call_count, 0 if already_viewable else 1)

    def make_view(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.closed, view.dirty, view.settings_loaded = False, False, True
        view.ui = ui_theme.build_theme("dark", system="windows", preference="dark")
        view.raw_settings = {}
        view.settings = resolve_meeting_settings({})
        defaults = dict(profile="balanced", language="auto", hotkey="", summary_model="qwen3.5-2b-q4",
                        destination="", voice_boost=False, privacy_notice_enabled=False,
                        privacy_notice_language="pt-BR", qa_mode="explicit_save", whole_meeting_auto=False,
                        whole_meeting_after_days="90", trash_days="30")
        for name, value in defaults.items():
            setattr(view, name, Variable(value))
        for name in ("profile_display", "language_display", "summary_display", "destination_label"):
            setattr(view, name, Variable())
        view.language_box = mock.Mock()
        view.profile_box = mock.Mock()
        view.whole_meeting_days_entry = mock.Mock()
        view.privacy_validation_fields = {"whole_meeting_after_days": view.whole_meeting_days_entry}
        view.recording_validation_fields = {"profile": view.profile_box}
        view._sync_raw_policy_controls, view._sync_qa_controls = mock.Mock(), mock.Mock()
        view._sync_source_controls, view._sync_shortcut_hint = mock.Mock(), mock.Mock()
        view._settings_drafts = {key: dict(baseline=view._draft_values(key), saving=False, after=None,
                                          save=mock.Mock(), discard=mock.Mock(), status=mock.Mock())
                                 for key in ("recording", "privacy")}
        view._restored_settings_drafts = {}
        view.settings_sections = mock.Mock(current="recording")
        view.persist_settings, view._submit = mock.Mock(return_value=True), mock.Mock(return_value=True)
        view._say, view._fail, view.refresh_library = mock.Mock(), mock.Mock(), mock.Mock()
        view.on_settings_changed = None
        view.window = mock.Mock()
        view.input_enabled, view.output_enabled = Variable(False), Variable(True)
        return view

    def test_recording_save_and_discard_do_not_persist_or_reset_capture_choices(self):
        view = self.make_view()
        baseline = dict(view._settings_drafts["recording"]["baseline"])
        view.profile.set("accuracy")
        view.voice_boost.set(True)
        view.save_settings()
        operation, callback = view._submit.call_args.args[1:3]
        callback(operation(), None)
        payload = view.persist_settings.call_args.args[0]
        self.assertEqual(set(payload), {"meeting_profile", "meeting_language", "meeting_hotkey",
                                       "meeting_summary_model", "meeting_destination", "meeting_voice_boost"})
        self.assertEqual(payload["meeting_profile"], "accuracy")
        self.assertEqual((view.input_enabled.get(), view.output_enabled.get()), (False, True))
        view._settings_drafts["recording"]["baseline"] = baseline
        view.discard_settings_section("recording")
        self.assertEqual(view.profile.get(), "balanced")
        self.assertFalse(view.voice_boost.get())
        self.assertEqual((view.input_enabled.get(), view.output_enabled.get()), (False, True))

    def test_restore_overlays_async_settings_load_with_original_baseline(self):
        view = self.make_view()
        original = dict(view._settings_drafts["recording"]["baseline"])
        working = dict(original, profile="accuracy", hotkey="ctrl+shift+f8")
        view._settings_drafts["recording"]["baseline"] = None
        view._restored_settings_drafts = {"recording": {"values": working, "baseline": original}}
        view.options, view.devices = {}, []
        view._render_devices, view.refresh_report_profiles = mock.Mock(), mock.Mock()
        view._settings_loaded({"meeting_profile": "compact"}, None)
        self.assertEqual(view._draft_values("recording"), working)
        self.assertEqual(view._settings_drafts["recording"]["baseline"], original)
        view._settings_drafts["recording"]["save"].configure.assert_called_with(state="normal")

    def test_invalid_privacy_draft_disables_save_and_focuses_the_invalid_field(self):
        view = self.make_view()
        view.whole_meeting_auto.set(True)
        view.whole_meeting_after_days.set("nan")
        view._sync_settings_bar("privacy")
        view._settings_drafts["privacy"]["save"].configure.assert_called_with(state="disabled")
        self.assertEqual(view._invalid_settings_field, ("privacy", "whole_meeting_after_days"))
        self.assertFalse(view._save_settings_section("privacy"))
        view.whole_meeting_days_entry.focus_set.assert_called_once_with()
        view.persist_settings.assert_not_called()

    def test_rebuild_before_initial_settings_load_does_not_restore_a_loading_baseline(self):
        view = self.make_view()
        view._settings_drafts["recording"]["baseline"] = None
        saved = view.capture_settings_drafts()
        self.assertNotIn("recording", saved)
        view._restored_settings_drafts = saved
        view.profile.set("compact")
        view._section_loaded("recording")
        self.assertEqual(view._settings_drafts["recording"]["baseline"]["profile"], "compact")
        # A form that only loaded reports nothing; the receipt waits for a save.
        view._settings_drafts["recording"]["status"].configure.assert_called_with(
            text="", fg=view.ui.text_muted)

    def test_live_recording_draft_validation_never_stats_the_destination(self):
        view = self.make_view()
        view.destination.set(os.path.abspath(os.path.join("synthetic", "remote-folder")))
        view.hotkey.set("ctrl+shift+f8")
        with mock.patch("meeting_gui.os.path.isdir", side_effect=AssertionError("GUI directory IO")), \
                mock.patch("meeting_gui.os.stat", side_effect=AssertionError("GUI stat IO")):
            view._sync_settings_bar("recording")
            view._recording_settings_payload()
        view._settings_drafts["recording"]["save"].configure.assert_called_with(state="normal")

    def test_save_worker_directory_failure_keeps_draft_and_focuses_destination(self):
        view = self.make_view()
        path = os.path.abspath(os.path.join("synthetic", "missing-folder"))
        view.destination.set(path)
        target, label = mock.Mock(), mock.Mock()
        view.recording_validation_fields["destination"] = target
        view.recording_field_errors = {"destination": label}
        view.save_settings()
        operation, callback = view._submit.call_args.args[1:3]
        with mock.patch("meeting_gui.os.path.isdir", return_value=False):
            with self.assertRaises(ValueError) as failure:
                operation()
        callback(None, str(failure.exception))
        view.persist_settings.assert_not_called()
        self.assertEqual(view.destination.get(), path)
        self.assertNotEqual(view._draft_values("recording"), view._settings_drafts["recording"]["baseline"])
        target.focus_set.assert_called_once_with()
        self.assertIn("⚠", label.configure.call_args.kwargs["text"])

    def test_settings_leave_stay_discard_and_failed_save_are_truthful(self):
        view = self.make_view()
        view.hotkey.set("ctrl+shift+f8")
        after = mock.Mock()
        with mock.patch("meeting_gui.ui_theme.confirm_draft", return_value=None):
            view.request_leave_settings(after)
        after.assert_not_called()
        with mock.patch("meeting_gui.ui_theme.confirm_draft", return_value="save"):
            view.request_leave_settings(after)
        callback = view._submit.call_args.args[2]
        callback(None, "synthetic persistence failure")
        after.assert_not_called()
        self.assertNotEqual(view._draft_values("recording"), view._settings_drafts["recording"]["baseline"])
        with mock.patch("meeting_gui.ui_theme.confirm_draft", return_value="discard"):
            view.request_leave_settings(after)
        after.assert_called_once_with()
        self.assertEqual(view.hotkey.get(), "")

    def title_view(self):
        view = self.make_view()
        view.dirty, view.selected, view.detail_ready = True, "one", True
        view.title, view.appearance_display = Variable("Changed title"), Variable(APPEARANCE_LABELS["light"])
        view.appearance_status = Variable()
        view.on_appearance_changed = mock.Mock()
        view._sync_trash_controls = mock.Mock()
        return view

    def test_preference_cancel_precedes_persistence_and_restores_choice(self):
        view = self.title_view()
        with mock.patch("meeting_gui.messagebox.askyesnocancel", return_value=None):
            view.save_appearance()
        view._submit.assert_not_called()
        view.persist_settings.assert_not_called()
        self.assertEqual(view.appearance_display.get(), APPEARANCE_LABELS["dark"])

    def test_preference_waits_for_existing_title_save_without_prompting_again(self):
        view = self.title_view()
        view.save_title()
        title_callback = view._submit.call_args.args[2]
        with mock.patch("meeting_gui.messagebox.askyesnocancel", side_effect=AssertionError("duplicate prompt")):
            view.save_appearance()
        self.assertEqual(view._submit.call_count, 1)
        view.persist_settings.assert_not_called()
        title_callback(True, None)
        self.assertEqual(view._submit.call_args.args[0], "save_appearance")
        view._submit.call_args.args[1]()
        view.persist_settings.assert_called_once_with({"appearance": "light"})

    def test_failed_title_save_rolls_back_pending_preference_without_writing_it(self):
        view = self.title_view()
        with mock.patch("meeting_gui.messagebox.askyesnocancel", return_value=True):
            view.save_appearance()
        view._submit.call_args.args[2](False, "synthetic rename failure")
        view.persist_settings.assert_not_called()
        self.assertEqual(view.appearance_display.get(), APPEARANCE_LABELS["dark"])

    def test_cross_citation_enrichment_is_bounded_and_uses_canonical_text(self):
        view = self.make_view()
        view.controller = mock.Mock()
        view.controller.resolve_search_result.return_value = {
            "title": "Synthetic planning", "start": 125, "text": "canonical " * 100}
        labels = view._cross_source_labels(view.controller, [{"session_id": str(index)} for index in range(20)])
        self.assertEqual(len(labels), 16)
        self.assertEqual(view.controller.resolve_search_result.call_count, 16)
        self.assertTrue(all(label.startswith("Synthetic planning · 00:02:05 · canonical") for label in labels))
        self.assertLessEqual(max(map(len, labels)), 200 + 120 + 16)

    def initial_privacy_view(self):
        view = self.make_view()
        view._privacy_initial_loaded = view._workspace_initial_loaded = False
        view._settings_drafts["privacy"]["baseline"] = None
        view.privacy_status = Variable()
        view.privacy_defaults, view.retention_defaults = {}, {}
        view.privacy_save_inflight = False
        view.controller = mock.Mock()
        view.privacy_notice_check = mock.Mock()
        view.trash_days_entry = mock.Mock()
        return view

    def test_privacy_save_waits_for_both_initial_responses_in_either_order(self):
        for first in ("privacy", "workspace"):
            with self.subTest(first=first):
                view = self.initial_privacy_view()
                privacy = {"recording_notice": {"enabled": False, "language": "pt-BR"}, "qa_mode": "explicit_save"}
                workspace = {"generation": 3, "privacy_defaults": privacy,
                             "retention_defaults": {"trash_days": 90, "whole_meeting": {"mode": "keep"}}}
                responses = {"privacy": lambda: view._privacy_loaded(privacy, None),
                             "workspace": lambda: view._workspace_loaded(workspace, None)}
                responses[first]()
                self.assertIsNone(view._settings_drafts["privacy"]["baseline"])
                self.assertNotIn("carregada", view.privacy_status.get())
                view.privacy_notice_check.configure.assert_called_with(state="disabled")
                view.privacy_notice_enabled.set(True)
                view.save_privacy_settings()
                view._submit.assert_not_called()
                responses["workspace" if first == "privacy" else "privacy"]()
                self.assertEqual(view._settings_drafts["privacy"]["baseline"]["trash_days"], "90")
                view.privacy_notice_check.configure.assert_called_with(state="normal")
                view.privacy_notice_enabled.set(True)
                view.save_privacy_settings()
                view._submit.call_args.args[1]()
                submitted = view.controller.update_workspace.call_args.args[0]
                self.assertTrue(submitted["privacy_defaults"]["recording_notice"]["enabled"])
                self.assertEqual(submitted["retention_defaults"]["trash_days"], 90)

    def test_initial_privacy_or_workspace_failure_never_establishes_save_baseline(self):
        for failed in ("privacy", "workspace"):
            with self.subTest(failed=failed):
                view = self.initial_privacy_view()
                view._privacy_loaded({}, "synthetic unavailable" if failed == "privacy" else None)
                view._workspace_loaded({"retention_defaults": {"trash_days": 90}},
                                       "synthetic unavailable" if failed == "workspace" else None)
                self.assertIsNone(view._settings_drafts["privacy"]["baseline"])
                view.save_privacy_settings()
                view._submit.assert_not_called()
                self.assertNotIn("carregada", view.privacy_status.get())

    def test_delayed_privacy_restore_overlays_only_after_complete_initial_baseline(self):
        view = self.initial_privacy_view()
        baseline = dict(view._draft_values("privacy"), trash_days="90")
        working = dict(baseline, privacy_notice_enabled=True)
        view._restored_settings_drafts = {"privacy": {"baseline": baseline, "values": working}}
        view._privacy_loaded({}, None)
        self.assertIn("privacy", view._restored_settings_drafts)
        self.assertIsNone(view._settings_drafts["privacy"]["baseline"])
        view._workspace_loaded({"retention_defaults": {"trash_days": 90}}, None)
        self.assertEqual(view._draft_values("privacy"), working)
        self.assertEqual(view._settings_drafts["privacy"]["baseline"], baseline)

    def test_later_title_draft_survives_actual_matching_detail_delivery(self):
        view = self.make_view()
        view.selected, view.title = "one", Variable("Saved")
        view._restored_title_draft = ("one", "Later title edit", "Original baseline")
        for name in ("_explain_capture", "_set_audio_capabilities", "_render_detail_state", "_show_processing_issue",
                     "_sync_trash_controls", "_render_transcript", "_render_annotations",
                     "_update_transcript_paging_controls", "_show_summary", "_load_transcript_document", "refresh_reports"):
            setattr(view, name, mock.Mock())
        for name in ("playback_position", "playback_clock", "audio_overview"):
            setattr(view, name, Variable())
        view.playback_seek = mock.Mock()
        view._detail_loaded("one", ({"title": "Saved"}, []), None)
        self.assertEqual(view.title.get(), "Later title edit")
        self.assertEqual(view._saved_title, "Original baseline")
        self.assertTrue(view.dirty)
        self.assertIsNone(view._restored_title_draft)


class ReplayChoiceTests(unittest.TestCase):
    def test_final_mix_is_primary_even_after_raw_retention(self):
        tracks = {"microphone": {"available": False, "raw_removed": True},
                  "system": {"available": True}}
        self.assertEqual(playback_sources(tracks, True), ("Áudio final",))
        self.assertEqual(playback_sources(tracks, False), ())
        self.assertEqual(playback_sources({}, False), ())


class Text:
    def __init__(self, text=""):
        self.text = text
        self.state = "normal"
        self.tags = {}

    def get(self, *_args):
        return self.text

    def configure(self, **kwargs):
        self.state = kwargs.get("state", self.state)

    def delete(self, *_args):
        self.text = ""

    def insert(self, _position, text):
        self.text += text

    def edit_modified(self, *_args):
        return False

    def tag_add(self, tag, start, end):
        self.tags.setdefault(tag, []).append((start, end))

    def tag_configure(self, *_args, **_kwargs):
        pass


class MeetingGuiLogicTests(unittest.TestCase):
    def test_meter_decibels_are_actual_peak_dbfs_not_meter_normalization(self):
        for peak, expected in ((1, "0.0 dBFS"), (0.1, "-20.0 dBFS"), (0.001, "-60.0 dBFS"),
                               (0.00001, "-100.0 dBFS"), (2, "0.0 dBFS")):
            self.assertEqual(meter_decibels(peak), expected)
        for peak in (0, -1, None, "invalid", float("nan"), float("inf")):
            self.assertEqual(meter_decibels(peak), "sem sinal")

    def test_inline_timestamps_preserve_exact_text_and_use_line_local_indices_after_emoji(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.transcript_document = Text("00:00:02 Microfone: 😀 synthetic\n00:01:03 Sistema: next")
        text = view.transcript_document.get()
        view._tag_transcript_timestamps(text, ("one", "r1", 4))
        self.assertEqual(view.transcript_document.get(), text)
        self.assertEqual(view.transcript_document.tags["timestamp"], [("1.0", "1.8"), ("2.0", "2.8")])
        self.assertEqual(view._transcript_timestamps, {"timestamp_1": 2, "timestamp_2": 63})
        view.transcript_document.tag_names = lambda _index: ("timestamp", "timestamp_2")
        view.closed, view.detail_ready, view.transcript_document_ready = False, True, True
        view.selected, view.transcript_revision, view.transcript_document_request = "one", "r1", 4
        view.position, view.snapshot, view._action = Variable(), {"state": "idle"}, mock.Mock()
        view._seek_document_timestamp("2.0")
        view._action.assert_called_once_with("seek_playback", "one", "final", start=63.0, urgent=True)
        for field, stale in (("selected", "other"), ("transcript_revision", "r2"),
                             ("transcript_document_request", 5), ("closed", True)):
            old = getattr(view, field)
            setattr(view, field, stale)
            view._action.reset_mock()
            view._seek_document_timestamp("2.0")
            view._action.assert_not_called()
            setattr(view, field, old)

    def test_primary_text_stays_visible_in_both_styles_and_audio_navigation_is_opt_in(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.ui = ui_theme.build_theme("dark", system="windows")
        view.transcript_document_frame, view.timed_transcript = mock.Mock(), mock.Mock()
        view.transcript_processing_actions, view.transcript_audio_toggle = mock.Mock(), mock.Mock()
        view.transcript_style = Variable("Com horários")
        view.transcript_audio_expanded = False
        view._sync_transcript_style()
        view.transcript_document_frame.pack.assert_called_once()
        view.timed_transcript.pack_forget.assert_called_once()
        view._toggle_transcript_audio()
        self.assertTrue(view.transcript_audio_expanded)
        view.timed_transcript.pack.assert_called_once()
        view.transcript_style.set("Texto completo")
        view._sync_transcript_style()
        self.assertTrue(view.transcript_audio_expanded)

    def test_audio_page_beyond_truncated_primary_preview_keeps_bounded_seek_and_copy_contract(self):
        view = new_view()
        view.closed, view.selected, view.detail_ready = False, "one", True
        view.transcript_revision, view.transcript_request = "r1", 0
        view.transcript_document = Text("00:00:00 Microfone: limited preview")
        primary = view.transcript_document.get()
        view.transcript_document_ready, view.transcript_document_truncated = True, True
        view.transcript_offset, view.transcript_has_more = 0, True
        view.transcript, view.bridge = mock.Mock(), mock.Mock()
        view.transcript.get_children.return_value = []
        view.segments, view.segment_text, view.position = {}, Text(), Variable()
        view.transcript_timing = Variable()
        view.snapshot, view._action, view._submit = {"state": "idle"}, mock.Mock(), mock.Mock()
        view._read_transcript_page = mock.Mock(return_value={"offset": TRANSCRIPT_PAGE_SIZE,
            "has_previous": True, "has_more": True,
            "segments": [{"id": "later", "start": 600, "text": "Beyond preview"}]})
        view.change_transcript_page(1)
        operation, callback = view._submit.call_args.args[1:]
        callback(operation(), None)
        self.assertEqual(view.transcript_offset, TRANSCRIPT_PAGE_SIZE)
        view.transcript.selection.return_value = ("later",)
        view._play_transcript_selected()
        view._action.assert_called_once_with("seek_playback", "one", "final", start=600.0, urgent=True)
        self.assertEqual(view.segment_text.get(), "Beyond preview")
        self.assertEqual(view.transcript_document.get(), primary)
        view._submit.reset_mock()
        view.copy_transcript()
        view._submit.assert_not_called()
        self.assertEqual(view.transcript_document.get(), primary)

    def test_live_search_cancels_previous_timer_and_invalidates_old_reads(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.window = mock.Mock()
        view.bridge = mock.Mock()
        view._library_search_after = None
        view.library_filter_generation = 0
        view.refresh_library = mock.Mock()
        view._queue_library_search()
        first = view._library_search_after
        view._queue_library_search()
        view.window.after_cancel.assert_called_once_with(first)
        self.assertEqual(view.library_filter_generation, 2)
        self.assertEqual(view.bridge.invalidate.call_args_list, [mock.call("library"), mock.call("library")])
        delay, callback = view.window.after.call_args.args
        self.assertEqual(delay, 250)
        callback()
        view.refresh_library.assert_called_once_with()
        self.assertIsNone(view._library_search_after)

    def test_escape_clears_query_and_runs_one_immediate_search(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.query = Variable("query")
        view.search = mock.Mock()
        self.assertEqual(view._clear_library_search(), "break")
        self.assertEqual(view.query.get(), "")
        view.search.assert_called_once_with()

    def test_search_shortcut_selects_library_and_focuses_the_labelled_field(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.notebook, view.search_entry = mock.Mock(), mock.Mock()
        view.library_tab = object()
        self.assertEqual(view._focus_library_search(), "break")
        view.notebook.select.assert_called_once_with(view.library_tab)
        view.search_entry.focus_set.assert_called_once_with()
        view.search_entry.selection_range.assert_called_once_with(0, "end")

    def test_detail_cycling_is_user_navigation_and_wraps_in_both_directions(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.selected = "one"
        view._visible_destination = lambda: "library"
        view.detail_sections = mock.Mock(current="files")
        self.assertEqual(view._cycle_detail_section(1), "break")
        view.detail_sections.user_select.assert_called_with("transcript")
        view.detail_sections.current = "transcript"
        view._cycle_detail_section(-1)
        view.detail_sections.user_select.assert_called_with("files")

    def test_cancel_rename_restores_saved_title_without_dispatching(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.title = Variable("Unsaved")
        view._saved_title, view.dirty = "Saved", True
        view.save_title = mock.Mock()
        view._hide_title_editor = mock.Mock()
        self.assertEqual(view._end_title_edit(cancel=True), "break")
        self.assertEqual(view.title.get(), "Saved")
        self.assertFalse(view.dirty)
        view.save_title.assert_not_called()

    def test_title_header_reflows_only_at_its_readable_width_boundary(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.ui = types.SimpleNamespace(space_sm=8, space_xs=4)
        view.rename_button = mock.Mock()
        view.rename_button.winfo_reqwidth.return_value = 112
        view._reflow_title_header(types.SimpleNamespace(width=308))
        view.rename_button.grid.assert_called_once_with(
            row=1, column=0, sticky="w", padx=0, pady=(4, 0))
        view._reflow_title_header(types.SimpleNamespace(width=400))
        self.assertEqual(view.rename_button.grid.call_count, 1)
        view._reflow_title_header(types.SimpleNamespace(width=520))
        view.rename_button.grid.assert_called_with(
            row=0, column=1, sticky="e", padx=(8, 0), pady=0)
        view.rename_button.focus_set.assert_not_called()

    def test_plain_delete_confirmation_uses_the_plan_actual_restore_period(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.session_items = {"one": {"duration": 65}}
        projection = retention_plan_projection({"session_id": "one", "byte_estimate": 1500000,
                                                "policy": {"purge_after_days": 7}})
        text = view._trash_confirmation_text(projection, "Planning")
        for expected in ("Planning", "00:01:05", "1,5 MB", "7 dias"):
            self.assertIn(expected, text)
        self.assertNotIn("30 dias", text)

    def test_trash_confirmation_uses_only_a_known_duration_for_the_exact_recording(self):
        projection = retention_plan_projection({"session_id": "one", "byte_estimate": 1500000,
                                                "policy": {"purge_after_days": 7}})

        def confirmation(items=None, detail=None):
            view = MeetingWindow.__new__(MeetingWindow)
            view.session_items = items or {}
            view.detail_state_item = detail
            return view._trash_confirmation_text(projection, "Planning")

        self.assertIn("00:02:03", confirmation(detail={"id": "one", "duration": 123}))
        self.assertIn("Duração desconhecida", confirmation(detail={"id": "two", "duration": 123}))
        self.assertIn("Duração desconhecida", confirmation())
        for duration in (None, "invalid", -1, float("nan"), float("inf"), True):
            with self.subTest(duration=duration):
                self.assertIn("Duração desconhecida", confirmation({"one": {"duration": duration}}))
        self.assertIn("00:00:00", confirmation({"one": {"duration": 0}}))
        known_list = confirmation({"one": {"duration": 65}}, {"id": "one", "duration": 123})
        self.assertIn("00:01:05", known_list)
        self.assertNotIn("00:02:03", known_list)

    def test_undo_restores_the_deleted_session_even_after_selection_changes(self):
        view = new_view()
        view._undo_session, view.selected = "deleted", "another"
        view.undo_button, view.undo_receipt = mock.Mock(), mock.Mock()
        view._submit, view.refresh_library = mock.Mock(return_value=True), mock.Mock()
        view.controller = mock.Mock()
        view._undo_trash()
        key, operation, finished = view._submit.call_args.args
        self.assertEqual(key, "library_undo")
        operation()
        view.controller.restore_session.assert_called_once_with("deleted")
        finished(True, None)
        self.assertIsNone(view._undo_session)
        view.undo_receipt.pack_forget.assert_called_once_with()
        self.assertEqual(view.selected, "another")

    def test_undo_failure_keeps_a_retry_and_does_not_claim_restored(self):
        view = new_view()
        view._undo_session = "deleted"
        view.undo_button, view.undo_receipt = mock.Mock(), mock.Mock()
        view._submit, view.refresh_library = mock.Mock(return_value=True), mock.Mock()
        view.controller = mock.Mock()
        view._undo_trash()
        view._submit.call_args.args[2](False, None)
        self.assertEqual(view._undo_session, "deleted")
        view.undo_receipt.pack_forget.assert_not_called()
        view.refresh_library.assert_not_called()
        self.assertEqual(said(view)[0], "problem")

    def test_library_empty_state_offers_a_relevant_next_action(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.ui = ui_theme.build_theme("windows", system="windows")
        view.library_empty_panel = mock.Mock()
        view.library_empty = Variable()
        view.query = Variable()
        view.status_filter = Variable("Todos")
        view._library_filters = mock.Mock(return_value={"collection": None})
        for name in ("record", "import", "clear", "retry"):
            setattr(view, f"library_empty_{name}_button", mock.Mock())

        view._render_library_empty_state([])
        self.assertIn("Nenhuma gravação ainda", view.library_empty.get())
        view.library_empty_record_button.pack.assert_called_once()
        view.library_empty_import_button.pack.assert_called_once()

        view.query.set("missing")
        view._render_library_empty_state([])
        self.assertIn("Nenhuma gravação encontrada", view.library_empty.get())
        view.library_empty_clear_button.pack.assert_called_once()

        view._render_library_empty_state([{"id": "saved"}])
        view.library_empty_panel.place_forget.assert_called_once()

    def test_clear_library_search_resets_all_scopes_once(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.query = Variable("meeting")
        view.search = mock.Mock()

        view._clear_library_search()

        self.assertEqual(view.query.get(), "")
        view.search.assert_called_once_with()

    def test_transcript_double_click_seeks_without_playing_on_selection(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.transcript = mock.Mock()
        view.transcript.identify_row.return_value = "segment"
        view.transcript.selection.return_value = ("segment",)
        view.segments = {"segment": {"id": "segment", "start": 12.0}}
        view._transcript_selected = mock.Mock()
        view.play = mock.Mock()
        event = types.SimpleNamespace(y=8)
        self.assertEqual(view._play_transcript_click(event), "break")
        view.transcript.selection_set.assert_called_once_with("segment")
        view._transcript_selected.assert_called_once_with()
        view.play.assert_called_once_with()

        view.transcript.identify_row.return_value = ""
        self.assertEqual(view._play_transcript_click(event), "break")
        view.play.assert_called_once()

    def test_play_button_uses_seek_for_selected_meeting(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.selected = "meeting"
        view.snapshot = {"state": "idle"}
        view.position = Variable("12.5")
        view._action = mock.Mock()
        view.play()
        view._action.assert_called_once_with(
            "seek_playback", "meeting", "final", start=12.5, urgent=True,
        )

    def test_preview_status_distinguishes_signal_from_silence(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.preview_signature = ("both", "default:multimedia", "default:multimedia")
        view.preview_status = Variable()
        view._current_settings = mock.Mock(return_value=resolve_meeting_settings({}))
        view._previewed({
            "enabled": ("microphone", "system"),
            "peaks": {"microphone": 0.25, "system": 0.0}, "errors": (),
        }, None)
        self.assertEqual(
            view.preview_status.get(),
            "Microfone: sinal detectado · Sistema: sem sinal detectado.",
        )
        self.assertAlmostEqual(meter_value(0.01), 1 / 3)
        self.assertEqual(meter_value(float("nan")), 0.0)

    def test_every_selectable_profile_has_a_distinct_label(self):
        from voice_catalog import selectable_catalog
        labels = [PROFILE_LABELS[entry["profile"]] for entry in selectable_catalog()]
        self.assertEqual(len(labels), len(set(labels)))

    def test_models_list_copy_leads_with_the_selector_label(self):
        from voice_catalog import selectable_catalog
        for entry in selectable_catalog():
            self.assertTrue(entry["purpose"].startswith(PROFILE_LABELS[entry["profile"]]),
                            entry["profile"])

    def test_summary_selector_maps_translated_labels_back_to_model_ids(self):
        import i18n
        view = MeetingWindow.__new__(MeetingWindow)
        view.summary_model = Variable("qwen3.5-2b-q4")
        view.summary_display = Variable("High quality · Qwen3.5 4B")
        i18n.set_language("en-US")
        try:
            view._summary_display_changed()
        finally:
            i18n.set_language(i18n.DEFAULT_LANGUAGE)
        self.assertEqual(view.summary_model.get(), "qwen3.5-4b-q4")

    def test_appearance_save_persists_then_requests_a_safe_rebuild(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.appearance_display = Variable(APPEARANCE_LABELS["dark"])
        view.appearance_status = Variable()
        view.persist_settings = mock.Mock(return_value=True)
        view.on_appearance_changed = mock.Mock()
        view.raw_settings = {}
        view.window = mock.Mock()
        view.window.after_idle.side_effect = lambda callback: callback()
        view._remember_operation_error = mock.Mock()

        def submit(_key, operation, callback, urgent=False):
            self.assertTrue(urgent)
            callback(operation(), None)
            return True

        view._submit = submit
        view.save_appearance()

        view.persist_settings.assert_called_once_with({"appearance": "dark"})
        self.assertEqual(view.raw_settings["appearance"], "dark")
        view.on_appearance_changed.assert_called_once_with("dark")

    def test_invalid_appearance_label_never_persists(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.appearance_display = Variable("Solarized")
        view.appearance_status = Variable()
        view.persist_settings = mock.Mock()
        view.save_appearance()
        view.persist_settings.assert_not_called()
        self.assertIn("válida", view.appearance_status.get())

    def test_operation_error_redacts_drive_unc_and_spaced_paths(self):
        view = new_view()
        view._fail("library", "Falhou",
                   error=r"failed C:\Users\Alice Smith\meeting; then \\server\share\Bob Jones\audio")
        details = said(view)[3]
        self.assertNotIn("Alice", details)
        self.assertNotIn("server", details)
        self.assertNotIn("Bob", details)
        self.assertEqual(details.count("[caminho local]"), 2)

    def test_start_waits_for_inflight_privacy_save(self):
        view = new_view("recording")
        view.privacy_save_inflight = True
        view._validated_recording_request = mock.Mock()
        self.assertFalse(view.request_start("hotkey"))
        view._validated_recording_request.assert_not_called()
        kind, title, body, _details = said(view, "recording")
        self.assertEqual((kind, title), ("problem", "Gravação não iniciada"))
        self.assertIn("privacidade", body)

    def test_startup_failure_disables_recording_and_retention_controls(self):
        view = new_view("recording")
        view.start_button = mock.Mock()
        view.delete_button = mock.Mock()
        view.privacy_status = Variable()
        view.set_startup_status(False, r"failed at C:\Users\private\meeting")
        view.start_button.configure.assert_called_with(state="disabled")
        view.delete_button.configure.assert_called_with(state="disabled")
        _kind, title, body, _details = said(view, "recording")
        self.assertEqual(title, "A gravação aguarda a recuperação local")
        self.assertNotIn("C:\\Users", body)
        # It states a standing condition: leaving the page does not clear it,
        # and it cannot be dismissed, but recovery ends it.
        view.messages.leave("recording")
        self.assertIsNotNone(view.messages.current("recording"))
        self.assertTrue(view.messages.current("recording").sticky)
        view.set_startup_status(True, "")
        self.assertIsNone(view.messages.current("recording"))

    def test_shared_start_path_routes_window_hotkey_and_tray_to_notice(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.closed = False
        view.retention_ready = True
        view.privacy_ready = True
        view.controller = mock.Mock()
        view.controller.recording_notice_required.return_value = True
        view._validated_recording_request = mock.Mock(return_value=("settings", "title"))
        view._show_recording_notice = mock.Mock()
        for origin in ("window", "hotkey", "tray"):
            view.request_start(origin)
        self.assertEqual([item.args[0] for item in view._show_recording_notice.call_args_list],
                         ["window", "hotkey", "tray"])

    def test_start_consent_cancel_never_submits_and_confirm_is_one_start(self):
        view = new_view("recording")
        view.retention_ready = True
        view.privacy_ready = True
        view.controller = mock.Mock()
        view.controller.recording_notice_required.return_value = False
        settings = types.SimpleNamespace(sources="both")
        view._validated_recording_request = mock.Mock(return_value=(settings, "title"))
        view._submit = mock.Mock(return_value=True)
        self.assertTrue(view.request_start("hotkey"))
        operation = view._submit.call_args.args[1]
        operation()
        view.controller.start.assert_called_once_with(settings, title="title")
        view._started(False, None, "hotkey")
        view.controller.revoke_recording_consent.assert_called_once_with()

    def test_recording_notice_projection_never_discloses_local_paths(self):
        projection = retention_plan_projection({
            "session_id": "meeting-1",
            "operation": "whole_meeting",
            "eligible": True,
            "byte_estimate": 12,
            "targets": [{"kind": "bundle", "path": r"C:\Users\private\meetings\meeting-1", "bytes": 12}],
            "reasons": [r"could not inspect C:\Users\private\meetings\meeting-1"],
            "recovery_mode": "same-root-trash",
        })
        view = MeetingWindow.__new__(MeetingWindow)
        text = view._retention_preview_text(projection, "meeting-1")
        self.assertNotIn("C:\\Users", text)
        self.assertIn("[caminho local]", text)

    def test_follow_dictation_uses_the_current_dictation_model(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.profile = Variable("dictation")
        view.raw_settings = {"voice_profile": "balanced"}
        view.settings_getter = lambda: {"voice_profile": "whisper-small"}
        self.assertEqual(view._effective_profile(), "whisper-small")
        view.profile = Variable("accuracy")
        self.assertEqual(view._effective_profile(), "accuracy")

    def test_final_audio_capabilities_are_the_only_playback_contract(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.play_button = mock.Mock()
        view.transcribe_button = mock.Mock()
        view.export_audio_button = mock.Mock()
        view.recover_audio_button = mock.Mock()
        view.audio_source = Variable()
        view._set_audio_capabilities({
            "microphone": {"available": False, "raw_removed": True},
            "system": {"available": True},
        }, final_available=True)
        self.assertEqual(view.playback_choices, ("Áudio final",))
        self.assertEqual(view.audio_source.get(), "Áudio final")
        view.play_button.configure.assert_called_with(state="normal")
        view.transcribe_button.configure.assert_called_with(state="normal")
        view.recover_audio_button.configure.assert_called_with(
            text="Verificar e otimizar áudio", state="normal")

    def test_final_audio_recovery_remains_available_for_working_capture_tracks(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.play_button = mock.Mock()
        view.transcribe_button = mock.Mock()
        view.export_audio_button = mock.Mock()
        view.recover_audio_button = mock.Mock()
        view.audio_source = Variable()
        view._set_audio_capabilities({
            "microphone": {"available": True},
            "system": {"available": False, "raw_removed": True},
        }, final_available=True)
        self.assertTrue(view.recovery_needed)
        view.recover_audio_button.configure.assert_called_with(
            text="Verificar e otimizar áudio", state="normal")

    def test_play_starts_final_audio_in_the_english_ui(self):
        import i18n
        view = MeetingWindow.__new__(MeetingWindow)
        view.selected = "meeting-1"
        view.detail_ready = True
        view.snapshot = {"state": "idle"}
        view.playback_status = Variable()
        view.playback_position = Variable(0.0)
        view.playback_duration = 10.0
        view._action = mock.Mock()
        i18n.set_language("en-US")
        try:
            view.playback_choices = playback_sources({}, True)
            view.play_selected_recording()
        finally:
            i18n.set_language(i18n.DEFAULT_LANGUAGE)
        view._action.assert_called_once_with(
            "seek_playback", "meeting-1", "final", start=0.0,
            urgent=True, callback=view._replay_started)

    def test_missing_final_audio_enables_recovery_action(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.play_button = mock.Mock()
        view.transcribe_button = mock.Mock()
        view.export_audio_button = mock.Mock()
        view.recover_audio_button = mock.Mock()
        view.audio_source = Variable()
        view._set_audio_capabilities({}, final_available=False)
        self.assertEqual(view.playback_choices, ())
        view.recover_audio_button.configure.assert_called_with(
            text="Preparar áudio final", state="normal")
        view.export_audio_button.configure.assert_called_with(state="disabled")

    def test_prepare_final_audio_retries_verification_when_final_exists(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.selected = "meeting-1"
        view.detail_ready = True
        view.final_available = True
        view.recovery_needed = True
        view.controller = mock.Mock()
        view._action = mock.Mock()
        view.prepare_final_audio()
        self.assertEqual(view._action.call_args.args[:2], ("recover_audio", "meeting-1"))

    def _privacy_view(self):
        view = MeetingWindow.__new__(MeetingWindow)
        for name, value in (("privacy_notice_enabled", False), ("privacy_notice_language", "pt-BR"),
                            ("qa_mode", "explicit_save"), ("whole_meeting_auto", False),
                            ("whole_meeting_after_days", "90"), ("trash_days", "30"),
                            ("privacy_status", "")):
            setattr(view, name, Variable(value))
        view.privacy_defaults, view.retention_defaults = {}, {}
        view.workspace_generation = 0
        view.privacy_save_inflight = False
        view.controller = mock.Mock()
        view._submit = mock.Mock(return_value=True)
        view._sync_qa_controls = mock.Mock()
        return view

    def test_legacy_raw_policy_is_normalized_to_keep(self):
        view = self._privacy_view()
        view._apply_workspace_settings({"retention_defaults": {
            "raw_audio": {"mode": "raw_tracks", "after_days": 14.0, "tracks": ["microphone"]}}})
        view.save_privacy_settings()
        saved = view._submit.call_args
        self.assertEqual(saved.args[0], "save_privacy")
        saved.args[1]()
        patch = view.controller.update_workspace.call_args.args[0]["retention_defaults"]["raw_audio"]
        self.assertEqual(patch, {"mode": "keep", "tracks": []})

    def test_final_audio_policy_never_enables_legacy_raw_cleanup(self):
        view = self._privacy_view()
        view.save_privacy_settings()
        view._submit.call_args.args[1]()
        patch = view.controller.update_workspace.call_args.args[0]["retention_defaults"]["raw_audio"]
        self.assertEqual(patch["mode"], "keep")

    def test_meeting_auto_trash_setting_round_trips_and_needs_days(self):
        view = self._privacy_view()
        view._apply_workspace_settings({"retention_defaults": {
            "whole_meeting": {"mode": "whole_meeting", "after_days": 180.0}, "trash_days": 30.0}})
        self.assertTrue(view.whole_meeting_auto.get())
        self.assertEqual((view.whole_meeting_after_days.get(), view.trash_days.get()), ("180", "30"))
        view.save_privacy_settings()
        view._submit.call_args.args[1]()
        patch = view.controller.update_workspace.call_args.args[0]["retention_defaults"]["whole_meeting"]
        self.assertEqual(patch, {"mode": "whole_meeting", "after_days": 180.0})
        view = self._privacy_view()
        view.whole_meeting_auto.set(True)
        view.whole_meeting_after_days.set("")
        view.save_privacy_settings()
        view._submit.assert_not_called()

    def test_memory_only_q_and_a_does_not_submit_save(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.qa_mode = Variable("memory_only")
        view.ask_status = Variable()
        view.unsaved_answer = {"answer": "local", "question": "q"}
        view.selected = "meeting-1"
        view._sync_qa_controls = mock.Mock()
        view.ask_save_button = mock.Mock()
        view.save_answer()
        self.assertIn("memory_only", view.ask_status.get())

    def test_copy_summary_uses_rendered_readable_text(self):
        view = new_view()
        view.summary_text = "Resumo legível\n\nDecisões:\n• Enviar relatório"
        view._submit = mock.Mock(return_value=True)
        with mock.patch("meeting_gui.Clipboard.set_content", return_value=True) as copy:
            view.copy_summary()
            key, operation, callback = view._submit.call_args.args
            self.assertEqual(key, "copy_summary")
            operation()
            callback(True, None)
        copy.assert_called_once_with(view.summary_text)
        self.assertEqual(said(view)[:2], ("receipt", "Resumo copiado."))

    def test_a_failed_copy_is_trouble_on_the_page_not_a_quiet_line(self):
        view = new_view()
        view.summary_text = "Resumo"
        view._submit = mock.Mock(return_value=True)
        view.copy_summary()
        callback = view._submit.call_args.args[2]
        callback(False, None)
        kind, title, body, _details = said(view)
        self.assertEqual((kind, title), ("problem", "Não foi possível copiar o resumo"))
        self.assertIn("Tente de novo", body)
        callback(False, "clipboard busy")
        self.assertIn("clipboard busy", said(view)[3])

    def test_stale_trash_result_is_ignored_after_close_or_refresh(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.closed = False
        view.trash_request = 4
        view.trash_tree = mock.Mock()
        view.trash_status = Variable()
        view.trash_entries = []
        view._trash_loaded([], None, 3)
        view.trash_tree.delete.assert_not_called()

    def test_retention_delete_previews_and_applies_the_exact_plan(self):
        view = new_view()
        view.selected = "meeting-1"
        view.sessions = mock.Mock()
        view.sessions.selection.return_value = ("meeting-1",)
        view.detail_ready = True
        view.retention_ready = True
        view.retention_request = 0
        view.controller = mock.Mock()
        view.title = Variable("Planning")
        view.delete_button = mock.Mock()
        view.window = object()
        view._submit = mock.Mock(return_value=True)
        plan = {
            "session_id": "meeting-1",
            "operation": "whole_meeting",
            "eligible": True,
            "byte_estimate": 128,
            "targets": [{"path": r"C:\Users\private\meetings\meeting-1", "bytes": 128}],
            "reasons": [r"target C:\Users\private\meetings\meeting-1"],
            "recovery_mode": "same-root-trash",
            "excluded_external_exports": [r"D:\exports\meeting-1.wav"],
        }
        view.controller.retention_plan.return_value = plan

        view.delete_selected()
        preview_call = view._submit.call_args
        self.assertEqual(preview_call.args[0], "retention_plan")
        self.assertEqual(preview_call.args[1](), plan)
        view.controller.retention_plan.assert_called_once_with(
            "meeting-1", policy={"mode": "whole_meeting", "after_days": 0},
        )
        with mock.patch.object(view, "_confirm_trash", return_value=True) as confirm:
            preview_call.args[2](plan, None)
        projection, title = confirm.call_args.args
        prompt = view._trash_confirmation_text(projection, title)
        self.assertNotIn("C:\\Users", prompt)
        self.assertNotIn("D:\\exports", prompt)
        self.assertIn("30 dias", prompt)
        apply_call = view._submit.call_args_list[-1]
        self.assertEqual(apply_call.args[0], "retention_apply")
        apply_call.args[1]()
        view.controller.apply_retention.assert_called_once_with(plan, confirm=True)

    def _retention_view(self):
        view = self.make_edit_view()
        view.retention_ready, view.retention_request = True, 0
        view.window = mock.Mock()
        view._clear_library_detail = mock.Mock()
        view._show_trash_receipt = mock.Mock()
        plan = {"session_id": "one", "eligible": True,
                "policy": {"purge_after_days": 7}}
        view.controller.retention_plan.return_value = plan
        return view, plan

    def test_every_delete_route_rejects_a_different_or_unready_loaded_target(self):
        for state in ("different", "empty", "multiple", "wrong_detail", "loading", "saving", "recording", "recovery"):
            for route in ("delete_selected", "_delete_shortcut"):
                with self.subTest(state=state, route=route):
                    view, _plan = self._retention_view()
                    if state == "different":
                        view.sessions.selection.return_value = ("two",)
                    elif state == "empty":
                        view.sessions.selection.return_value = ()
                    elif state == "multiple":
                        view.sessions.selection.return_value = ("one", "two")
                    elif state == "wrong_detail":
                        view.detail_state_item = {"id": "two", "status": "completed"}
                    elif state == "loading":
                        view.detail_ready = False
                    elif state == "saving":
                        view._title_save_pending = ("one", "Title")
                    elif state == "recording":
                        view.detail_state_item = {"status": "recording"}
                    else:
                        view.retention_ready = False
                    getattr(view, route)()
                    view._submit.assert_not_called()
                    self.assertEqual(view.retention_request, 0)

    def test_loaded_search_detail_with_no_list_row_can_use_the_exact_trash_plan(self):
        view, plan = self._retention_view()
        view.sessions.selection.return_value = ()
        view.detail_state_item = {"id": "one", "status": "completed"}
        view.delete_selected()
        with mock.patch.object(view, "_confirm_trash", return_value=True):
            view._submit.call_args.args[2](plan, None)
        self.assertEqual(view._submit.call_args.args[0], "retention_apply")
        view._submit.call_args.args[1]()
        self.assertIs(view.controller.apply_retention.call_args.args[0], plan)
        self.assertEqual(view.controller.apply_retention.call_args.kwargs, {"confirm": True})

    def test_title_save_resyncs_current_trash_readiness_without_changing_context(self):
        for outcome in ("success", "false", "error", "refused"):
            for pending in (False, True):
                with self.subTest(outcome=outcome, retention_pending=pending):
                    view = self.make_edit_view()
                    after = mock.Mock()
                    view.save_title(after)
                    view.delete_button.configure.assert_called_with(state="disabled")
                    callback = view._submit.call_args.args[2]
                    # The user has already opened B before A's save finishes.
                    view.selected, view.dirty = "two", False
                    view.title.set("Other title")
                    view._saved_title = "Other title"
                    view.detail_state_item = {"id": "two", "status": "completed"}
                    view.sessions.selection.return_value = ("two",)
                    view._retention_pending = (1, "two") if pending else None
                    if outcome == "refused":
                        # A refused admission must release the same visible gate.
                        view._title_save_pending = None
                        view._submit.return_value = False
                        view.save_title(after)
                    else:
                        callback(outcome != "false", RuntimeError("synthetic save failure")
                                 if outcome == "error" else None)
                    view.delete_button.configure.assert_called_with(
                        state="disabled" if pending else "normal")
                    self.assertIsNone(view._title_save_pending)
                    self.assertEqual((view.selected, view.title.get(), view._saved_title),
                                     ("two", "Other title", "Other title"))
                    view.refresh_library.assert_not_called()
                    after.assert_not_called()

    def test_context_click_aborts_when_dirty_guard_cancels_or_waits_for_save(self):
        for answer in (None, True):
            with self.subTest(answer=answer):
                view, _plan = self._retention_view()
                view.sessions.identify_row.return_value = "two"
                view.sessions.get_children.return_value = ("one", "two")
                current = ["one"]
                view.sessions.selection.side_effect = lambda: tuple(current)
                view.sessions.selection_set.side_effect = lambda row: current.__setitem__(0, row)
                view.session_menu = mock.Mock()
                with mock.patch("meeting_gui.messagebox.askyesnocancel", return_value=answer):
                    view._show_session_menu(types.SimpleNamespace(num=3, y=50))
                view.session_menu.tk_popup.assert_not_called()
                self.assertEqual(view.selected, "one")
                if answer:
                    self.assertEqual(view._title_save_pending, ("one", "Title"))
                    view._delete_shortcut()
                self.assertFalse(any(call.args[0] == "retention_plan"
                                     for call in view._submit.call_args_list))

    def test_repeated_delete_during_preview_and_apply_keeps_the_admitted_callback(self):
        view, plan = self._retention_view()
        view.delete_selected()
        preview = view._submit.call_args.args[2]
        view._delete_shortcut()
        view.delete_selected()
        self.assertEqual(view._submit.call_count, 1)
        self.assertEqual(view.retention_request, 1)
        with mock.patch.object(view, "_confirm_trash", return_value=True):
            preview(plan, None)
        apply = view._submit.call_args.args[2]
        view._delete_shortcut()
        view.delete_selected()
        self.assertEqual(view._submit.call_count, 2)
        self.assertEqual(view.retention_request, 1)
        apply(True, None)
        self.assertIsNone(view._retention_pending)
        view._clear_library_detail.assert_called_once_with()
        view.refresh_library.assert_called_once_with()
        view._show_trash_receipt.assert_called_once_with("one")

    def test_preview_terminal_paths_release_admission_and_allow_retry(self):
        for outcome in ("error", "blocked", "refused", "cancel", "selection", "saving", "apply_refused"):
            with self.subTest(outcome=outcome):
                view, plan = self._retention_view()
                view.delete_selected()
                preview = view._submit.call_args.args[2]
                error = RuntimeError("synthetic preview failure") if outcome == "error" else None
                if outcome == "blocked":
                    plan = {**plan, "eligible": False}
                elif outcome == "refused":
                    plan = False
                elif outcome == "selection":
                    view.sessions.selection.return_value = ("two",)
                elif outcome == "saving":
                    view._title_save_pending = ("one", "Title")
                elif outcome == "apply_refused":
                    view._submit.return_value = False
                with mock.patch.object(view, "_confirm_trash", return_value=outcome != "cancel"), \
                        mock.patch("meeting_gui.messagebox.showwarning"):
                    preview(plan, error)
                self.assertIsNone(view._retention_pending)
                view._show_trash_receipt.assert_not_called()
                view.sessions.selection.return_value = ("one",)
                view._title_save_pending = None
                view._submit.return_value = True
                view.delete_selected()
                self.assertEqual(view._retention_pending, (2, "one"))

    def test_preview_admission_refusal_releases_gate_without_losing_next_request(self):
        view, _plan = self._retention_view()
        view._submit.return_value = False
        view.delete_selected()
        self.assertIsNone(view._retention_pending)
        view._submit.return_value = True
        view.delete_selected()
        self.assertEqual(view._retention_pending, (2, "one"))

    def test_apply_terminal_paths_release_gate_and_navigation_keeps_captured_undo(self):
        for outcome in ("error", "refused", "success", "navigated", "closed"):
            with self.subTest(outcome=outcome):
                view, plan = self._retention_view()
                view.delete_selected()
                with mock.patch.object(view, "_confirm_trash", return_value=True):
                    view._submit.call_args.args[2](plan, None)
                applied = view._submit.call_args.args[2]
                if outcome == "navigated":
                    view.selected = "two"
                    view.sessions.selection.return_value = ("two",)
                elif outcome == "closed":
                    view.closed = True
                applied(outcome != "refused", RuntimeError("synthetic apply failure")
                        if outcome == "error" else None)
                self.assertIsNone(view._retention_pending)
                if outcome in {"success", "navigated"}:
                    view.refresh_library.assert_called_once_with()
                    view._show_trash_receipt.assert_called_once_with("one")
                else:
                    view._show_trash_receipt.assert_not_called()
                if outcome == "navigated":
                    view._clear_library_detail.assert_not_called()
                    self.assertEqual(view.selected, "two")

    def test_selection_changed_inside_confirmation_does_not_apply_the_preview(self):
        view, plan = self._retention_view()
        view.delete_selected()
        def accept_after_selection_changes(*_args):
            view.sessions.selection.return_value = ("two",)
            return True
        with mock.patch.object(view, "_confirm_trash", side_effect=accept_after_selection_changes):
            view._submit.call_args.args[2](plan, None)
        self.assertIsNone(view._retention_pending)
        self.assertEqual(view._submit.call_count, 1)

    def test_trash_restore_purge_and_expired_empty_use_worker_confirmations(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.closed = False
        view.trash_request = 7
        view.trash_tree = mock.Mock()
        view.trash_tree.selection.return_value = ("0",)
        view.trash_entries = [{"session_id": "meeting-1", "deleted_at": "2026-09-01T12:00:00Z",
                               "purge_after": "2026-10-01T12:00:00Z", "byte_estimate": 48_213_377}]
        view.trash_status = Variable()
        view.trash_dialog = object()
        view.controller = mock.Mock()
        view._submit = mock.Mock(return_value=True)

        view.restore_selected_trash()
        restore_call = view._submit.call_args
        self.assertEqual(restore_call.args[0], "trash_restore")
        restore_call.args[1]()
        view.controller.restore_session.assert_called_once_with("meeting-1")

        # Without the typed word nothing is submitted.
        view._submit.reset_mock()
        view._confirm_permanent_delete = mock.Mock(return_value=False)
        view.purge_selected_trash()
        view.empty_expired_trash()
        view._submit.assert_not_called()

        view._confirm_permanent_delete = mock.Mock(return_value=True)
        view.purge_selected_trash()
        question, facts = view._confirm_permanent_delete.call_args.args
        self.assertEqual(question, "Excluir “meeting-1” definitivamente?")
        self.assertIn("48,2 MB", facts)
        purge_call = view._submit.call_args
        self.assertEqual(purge_call.args[0], "trash_purge")
        purge_call.args[1]()
        view.empty_expired_trash()
        self.assertEqual(view._confirm_permanent_delete.call_args.args[0],
                         "Excluir 1 gravação expirada definitivamente?")
        empty_call = view._submit.call_args
        self.assertEqual(empty_call.args[0], "trash_empty_expired")
        empty_call.args[1]()
        view.controller.purge_session.assert_called_once_with("meeting-1", confirm=True)
        view.controller.empty_trash.assert_called_once_with(confirm=True)

        # Nothing expired: no dialog and no purge.
        view.trash_entries[0]["purge_after"] = "2999-01-01T00:00:00Z"
        view._confirm_permanent_delete.reset_mock()
        view.empty_expired_trash()
        view._confirm_permanent_delete.assert_not_called()
        self.assertEqual(view.trash_status.get(), "Nenhuma gravação da lixeira expirou ainda.")

    def test_library_columns_give_way_duration_first_and_the_title_takes_the_rest(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view._session_column_widths = {"status": 110, "date": 129, "duration": 74}
        tree = view.sessions = mock.MagicMock()
        state = {"displaycolumns": ("status", "title")}
        tree.__getitem__.side_effect = lambda key: (
            ("status", "title", "date", "duration") if key == "columns" else state[key])
        tree.configure.side_effect = state.update
        # The tree draws a 2px edge on each side; the title keeps 190px.
        for width, shown, title in (
            (560, ("status", "title", "date", "duration"), 243),
            (450, ("status", "title", "date"), 207),
            # The smallest list keeps the title whole before it shows the date.
            (370, ("status", "title"), 256),
            # Room for the duration but not the date shows neither.
            (334, ("status", "title"), 220),
            (250, ("status", "title"), 190),
        ):
            with self.subTest(width=width):
                tree.winfo_width.return_value = width
                view._fit_session_columns()
                self.assertEqual(state["displaycolumns"], shown)
                tree.column.assert_called_with("title", width=title)

    def test_closing_the_trash_returns_focus_to_its_opener(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.trash_request = 3
        dialog = view.trash_dialog = mock.Mock()
        view.trash_tree = mock.Mock()
        opener = view.trash_opener = mock.Mock()
        opener.winfo_exists.return_value = True
        view._close_trash_dialog()
        dialog.destroy.assert_called_once_with()
        opener.focus_set.assert_called_once_with()
        self.assertIsNone(view.trash_tree)
        self.assertIsNone(view.trash_opener)
        self.assertEqual(view.trash_request, 4)

    def test_trash_names_tell_same_day_recordings_apart(self):
        # Store ids open with the local date, which repeats within a day.
        self.assertEqual(MeetingWindow._trash_name("20261020-091500-4f1c2a9e8b"), "…1c2a9e8b")
        self.assertEqual(MeetingWindow._trash_name("20261020-101500-77aa00bb11"), "…aa00bb11")
        self.assertEqual(MeetingWindow._trash_name("meeting-1"), "meeting-1")

    def test_detail_reads_the_first_failed_or_cancelled_step_in_pipeline_order(self):
        raw = {"processing": {
            "audio": {"status": "completed"},
            "summary": {"status": "failed", "error": "model unavailable"},
            "transcription": {"status": "cancelled", "error": None},
            "capture_cleanup": {"status": "skipped", "error": "kept"},
        }}
        self.assertEqual(_processing_issue(raw),
                         {"stage": "transcription", "status": "cancelled", "error": ""})
        self.assertEqual(_stage_status(raw, "transcription"), "cancelled")
        self.assertEqual(_stage_status(raw, "missing"), "")
        self.assertIsNone(_processing_issue({"processing": {"summary": {"status": "completed"}}}))
        self.assertIsNone(_processing_issue({}))

    def test_failed_and_pending_transcripts_explain_their_empty_text_differently(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.selected, view.library_processing_session = "s1", None
        texts = {}
        for status in ("", "pending", "processing", "failed", "cancelled"):
            view.transcription_status = status
            texts[status] = view._empty_transcript_text()
        self.assertEqual(texts[""], "A transcrição aparecerá após o processamento com um modelo local.")
        self.assertEqual(texts["pending"], texts[""])
        self.assertEqual(texts["processing"], texts[""])
        self.assertEqual(texts["failed"], "A transcrição falhou e esta gravação não tem texto.")
        self.assertIn("cancelada", texts["cancelled"])
        self.assertEqual(len({texts[""], texts["failed"], texts["cancelled"]}), 3)

    def test_a_job_on_the_selected_recording_outranks_a_saved_failure_in_the_empty_copy(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.selected = "s1"
        working = "Processando esta gravação. O resultado aparecerá aqui ao terminar."
        for status in ("", "pending", "failed", "cancelled"):
            view.transcription_status = status
            view.library_processing_session = "s1"
            self.assertEqual(view._empty_transcript_text(), working, status)
            # A job on another recording says nothing about this one.
            view.library_processing_session = "s2"
            self.assertNotEqual(view._empty_transcript_text(), working, status)
        view.selected, view.library_processing_session = None, None
        self.assertNotEqual(view._empty_transcript_text(), working)

    def test_label_allows_an_explicit_font_override(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.ui = mock.Mock(surface="surface", text="text")
        view.ui.font.return_value = "default-font"
        parent = mock.Mock()
        with mock.patch("meeting_gui.tk.Label") as label:
            view._label(parent, "Heading", font="bold-font")
        label.assert_called_once_with(
            parent, text="Heading", bg="surface", fg="text", font="bold-font",
        )

    def test_settings_mousewheel_routes_descendant_events_to_canvas(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.window = mock.Mock()
        region = types.SimpleNamespace(master=None)
        child = types.SimpleNamespace(master=region)
        canvas = mock.Mock()

        view._bind_mousewheel_region(region, canvas)

        callbacks = {
            call.args[0]: call.args[1] for call in view.window.bind.call_args_list
        }
        result = callbacks["<MouseWheel>"](
            types.SimpleNamespace(widget=child, delta=-120, num=None),
        )
        self.assertEqual(result, "break")
        canvas.yview_scroll.assert_called_once_with(1, "units")

        canvas.reset_mock()
        outside = types.SimpleNamespace(master=None)
        result = callbacks["<MouseWheel>"](
            types.SimpleNamespace(widget=outside, delta=-120, num=None),
        )
        self.assertIsNone(result)
        canvas.yview_scroll.assert_not_called()

    def test_library_query_has_no_manual_filter_payload(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.library_cursor = None
        view.library_next_cursor = None
        view.library_back_stack = [None]
        view.library_page_index = 0
        view.offset = 0
        view.library_filter_generation = 0
        view.query = Variable("planning")
        view.controller = mock.Mock()
        view.controller.list_sessions_page.return_value = {
            "items": [], "next_cursor": None,
        }
        view._submit = mock.Mock()

        view.refresh_library()
        operation = view._submit.call_args.args[1]
        operation()

        call = view.controller.list_sessions_page.call_args
        self.assertEqual(call.kwargs["query"], "planning")
        self.assertNotIn("collection", call.kwargs)
        self.assertNotIn("tag", call.kwargs)
        self.assertNotIn("person", call.kwargs)
        self.assertNotIn("series", call.kwargs)

    def test_library_cursor_backstack_is_bounded_and_stale_page_callback_is_ignored(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.library_next_cursor = "cursor"
        view.library_cursor = None
        view.library_back_stack = [None]
        view.library_page_index = 0
        view.offset = 0
        view.refresh_library = mock.Mock()
        for _index in range(MAX_PAGE_BACKSTACK + 8):
            view.change_page(1)
        self.assertLessEqual(len(view.library_back_stack), MAX_PAGE_BACKSTACK)
        self.assertEqual(view.library_page_index, len(view.library_back_stack) - 1)
        view.change_page(-1)
        self.assertEqual(view.library_cursor, view.library_back_stack[-1])

        stale = new_view()
        stale.library_filter_generation = 4
        stale.sessions = mock.Mock()
        stale._library_loaded(({"items": [], "next_cursor": None}, [], 3), None)
        stale.sessions.delete.assert_not_called()

    def test_keyset_page_uses_zero_offset_and_preserves_page_label_index(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.library_cursor = "cursor-page-2"
        view.library_page_index = 1
        view.library_filter_generation = 0
        view.query = Variable("")
        view.status_filter = Variable("Todos")
        view.collection_filter = Variable("")
        view.tag_filter = Variable("")
        view.people_filter = Variable("")
        view.series_filter = Variable("")
        view.date_from_filter = Variable("")
        view.date_to_filter = Variable("")
        view.controller = mock.Mock()
        view.controller.list_sessions_page.return_value = {
            "items": [{"id": "page-2", "title": "Page 2", "status": "completed"}],
            "next_cursor": None,
        }
        view._submit = mock.Mock()

        view.refresh_library()
        operation = view._submit.call_args.args[1]
        operation()

        call = view.controller.list_sessions_page.call_args
        self.assertEqual(call.kwargs["cursor"], "cursor-page-2")
        self.assertEqual(call.kwargs["offset"], 0)

    def test_cross_meeting_question_includes_visible_status_filter(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.cross_question = Variable("What was decided?")
        view.summary_model = Variable("local-model")
        view.cross_cancel_button = mock.Mock()
        view.cross_status = Variable()
        view.cross_request = 0
        view.closed = False
        view.controller = mock.Mock()
        view._submit = mock.Mock()

        view.ask_across_meetings()
        operation = view._submit.call_args.args[1]
        # A long worker must use the model captured on Tk, never read its StringVar.
        view.summary_model = mock.Mock()
        view.summary_model.get.side_effect = AssertionError("Worker touched a Tk variable")
        operation()

        view.controller.ask_across_meetings.assert_called_once_with(
            "What was decided?", "local-model",
            filters={}, cancel_event=view.cross_request_cancel,
        )

    def test_search_resolution_callback_ignores_stale_generation(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.closed = False
        view.search_request = 8
        view.selected = "current"
        view._jump_to_transcript_evidence = mock.Mock()

        view._apply_search_resolution({
            "source_kind": "transcript", "session_id": "current",
            "revision_id": "revision-1", "segment_id": "segment-1",
        }, request=7)

        view._jump_to_transcript_evidence.assert_not_called()

    def test_missing_manual_device_stays_pinned(self):
        selection = EndpointSelection("manual", "opaque-id")
        options = endpoint_options([], "system", selection)
        self.assertEqual(options[-1][1], selection)
        self.assertIn("indisponível", options[-1][0])
        self.assertNotIn("opaque-id", options[-1][0])
        self.assertEqual(options[1][1].argument(), "default:communications")

    def test_duplicate_names_are_disambiguated_without_showing_device_ids(self):
        options = endpoint_options([
            {"id": "{input-a}", "name": "Microfone USB", "kind": "microphone"},
            {"id": "{input-b}", "name": "Microfone USB", "kind": "microphone"},
            {"id": "c", "name": "Output", "kind": "system"},
        ], "microphone", EndpointSelection())
        self.assertEqual(
            [selection.endpoint_id for _, selection in options[2:]],
            ["{input-a}", "{input-b}"],
        )
        self.assertEqual([label for label, _selection in options[2:]],
                         ["Microfone USB", "Microfone USB (2)"])
        self.assertNotIn("{input", " ".join(label for label, _selection in options))

    def test_input_and_output_labels_keep_only_friendly_device_names(self):
        devices = [
            {"id": "{0.0.1.00000000}.input-code",
             "name": "Microfone (G522 LIGHTSPEED - USB Mode)", "kind": "microphone"},
            {"id": "{0.0.0.00000000}.output-code",
             "name": "Fones de ouvido (G522 LIGHTSPEED - USB Mode)", "kind": "system"},
        ]
        microphone = endpoint_options(devices, "microphone", EndpointSelection())
        system = endpoint_options(devices, "system", EndpointSelection())

        self.assertEqual(microphone[2][0], "Microfone (G522 LIGHTSPEED - USB Mode)")
        self.assertEqual(system[2][0], "Fones de ouvido (G522 LIGHTSPEED - USB Mode)")
        self.assertNotIn("input-code", microphone[2][0])
        self.assertNotIn("output-code", system[2][0])

    def test_identifier_only_devices_use_a_generic_user_label(self):
        identifier = "{0.0.1.00000000}.opaque"
        options = endpoint_options([
            {"id": identifier, "name": identifier, "kind": "microphone"},
        ], "microphone", EndpointSelection())
        self.assertEqual(options[2][0], "Dispositivo de entrada")

    def test_settings_reject_invalid_and_overlapping_shortcuts(self):
        for hotkey in ("ctrl+alt+space", "ctrl+space", "ctrl+alt+shift+cmd+space", "not+a+chord"):
            with self.subTest(hotkey=hotkey), self.assertRaises(ValueError):
                validated_settings({"meeting_hotkey": hotkey})
        self.assertEqual(validated_settings({}).hotkey, "")
        self.assertEqual(validated_settings({"meeting_hotkey": "ctrl+alt+r"}).hotkey, "ctrl+alt+r")

    def test_model_language_constraints_are_visible(self):
        with self.assertRaisesRegex(ValueError, "automática"):
            validated_settings({"meeting_profile": "compact", "meeting_language": "pt-BR"})

    def test_time_handles_gaps_and_invalid_values(self):
        self.assertEqual(format_time(3661.25), "01:01:01")
        self.assertEqual(format_time(float("nan")), "00:00:00")
        self.assertEqual(format_time(-10), "00:00:00")

    def test_recording_status_hides_paths_ids_and_technical_errors(self):
        status = format_recording_status({
            "state": "idle",
            "last_status": "partial",
            "elapsed": 31,
            "partial": True,
            "final_audio": r"C:\\recordings\\20260915-opaque-id.wav",
            "error": "native_discontinuity: HRESULT 0x88890004",
        })

        # The technical detail lives behind the page's Detalhes, not in the state line.
        self.assertEqual(status, "Pronto · 00:00:31 · Gravação preservada")
        self.assertNotIn("opaque-id", status)
        self.assertNotIn("HRESULT", status)

    def test_recording_status_shows_export_progress_instead_of_generic_processing(self):
        status = format_recording_status({
            "state": "postprocessing", "processing": True,
            "audio_export": {"stage": "encoding", "label": "Criando áudio final · 42%"},
        })
        self.assertIn("Criando áudio final · 42%", status)
        self.assertNotIn("Processando localmente", status)

    def test_recording_status_names_the_source_that_lost_its_audio(self):
        snapshot = {"state": "recording", "elapsed": 15, "capture_health": {
            "system": {"state": "recovering", "reason": "HRESULT synthetic-native-detail"}}}
        status = format_recording_status(snapshot)
        self.assertIn("Áudio do sistema: reconectando", status)
        self.assertNotIn("HRESULT", status)
        for healthy in ("receiving", "quiet", "waiting", "paused"):
            snapshot["capture_health"]["system"]["state"] = healthy
            self.assertNotIn("Áudio do sistema", format_recording_status(snapshot), healthy)
        snapshot["capture_health"]["system"]["state"] = "disconnected"
        status = format_recording_status(snapshot)
        self.assertEqual(status, "Gravando · 00:00:15 · Áudio do sistema: sem áudio")
        snapshot["capture_health"]["microphone"] = {"state": "disconnected", "reason": ""}
        self.assertEqual(
            format_recording_status(snapshot),
            "Gravando · 00:00:15 · Microfone: sem áudio · Áudio do sistema: sem áudio")
        # Outside a capture nothing is lost, whatever the last health said.
        snapshot["state"] = "idle"
        self.assertNotIn("sem áudio", format_recording_status(snapshot))

    def test_recording_status_never_reads_ready_while_start_is_held(self):
        held = (
            ({"state": "idle", "processing": True, "processing_kind": "report"}, False, "Processando"),
            ({"state": "postprocessing", "processing": True, "processing_kind": "summary"}, False,
             "Processando"),
            ({"state": "idle", "processing": True, "previewing": True, "processing_kind": "preview"}, False,
             "Testando"),
            ({"state": "idle", "final_copy": True}, False, "Aguardando"),
            ({"state": "idle", "playback": {"active": True}}, False, "Aguardando"),
            ({"state": "idle"}, True, "Aguardando"),
        )
        for snapshot, start_held, lead in held:
            status = format_recording_status(snapshot, start_held=start_held)
            self.assertTrue(status.startswith(lead + " · "), status)
        self.assertTrue(format_recording_status({"state": "idle"}).startswith("Pronto · "))
        # The lead names the processing, so the generic suffix no longer repeats it.
        self.assertEqual(format_recording_status(held[0][0]), "Processando · 00:00:00")

    @staticmethod
    def _closing_view(state):
        view = MeetingWindow.__new__(MeetingWindow)
        view.closed = False
        view.controller = mock.Mock()
        view.controller.snapshot.return_value = {"state": state, "processing": state == "postprocessing"}
        view.bridge = mock.Mock()
        view.root = mock.Mock()
        view.window = mock.Mock()
        view.after_id = None
        view._unbind_mousewheel_regions = mock.Mock()
        return view

    def test_close_never_cancels_a_capture_or_its_post_processing(self):
        for state in ("starting", "recording", "paused", "stopping", "postprocessing"):
            view = self._closing_view(state)
            # Even a chat or summary request this view still tracks.
            view.ask_pending, view.cross_pending, view.summary_pending = ("s1", 1), 2, ("s1", 3)
            view.close_without_prompt(destroy=False)
            view.controller.cancel_processing.assert_not_called()
            view.bridge.close.assert_called_once_with()
        view = self._closing_view("idle")
        view.close_without_prompt(destroy=False)
        view.controller.cancel_processing.assert_not_called()

    def test_close_cancels_only_this_views_interactive_jobs_while_idle(self):
        view = self._closing_view("idle")
        view.ask_pending, view.summary_pending = ("s1", 4), ("s2", 5)
        ask, summary = threading.Event(), threading.Event()
        view.ask_request_cancel, view.summary_request_cancel = ask, summary
        view.close_without_prompt(destroy=False)
        self.assertTrue(ask.is_set())
        self.assertTrue(summary.is_set())
        view.controller.cancel_processing.assert_not_called()
        self.assertIsNone(view.ask_pending)
        self.assertIsNone(view.cross_pending)

    def test_export_progress_is_scoped_to_selection_and_refreshes_final_before_asr_finishes(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.selected = "session"
        view.detail_ready = True
        view.audio_export_progress = mock.Mock()
        view.final_audio_progress = mock.Mock()
        for bar in (view.audio_export_progress, view.final_audio_progress):
            bar.cget.return_value = "determinate"
        view.final_audio_status = Variable()
        view.export_audio_button = mock.Mock()
        view.refresh_outputs = mock.Mock()
        view.snapshot = {"processing": True, "audio_export": {
            "session_id": "session", "stage": "encoding", "percent": 42,
            "label": "Criando áudio final · 42%",
        }}
        view._sync_audio_export_progress()
        self.assertEqual(view.final_audio_status.get(), "Criando áudio final · 42%")
        view.final_audio_progress.configure.assert_called_with(value=42)
        view.export_audio_button.configure.assert_called_with(state="disabled")
        view.selected = "other"
        view.final_audio_status.set("Other recording")
        view._sync_audio_export_progress()
        self.assertEqual(view.final_audio_status.get(), "Other recording")
        view.final_audio_progress.grid_remove.assert_called()
        view.selected = "session"
        view.snapshot["audio_export"]["stage"] = "completed"
        view.snapshot["audio_export"]["percent"] = 100
        view._sync_audio_export_progress()
        view._sync_audio_export_progress()
        self.assertTrue(view.snapshot["processing"])
        view.refresh_outputs.assert_called_once_with("session")

    def test_preparing_export_animation_restarts_after_hiding_without_duplicate_timers(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.selected = None
        view.detail_ready = False
        view.audio_export_progress = mock.Mock()
        view.audio_export_progress.cget.return_value = "indeterminate"
        view.snapshot = {"processing": True, "audio_export": {
            "session_id": "session", "stage": "preparing", "percent": None,
            "label": "Preparando o áudio final…",
        }}
        view._sync_audio_export_progress()
        view._sync_audio_export_progress()
        view.audio_export_progress.start.assert_called_once_with(50)
        view.snapshot["processing"] = False
        view._sync_audio_export_progress()
        view.snapshot["processing"] = True
        view._sync_audio_export_progress()
        self.assertEqual(view.audio_export_progress.start.call_count, 2)

    def test_final_audio_download_stays_enabled_while_summary_finishes(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.selected = "session"
        view.detail_ready = True
        view.final_available = True
        view.audio_export_progress = mock.Mock()
        view.final_audio_progress = mock.Mock()
        for bar in (view.audio_export_progress, view.final_audio_progress):
            bar.cget.return_value = "determinate"
        view.final_audio_status = Variable()
        view.export_audio_button = mock.Mock()
        view.recover_audio_button = mock.Mock()
        view.refresh_outputs = mock.Mock()
        view.snapshot = {"processing": True, "audio_export": {
            "session_id": "session", "stage": "completed", "percent": 100,
            "label": "Áudio final concluído",
        }}

        view._sync_audio_export_progress()

        view.export_audio_button.configure.assert_called_with(state="normal")

    def test_detailed_notes_editor_keeps_first_middle_and_last_notes(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.report_sections = {"detailed_notes": [
            {"start": 0, "end": 1, "text": "FIRST-" + "a" * 22000, "segment_ids": ["s1"]},
            {"start": 60, "end": 61, "text": "MIDDLE-" + "b" * 22000, "segment_ids": ["s2"]},
            {"start": 120, "end": 121, "text": "LAST-" + "c" * 22000, "segment_ids": ["s3"]},
        ]}
        view.report_section_choice = Variable("detailed_notes")
        view.report_editor = mock.Mock()

        view._render_report_section()

        rendered = view.report_editor.insert.call_args.args[1]
        self.assertIn("FIRST-", rendered)
        self.assertIn("MIDDLE-", rendered)
        self.assertIn("LAST-", rendered)
        self.assertGreater(len(rendered), 65536)

    def test_oversized_report_review_is_rejected_without_truncation(self):
        view = new_view()
        view.selected = "session"
        view.selected_report = {"id": "report", "reviewed_artifact": {"generation": 2}}
        view.report_section_choice = Variable("summary")
        view.report_editor = mock.Mock()
        view.report_editor.get.return_value = "x" * 65537
        view._action = mock.Mock()

        view.save_report_review()

        self.assertIn("Exporte a seção completa", said(view)[1])
        view._action.assert_not_called()

    def test_short_report_review_still_saves_in_full(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.selected = "session"
        view.selected_report = {"id": "report", "reviewed_artifact": {"generation": 2}}
        view.report_section_choice = Variable("summary")
        view.report_editor = mock.Mock()
        view.report_editor.get.return_value = "Reviewed text"
        view._action = mock.Mock()

        view.save_report_review()

        view._action.assert_called_once_with(
            "review_report", "session", "report", {"summary": "Reviewed text"},
            expected_generation=2, callback=mock.ANY,
        )

    def test_terminal_audio_stage_keeps_existing_file_downloadable(self):
        for stage in ("failed", "cancelled"):
            view = MeetingWindow.__new__(MeetingWindow)
            view.selected = "session"
            view.detail_ready = True
            view.final_available = True
            view.audio_export_progress = mock.Mock()
            view.final_audio_progress = mock.Mock()
            for bar in (view.audio_export_progress, view.final_audio_progress):
                bar.cget.return_value = "determinate"
            view.final_audio_status = Variable()
            view.export_audio_button = mock.Mock()
            view.recover_audio_button = mock.Mock()
            view.refresh_outputs = mock.Mock()
            view.snapshot = {"processing": True, "audio_export": {
                "session_id": "session", "stage": stage, "percent": None,
                "label": "Audio operation failed",
            }}

            view._sync_audio_export_progress()

            view.export_audio_button.configure.assert_called_with(state="normal")

    def test_empty_destination_has_a_clear_local_default_label(self):
        self.assertEqual(destination_display(""), "Pasta local padrão (recordings)")
        self.assertEqual(destination_display(r"C:\\Audio"), r"C:\\Audio")

    def test_custom_destination_can_return_to_the_local_default(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.destination = Variable(r"C:\\Audio")
        view.destination_label = Variable(r"C:\\Audio")

        view.use_default_destination()

        self.assertEqual(view.destination.get(), "")
        self.assertEqual(
            view.destination_label.get(), "Pasta local padrão (recordings)",
        )

    def test_processing_errors_are_written_on_the_page_with_the_cause_behind_details(self):
        view = new_view()

        view._processing_launched("meeting", False, "HRESULT 0x88890004")

        kind, title, body, details = said(view)
        self.assertEqual((kind, title), ("problem", "Não foi possível iniciar o processamento local"))
        self.assertNotIn("HRESULT", title + body)
        self.assertIn("HRESULT", details)
        # What happened, what it affects, what to do: and no pointer to another tab.
        self.assertNotIn("aba", body)
        self.assertEqual(view.messages.current("library").session, "meeting")

    def test_repeated_worker_error_detail_is_shown_once(self):
        details = ["Detalhes técnicos: native discontinuity"]
        _append_detail_once(details, "Detalhes técnicos: native discontinuity")
        _append_detail_once(details, "Detalhes técnicos: encoder failure")
        self.assertEqual(details, [
            "Detalhes técnicos: native discontinuity",
            "Detalhes técnicos: encoder failure",
        ])

    def test_summary_inventory_result_is_applied_only_on_gui_callback(self):
        view = MeetingWindow.__new__(MeetingWindow)
        button = mock.Mock()
        view.summary_model_buttons = {"qwen": button}
        view.summary_model_status = Variable()
        view.toggle_summary_model = mock.Mock()
        view._summary_inventory_loaded({"qwen": True}, None)
        self.assertEqual(view.summary_model_installed, {"qwen": True})
        options = button.configure.call_args.kwargs
        self.assertEqual((options["text"], options["state"]), ("Remover", "normal"))
        options["command"]()
        view.toggle_summary_model.assert_called_once_with("qwen")

    def test_summary_download_shows_license_and_uses_background_job(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.window = mock.Mock()
        view.summary_model_installed = {}
        view.summary_model_buttons = {"gemma-4-e2b-q4": mock.Mock()}
        view.summary_model_status = Variable()
        view.summary_progress_lock = threading.Lock()
        view.summary_progress = None
        view._submit = mock.Mock(return_value=True)
        with mock.patch("meeting_gui.messagebox.askyesno", return_value=True) as confirm, \
                mock.patch("meeting_gui.download_summary_model", return_value="model.gguf") as download:
            view.toggle_summary_model("gemma-4-e2b-q4")
            operation = view._submit.call_args.args[1]
            operation()
        self.assertIn("Apache-2.0", confirm.call_args.args[1])
        download.assert_called_once()
        self.assertIsNotNone(download.call_args.kwargs["cancel_event"])
        self.assertTrue(callable(download.call_args.kwargs["progress"]))

    def test_liquidai_first_download_requires_nonstandard_license_acceptance(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.window = mock.Mock()
        view.summary_model_installed = {}
        view.summary_model_buttons = {"lfm2.5-2.6b-q4": mock.Mock()}
        view.summary_model_status = Variable()
        view.summary_progress_lock = threading.Lock()
        view.summary_progress = None
        view._submit = mock.Mock(return_value=True)

        with mock.patch("meeting_gui.messagebox.askyesno", return_value=False) as confirm:
            view.toggle_summary_model("lfm2.5-2.6b-q4")

        self.assertEqual("Licença do modelo", confirm.call_args.args[0])
        prompt = confirm.call_args.args[1]
        self.assertIn("LFM Open License v1.0", prompt)
        self.assertIn("não é MIT nem Apache-2.0", prompt)
        self.assertIn("US$ 10 milhões", prompt)
        self.assertIn("Termos completos: https://", prompt)
        view._submit.assert_not_called()

    def test_initial_saved_manual_selection_overrides_constructor_defaults(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.raw_settings = {}
        view.settings = resolve_meeting_settings({})
        view.devices = []
        view.options = {"microphone": [("old default", EndpointSelection())]}
        view.endpoint_vars = {track: Variable("old default") for track in ("microphone", "system")}
        view.endpoint_boxes = {track: mock.Mock() for track in ("microphone", "system")}
        for name in ("profile", "language", "profile_display", "language_display", "hotkey",
                     "summary_model", "summary_display", "destination", "destination_label"):
            setattr(view, name, Variable())
        view.shortcut_hint = mock.Mock()
        view.input_enabled, view.output_enabled = Variable(True), Variable(True)
        view.auto_transcribe, view.auto_summary = Variable(False), Variable(False)
        view.voice_boost = Variable(False)
        view.voice_boost_check, view.auto_summary_check = mock.Mock(), mock.Mock()
        view.language_box = mock.Mock()
        view._settings_loaded({"meeting_microphone": {"mode": "manual", "endpoint_id": "missing"}}, None)
        self.assertTrue(view.settings_loaded)
        self.assertIn("indisponível", view.endpoint_vars["microphone"].get())
        self.assertEqual(dict(view.options["microphone"])[view.endpoint_vars["microphone"].get()].endpoint_id, "missing")

    def test_independent_source_switches_and_postprocessing_are_persisted(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.settings_loaded = True
        view.raw_settings = {}
        view.input_enabled, view.output_enabled = Variable(True), Variable(False)
        destination = os.path.abspath("recordings")
        view.destination = Variable(destination)
        view.auto_transcribe, view.auto_summary = Variable(True), Variable(True)
        view.voice_boost = Variable(True)
        view.profile, view.language = Variable("balanced"), Variable("pt-BR")
        view.hotkey, view.summary_model = Variable(""), Variable("lfm2.5-2.6b-q4")
        view.endpoint_vars = {"microphone": Variable("default"), "system": Variable("default")}
        view.options = {track: [("default", EndpointSelection())]
                        for track in ("microphone", "system")}

        settings = view._current_settings()

        self.assertEqual(settings.sources, "microphone")
        self.assertEqual(settings.destination, destination)
        self.assertTrue(settings.auto_transcribe)
        self.assertTrue(settings.auto_summary)
        self.assertTrue(settings.voice_boost)

    def make_edit_view(self):
        view = new_view()
        view.ui = ui_theme.build_theme("dark", system="windows")
        view.selected, view.detail_ready = "one", True
        view.sessions = mock.Mock()
        view.sessions.selection.return_value = ("one",)
        view.dirty = True
        view.title = Variable("Title")
        view.controller = mock.Mock()
        view.refresh_library = mock.Mock()
        view._submit = mock.Mock(return_value=True)
        view.delete_button = mock.Mock()
        return view

    def test_failed_save_preserves_unsaved_title_and_does_not_navigate(self):
        view = self.make_edit_view()
        after = mock.Mock()
        view.save_title(after)
        callback = view._submit.call_args.args[2]
        callback(False, None)
        self.assertTrue(view.dirty)
        after.assert_not_called()
        kind, title, body, _details = said(view)
        self.assertEqual((kind, title), ("problem", "Não foi possível salvar o título"))
        self.assertIn("continua salvo", body)

    def test_edit_during_save_is_not_cleared_or_discarded(self):
        view = self.make_edit_view()
        after = mock.Mock()
        view.save_title(after)
        callback = view._submit.call_args.args[2]
        view.title.set("New title")
        callback(True, None)
        self.assertTrue(view.dirty)
        after.assert_not_called()
        self.assertEqual(view._saved_title, "Title")

    def test_cancel_after_an_inflight_save_restores_the_last_committed_title(self):
        view = self.make_edit_view()
        view._saved_title = "Original"
        view._hide_title_editor = mock.Mock()
        view.save_title()
        callback = view._submit.call_args.args[2]
        view.title.set("Later edit")
        view._end_title_edit(cancel=True)
        self.assertEqual(view.title.get(), "Later edit")
        view._hide_title_editor.assert_not_called()
        callback(True, None)
        view._end_title_edit(cancel=True)
        self.assertEqual(view.title.get(), "Title")
        self.assertFalse(view.dirty)

    def test_loading_detail_prevents_saving_old_title_to_new_session(self):
        view = self.make_edit_view()
        view.detail_ready = False
        view.save_title()
        view._submit.assert_not_called()

    def test_save_title_dispatches_only_a_rename_and_clears_saved_changes(self):
        view = self.make_edit_view()
        after = mock.Mock()
        view.save_title(after)
        key, operation, callback = view._submit.call_args.args
        self.assertEqual(key, "save_title")
        operation()
        view.controller.rename_session.assert_called_once_with("one", "Title")
        view.controller.update_notes.assert_not_called()
        callback(True, None)
        self.assertFalse(view.dirty)
        after.assert_called_once_with()

    def test_delete_selected_requires_confirmation_and_runs_in_background(self):
        view = self.make_edit_view()
        view.window = mock.Mock()
        view.delete_button = mock.Mock()
        view._clear_library_detail = mock.Mock()
        view._show_trash_receipt = mock.Mock()

        with mock.patch("meeting_gui.messagebox.askyesno", return_value=True) as confirm:
            view.delete_selected()

        self.assertIn("não será apagado", confirm.call_args.args[1])
        key, operation, callback = view._submit.call_args.args
        self.assertEqual(key, "delete_session")
        operation()
        view.controller.delete_session.assert_called_once_with("one")
        callback(True, None)
        view._clear_library_detail.assert_called_once_with()
        view.refresh_library.assert_called_once_with()
        view._show_trash_receipt.assert_called_once_with("one")

    def test_delete_selected_cancel_preserves_the_recording(self):
        view = self.make_edit_view()
        view.window = mock.Mock()
        view.delete_button = mock.Mock()

        with mock.patch("meeting_gui.messagebox.askyesno", return_value=False):
            view.delete_selected()

        view._submit.assert_not_called()
        view.controller.delete_session.assert_not_called()

    def test_detail_projection_omits_legacy_notes_and_bounds_transcript_text(self):
        view = self.make_edit_view()
        view.detail_sections = mock.Mock()
        view.play_button = mock.Mock()
        view.replay_stop_button = mock.Mock()
        view.audio_overview = Variable()
        view.playback_status = Variable()
        view.playback_position = Variable(0.0)
        view.bridge, view.transcript = mock.Mock(), mock.Mock()
        view.transcript.get_children.return_value = []
        view.segment_text = Text()
        view.summary = Text()
        view.summary_model_installed = {}
        view.segments = {}
        view.session_items = {}
        view._render_detail_state = mock.Mock()
        view._show_processing_issue = mock.Mock()
        view.controller.get_session.return_value = {
            "id": "two", "notes": "Legacy notes",
            "bookmarks": [{"timestamp": 1, "label": "Legacy bookmark"}],
            "tracks": {"microphone": {"available": True, "raw_removed": False}},
            "events": ["private audio descriptors"],
        }
        view.controller.get_transcript.return_value = [{"text": "a" * 10000, "track": "microphone", "start": 0}]
        view.load_session("two")
        self.assertEqual(view._selection_generation, 1)
        view.detail_sections.select.assert_not_called()
        self.assertFalse(view.detail_ready)
        metadata, segments = view._submit.call_args.args[1]()
        self.assertNotIn("notes", metadata)
        self.assertNotIn("bookmarks", metadata)
        self.assertNotIn("events", metadata)
        self.assertIn("microphone", metadata["tracks"])
        self.assertEqual(len(segments[0]["text"]), 8000)

    def test_transcript_document_defaults_to_full_text_and_switches_style(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.closed = False
        view.selected = "meeting-1"
        view.detail_ready = True
        view.transcript_revision = "revision-1"
        view.transcript_style = Variable("Texto completo")
        view.transcript_document_request = 0
        view.transcript_document_ready = False
        view.transcript_document_truncated = False
        view.transcript_document = Text()
        view.copy_transcript_button = mock.Mock()
        view.transcript_document_status = Variable()
        view.bridge = mock.Mock()
        view.controller = mock.Mock()
        view.controller.get_transcript_preview.side_effect = [
            {"text": "texto corrido", "truncated": False},
            {"text": "00:00:00 Microfone: texto", "truncated": False},
        ]
        view._submit = mock.Mock()
        view._load_transcript_document()
        operation, callback = view._submit.call_args.args[1:]
        self.assertEqual(operation(), {"text": "texto corrido", "truncated": False})
        view.controller.get_transcript_preview.assert_called_once_with(
            "meeting-1", revision="revision-1", style="full_text",
        )
        callback({"text": "texto corrido", "truncated": False}, None)
        self.assertEqual(view.transcript_document.get(), "texto corrido")

        view.transcript_style.set("Com horários")
        view._sync_transcript_style = mock.Mock()
        view._load_transcript_document()
        operation, callback = view._submit.call_args.args[1:]
        operation()
        self.assertEqual(view.controller.get_transcript_preview.call_args.kwargs["style"], "timestamped")
        callback({"text": "00:00:00 Microfone: texto", "truncated": False}, None)
        self.assertEqual(view.transcript_document.get(), "00:00:00 Microfone: texto")

    def test_late_transcript_document_callback_cannot_replace_new_selection_or_style(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.closed = False
        view.selected = "old"
        view.detail_ready = True
        view.transcript_revision = "revision-1"
        view.transcript_style = Variable("Texto completo")
        view.transcript_document_request = 3
        view.transcript_document_ready = False
        view.transcript_document_truncated = False
        view.transcript_document = Text("new content")
        view.copy_transcript_button = mock.Mock()
        view.transcript_document_status = Variable()
        view.bridge = mock.Mock()
        view.controller = mock.Mock()
        view.controller.get_transcript_preview.return_value = {
            "text": "old content", "truncated": False,
        }
        view._submit = mock.Mock()
        view._load_transcript_document()
        callback = view._submit.call_args.args[2]
        # A newer style/selection request invalidates the old callback.
        view.transcript_style.set("Com horários")
        view.selected = "new"
        view.transcript_document_request += 1
        callback({"text": "old content", "truncated": False}, None)
        self.assertEqual(view.transcript_document.get(), "new content")

    def test_export_transcript_uses_selected_style_and_revision(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.selected = "meeting-1"
        view.detail_ready = True
        view.transcript_revision = "revision-2"
        view.transcript_style = Variable("Com horários")
        view.window = mock.Mock()
        view._action = mock.Mock()
        with mock.patch("meeting_gui.filedialog.asksaveasfilename", return_value="export.txt"):
            view.export_transcript()
        view._action.assert_called_once_with(
            "export_transcript", "meeting-1", "export.txt",
            style="timestamped", revision="revision-2",
            receipt="Exportação da transcrição iniciada.",
        )

    def test_final_audio_download_copies_the_saved_file_format(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.selected = "meeting-1"
        view.final_available = True
        view.audio_sizes = {"final": 1024, "final_format": "MP3"}
        view.title = Variable("Reunião")
        view.window = mock.Mock()
        view._action = mock.Mock()
        with mock.patch("meeting_gui.filedialog.asksaveasfilename", return_value="final.mp3") as dialog:
            view.export_audio()
        self.assertEqual(dialog.call_args.kwargs["defaultextension"], ".mp3")
        self.assertEqual(dialog.call_args.kwargs["filetypes"], (("Áudio MP3", "*.mp3"),))
        view._action.assert_called_once_with(
            "export_final_audio", "meeting-1", "final.mp3",
            receipt="Exportação do áudio final iniciada.",
        )

    def test_audio_download_suggestions_keep_titles_out_of_paths(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.selected = "meeting-1"
        view.final_available = True
        view.audio_sizes = {"final_format": "MP3"}
        view.window = mock.Mock()
        view._action = mock.Mock()
        for title, stem in (
            ("Reunião 14/10/2026", "Reunião 14-10-2026"),
            ('Review<>:"/\\|?*\x00\nNotes', "Review-Notes"),
            ("  Reunião final.  ", "Reunião final"),
            ("CON", "_CON"),
            ("...", "recording"),
            ("Reunião – orçamento", "Reunião – orçamento"),
        ):
            with self.subTest(title=title):
                view.title = Variable(title)
                with mock.patch("meeting_gui.filedialog.asksaveasfilename", return_value="") as dialog:
                    view.export_audio()
                self.assertEqual(dialog.call_args.kwargs["initialfile"], stem + ".mp3")
                self.assertEqual(view.title.get(), title)
        view._action.assert_not_called()

    def test_summary_display_is_readable_and_read_only(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.ui = ui_theme.build_theme("dark", system="windows")
        view.summary = Text()
        view.summary_status = Variable()
        view.summary_model_installed = {"local": True}
        view.summary_model = Variable("local")
        view._show_summary({
            "summary": "A reunião terminou.",
            "segment_ids": ["microphone:0:1"],
            "decisions": [{"text": "Enviar o relatório."}],
        })
        self.assertIn("A reunião terminou.", view.summary.get())
        self.assertIn("Enviar o relatório.", view.summary.get())
        self.assertNotIn("segment_ids", view.summary.get())
        self.assertEqual(view.summary.state, "disabled")

    def test_refresh_outputs_selects_final_audio_only_for_adjusted_recording(self):
        view = new_view()
        view.selected = "meeting-1"
        view.detail_ready = True
        view.transcript_revision = "revision-1"
        view.transcript_offset = 0
        view.audio_source = Variable("Microfone")
        view._adjusted_audio_target = "meeting-1"
        view.controller = mock.Mock()
        view.controller.get_session.return_value = {
            "summary": {"summary": "Resumo"},
            "tracks": {"microphone": {"available": True}},
            "final_audio": {"path": "final.wav"},
        }
        view._read_transcript_page = mock.Mock(return_value={
            "segments": [], "offset": 0, "has_previous": False, "has_more": False,
        })
        view._set_audio_capabilities = mock.Mock()
        view._show_summary = mock.Mock()
        view._render_transcript = mock.Mock()
        view._render_annotations = mock.Mock()
        view._update_transcript_paging_controls = mock.Mock()
        view._load_transcript_document = mock.Mock()
        view.refresh_reports = mock.Mock()
        view._show_processing_issue = mock.Mock()
        view._submit = mock.Mock()
        with mock.patch("meeting_gui.os.path.isfile", return_value=True):
            view.refresh_outputs("meeting-1")
            operation, callback = view._submit.call_args.args[1:]
            callback(operation(), None)
        self.assertEqual(view.audio_source.get(), "Áudio final")
        self.assertIsNone(view._adjusted_audio_target)

    def test_refreshed_outputs_report_the_selected_processing_stage_not_the_capture_error(self):
        for stage_status, incomplete in (("completed", False), ("skipped", False),
                                         ("failed", True), ("cancelled", True)):
            with self.subTest(stage_status=stage_status):
                view = new_view()
                view.selected = "meeting-1"
                view.detail_ready = True
                view.transcript_revision = "revision-1"
                view.transcript_offset = 0
                view.snapshot = {"error": "native_discontinuity" if not incomplete else ""}
                view.controller = mock.Mock()
                view.controller.get_session.return_value = {
                    "processing": {"summary": {"status": stage_status, "error": "model unavailable"}},
                    "error": "native_discontinuity",
                }
                view._read_transcript_page = mock.Mock(return_value={"segments": []})
                for name in ("_set_audio_capabilities", "_show_summary", "_render_transcript",
                             "_render_annotations", "_update_transcript_paging_controls",
                             "_load_transcript_document", "refresh_reports", "_submit",
                             "_show_processing_issue"):
                    setattr(view, name, mock.Mock())
                view.refresh_outputs("meeting-1")
                operation, callback = view._submit.call_args.args[1:]
                callback(operation(), None)
                # A failed stage is the processing InfoBar's to show, with its fix; the
                # capture's own error is not repeated, and the refresh itself is one quiet line.
                view._show_processing_issue.assert_called_once_with(
                    {"stage": "summary", "status": stage_status, "error": "model unavailable"}
                    if incomplete else None)
                self.assertEqual(said(view)[:2], (
                    "receipt", "Resultado local atualizado. Revise a transcrição e o resumo."))
                view.selected = "meeting-2"
                view.messages.receipt("library", "Selected another recording")
                callback(operation(), None)
                self.assertEqual(said(view)[1], "Selected another recording")

    def test_recording_finished_opens_clean_new_recording_and_tracks_processing(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.dirty = False
        view.processing_target = None
        view.refresh_library = mock.Mock()
        view.notebook = mock.Mock()
        view.library_tab = mock.Mock()
        view.load_session = mock.Mock()
        view._visible_destination = mock.Mock(return_value="recording")
        view._recording_finished({"session_id": "meeting-2", "processing": True})
        view.refresh_library.assert_called_once_with()
        view.notebook.select.assert_called_once_with(view.library_tab)
        view.load_session.assert_called_once_with("meeting-2")
        self.assertEqual(view.processing_target, "meeting-2")

    def test_recording_finished_does_not_move_a_user_who_left_recording(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.dirty = False
        view.refresh_library = mock.Mock()
        view.notebook = mock.Mock()
        view.load_session = mock.Mock()
        view._visible_destination = mock.Mock(return_value="settings")
        view._recording_finished({"session_id": "new-recording", "processing": True})
        view.notebook.select.assert_not_called()
        view.load_session.assert_not_called()

    def test_resetting_transcript_content_preserves_the_chosen_presentation(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.transcript_document = Text()
        view.transcript_document_request = 0
        view.bridge = mock.Mock()
        view.copy_transcript_button = mock.Mock()
        view.transcript_style = Variable("Com horários")
        view._sync_transcript_style = mock.Mock()
        view._reset_transcript_document()
        self.assertEqual(view.transcript_style.get(), "Com horários")

    def test_recording_finished_preserves_dirty_title_and_does_not_load_new_recording(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.dirty = True
        view.processing_target = None
        view.refresh_library = mock.Mock()
        view.notebook = mock.Mock()
        view.library_tab = mock.Mock()
        view.load_session = mock.Mock()
        view._recording_finished({"session_id": "meeting-2", "processing": False})
        view.refresh_library.assert_called_once_with()
        view.notebook.select.assert_not_called()
        view.load_session.assert_not_called()
        self.assertIsNone(view.processing_target)

    def test_report_history_projection_drops_payloads_and_keeps_only_metadata(self):
        view = MeetingWindow.__new__(MeetingWindow)
        row = view._report_history_metadata({
            "id": "report-1", "kind": "report", "profile_id": "general",
            "created_at": "2026-09-16T12:00:00Z",
            "generated": {"summary": "private transcript" * 10_000},
            "payload": {"private": "private transcript" * 10_000},
            "reviewed_artifact": {"sections": {"summary": "private"}},
        })

        self.assertEqual(row["id"], "report-1")
        self.assertNotIn("generated", row)
        self.assertNotIn("payload", row)
        self.assertNotIn("reviewed_artifact", row)

    def test_disabled_custom_profile_remains_visible_and_can_be_enabled(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.language = Variable("pt-BR")
        view.report_profile_choice = Variable("")
        view.report_profile_box = mock.Mock()
        view.report_enable_button = mock.Mock()
        view.report_disable_button = mock.Mock()
        view._set_report_profiles([{
            "id": "custom", "name": "Custom", "version": 2,
            "sections": ["summary"], "instructions": "", "disabled": True,
        }])

        label = view.report_profile_choice.get()
        self.assertIn("desativado", label)
        view.controller = mock.Mock()
        view._submit = mock.Mock(return_value=True)
        view.enable_report_profile()
        key, operation, _callback = view._submit.call_args.args
        self.assertEqual(key, "enable_report_profile")
        operation()
        view.controller.enable_report_profile.assert_called_once_with("custom")
        view.report_enable_button.configure.assert_any_call(state="normal")
        view.report_disable_button.configure.assert_any_call(state="disabled")

    def test_builtin_profile_can_be_duplicated_as_an_enabled_custom_copy(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.window = mock.Mock()
        view.report_profile_choice = Variable("Geral · v1")
        view.report_profile_by_label = {
            "Geral · v1": {
                "id": "general", "name": "Geral", "version": 1,
                "builtin": True, "language": "pt-BR", "instructions": "Use facts.",
                "sections": ["summary"],
            },
        }
        view._submit = mock.Mock(return_value=True)
        view.controller = mock.Mock()
        with mock.patch("meeting_gui.ui_theme.form_dialog", return_value={"name": "custom-copy", "instructions": "Use facts."}):
            view.duplicate_report_profile()

        operation = view._submit.call_args.args[1]
        operation()
        copied = view.controller.save_report_profile.call_args.args[0]
        self.assertEqual(copied["id"], "custom-copy")
        self.assertNotIn("builtin", copied)
        self.assertFalse(copied["disabled"])

    def test_create_profile_maps_automatic_language_to_portuguese(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.window = mock.Mock()
        view.language = Variable("auto")
        view.report_profile_choice = Variable("Geral · v1")
        view.report_profile_by_label = {
            "Geral · v1": {
                "id": "general", "name": "Geral", "version": 1,
                "builtin": True, "instructions": "Use facts.", "sections": ["summary"],
            },
        }
        view._submit = mock.Mock(return_value=True)
        view.controller = mock.Mock()
        with mock.patch(
            "meeting_gui.ui_theme.form_dialog",
            return_value={"name": "Perfil", "instructions": "Use facts."},
        ):
            view.create_report_profile()

        operation = view._submit.call_args.args[1]
        operation()
        created = view.controller.save_report_profile.call_args.args[0]
        self.assertEqual(created["language"], "pt-BR")

    def test_transcript_page_projection_is_bounded_and_keeps_navigation_metadata(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.controller = mock.Mock()
        view.controller.get_transcript_page.return_value = {
            "segments": [
                {"id": str(index), "start": index, "end": index + 1,
                 "track": "microphone", "text": "x" * 10000}
                for index in range(TRANSCRIPT_PAGE_SIZE + 1)
            ],
            "offset": TRANSCRIPT_PAGE_SIZE,
            "has_previous": True,
            "has_more": True,
        }
        page = view._read_transcript_page("session", "revision", TRANSCRIPT_PAGE_SIZE)
        self.assertEqual(len(page["segments"]), TRANSCRIPT_PAGE_SIZE)
        self.assertEqual(len(page["segments"][0]["text"]), 8000)
        self.assertTrue(page["has_previous"])
        self.assertTrue(page["has_more"])

    def test_transcript_fallback_consumes_only_one_bounded_page(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.controller = mock.Mock()
        view.controller.get_transcript_page.side_effect = AttributeError("legacy controller")
        consumed = []

        def segments():
            for index in range(10_000):
                consumed.append(index)
                yield {"id": str(index), "start": index, "end": index + 1,
                       "track": "microphone", "text": "bounded"}

        view.controller.get_transcript.return_value = segments()
        page = view._read_transcript_page("session", "revision", 0)
        self.assertEqual(len(page["segments"]), TRANSCRIPT_PAGE_SIZE)
        self.assertEqual(len(consumed), TRANSCRIPT_PAGE_SIZE + 1)
        self.assertTrue(page["has_more"])

    def test_stale_playback_snapshot_does_not_update_tk_state(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.closed = False
        view.playback_generation = 4
        view.playback_status = Variable("current")
        view.selected = "session"
        view.segments = {}
        view.transcript = mock.Mock()
        view._apply_playback_snapshot({
            "playback": {"generation": 3, "active": True, "position": 9,
                         "session_id": "session", "track": "microphone"},
        })
        self.assertEqual(view.playback_status.get(), "current")
        view._apply_playback_snapshot({
            "playback": {"generation": 5, "active": False, "position": 9,
                         "session_id": "session", "track": "microphone"},
        })
        self.assertEqual(view.playback_status.get(), "Reprodução parada.")

    def test_previous_recording_playback_does_not_replace_selected_status(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.closed = False
        view.playback_generation = 0
        view.playback_status = Variable("Carregando áudio…")
        view.selected = "new-session"
        view.replay_stop_button = mock.Mock()
        view._apply_playback_snapshot({
            "playback": {"generation": 1, "active": True, "position": 9,
                         "session_id": "old-session", "track": "microphone"},
        })
        self.assertEqual(view.playback_status.get(), "Carregando áudio…")
        view.replay_stop_button.configure.assert_called_once_with(state="disabled")

    def test_embedded_close_runs_owner_callback_without_destroying_shared_window(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.closed, view.dirty = False, False
        view.bridge, view.root, view.window = mock.Mock(), mock.Mock(), mock.Mock()
        view.after_id = None
        after_close = mock.Mock()

        view.close(destroy=False, after_close=after_close)

        self.assertTrue(view.closed)
        view.bridge.close.assert_called_once_with()
        view.window.destroy.assert_not_called()
        after_close.assert_called_once_with()

    def test_embedded_close_cancel_keeps_shared_window_open(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.closed, view.dirty = False, True
        view.bridge, view.root, view.window = mock.Mock(), mock.Mock(), mock.Mock()
        view.after_id = None
        after_close = mock.Mock()

        with mock.patch("meeting_gui.messagebox.askyesnocancel", return_value=None):
            view.close(destroy=False, after_close=after_close)

        self.assertFalse(view.closed)
        view.bridge.close.assert_not_called()
        after_close.assert_not_called()


class PageMessageTests(unittest.TestCase):
    """The per-page message slots: what a page says, how long, and about which recording."""

    def messages(self):
        self.drawn = []
        return Messages(lambda page, message: self.drawn.append((page, message and message.kind)),
                        clock=lambda: datetime(2026, 10, 20, 14, 2))

    def test_each_page_holds_one_message_and_a_receipt_replaces_trouble(self):
        messages = self.messages()
        messages.problem("library", "Falhou", "Corpo.", error="boom")
        messages.receipt("library", "Título salvo.")
        messages.receipt("settings", "Configurações salvas.")
        self.assertEqual(messages.current("library").kind, "receipt")
        self.assertEqual(messages.current("settings").title, "Configurações salvas.")
        self.assertIsNone(messages.current("recording"))
        self.assertEqual(messages.current("library").when, datetime(2026, 10, 20, 14, 2))
        self.assertEqual(self.drawn, [("library", "problem"), ("library", "receipt"),
                                      ("settings", "receipt")])
        with self.assertRaises(KeyError):
            messages.current("ditado")

    def test_details_and_receipts_are_bounded_and_redacted(self):
        messages = self.messages()
        messages.problem("library", "Falhou", error="x" * 20000)
        self.assertEqual(len(messages.current("library").details), MAX_MESSAGE_DETAILS)
        messages.problem("library", "Falhou", error=r"failed at C:\Users\Alice\meeting")
        self.assertNotIn("Alice", messages.current("library").details)
        messages.receipt("library", "uma linha " * 100 + "\n com quebra")
        line = messages.current("library").title
        self.assertLessEqual(len(line), MAX_MESSAGE_TEXT)
        self.assertNotIn("\n", line)

    def test_a_message_about_one_recording_goes_when_another_is_chosen(self):
        messages = self.messages()
        messages.problem("library", "Sobre A", session="a")
        messages.problem("settings", "Sem gravação")
        messages.problem("recording", "Sobre B", session="b")
        messages.drop_other_sessions("b")
        self.assertIsNone(messages.current("library"))
        self.assertEqual(messages.current("settings").title, "Sem gravação")
        self.assertEqual(messages.current("recording").title, "Sobre B")
        messages.drop_other_sessions()
        self.assertIsNone(messages.current("recording"))
        self.assertEqual(messages.current("settings").title, "Sem gravação")

    def test_leaving_a_page_clears_it_except_for_a_standing_condition(self):
        messages = self.messages()
        messages.problem("library", "Falhou")
        messages.problem("recording", "Recuperação pendente", key="startup", sticky=True)
        messages.leave("library")
        messages.leave("recording")
        self.assertIsNone(messages.current("library"))
        self.assertEqual(messages.current("recording").title, "Recuperação pendente")
        # A keyed clear only removes the message it names.
        messages.clear("recording", key="other")
        self.assertIsNotNone(messages.current("recording"))
        messages.clear("recording", key="startup")
        self.assertIsNone(messages.current("recording"))

    def test_actions_end_in_a_receipt_or_a_written_failure(self):
        view = new_view("library")
        view._done(True, None, "Exportação iniciada.")
        self.assertEqual(said(view)[:2], ("receipt", "Exportação iniciada."))
        # Routine success without a receipt says nothing and leaves the page as it is.
        view._done(True, None)
        self.assertEqual(said(view)[:2], ("receipt", "Exportação iniciada."))
        view._done(False, None)
        self.assertEqual(said(view)[:2], ("problem", "A operação não foi aceita"))
        self.assertEqual(view.messages.current("library").tone, "warning")
        view._done(None, "disk full")
        kind, title, _body, details = said(view)
        self.assertEqual((kind, title), ("problem", "Não foi possível concluir a operação"))
        self.assertIn("disk full", details)
        # With no page of its own, the outcome lands on the page in view.
        view._destination = "settings"
        view._done(None, "settings failure")
        self.assertIn("settings failure", said(view, "settings")[3])

    def test_a_capture_that_ended_badly_says_so_for_that_recording(self):
        view = new_view("library")
        view._explain_capture("s1", {"status": "completed"})
        self.assertIsNone(view.messages.current("library"))
        for status, tone, title in (("failed", "danger", "A captura falhou"),
                                    ("interrupted", "warning", "A captura foi interrompida"),
                                    ("partial", "warning", "A captura teve problemas")):
            with self.subTest(status=status):
                view._explain_capture("s1", {"status": status, "error": "native_discontinuity"})
                message = view.messages.current("library")
                self.assertEqual((message.tone, message.title, message.session), (tone, title, "s1"))
                self.assertIn("native_discontinuity", message.details)

    def test_async_action_feedback_stays_on_its_submitting_page(self):
        for value, error, kind in ((True, None, "receipt"),
                                   (False, None, "problem"),
                                   (None, "synthetic stop failure", "problem")):
            with self.subTest(value=value, error=error):
                view = new_view("recording")
                view.controller = types.SimpleNamespace(stop=lambda: value)
                view.bridge = mock.Mock()
                view.bridge.submit.return_value = True
                view._action("stop", receipt="Parada solicitada.")
                callback = view.bridge.submit.call_args.args[2]
                view._destination = "settings"
                callback(value, error)
                self.assertEqual(view.messages.current("recording").kind, kind)
                self.assertIsNone(view.messages.current("settings"))

    def test_default_submit_feedback_captures_page_before_navigation(self):
        view = new_view("settings")
        view.bridge = mock.Mock()
        view.bridge.submit.return_value = True
        view._submit("operation", lambda: None)
        callback = view.bridge.submit.call_args.args[2]
        view._destination = "library"
        callback(None, "synthetic settings failure")
        self.assertIn("synthetic settings failure", said(view, "settings")[3])
        self.assertIsNone(view.messages.current("library"))

    def test_async_library_feedback_cannot_describe_a_different_recording(self):
        view = new_view("library")
        view.selected = "recording-a"
        view.controller = types.SimpleNamespace(export=lambda: True)
        view.bridge = mock.Mock()
        view.bridge.submit.return_value = True
        view._action("export", receipt="Exportação iniciada.")
        callback = view.bridge.submit.call_args.args[2]
        view.selected = "recording-b"
        callback(True, None)
        self.assertIsNone(view.messages.current("library"))


class RecordingFeedbackLogicTests(unittest.TestCase):
    """Source health, silence and the recorder's tone, as pure projections of its snapshot."""

    @staticmethod
    def snapshot(state="recording", **health):
        return {"state": state, "capture_health": {
            track: {"state": value, "reason": "synthetic"} for track, value in health.items()}}

    def test_silence_is_written_with_at_most_two_units(self):
        self.assertEqual([format_silence(value) for value in (-3, 0, 12, 59.9, 65, 3599, 3725)],
                         ["0 s", "0 s", "12 s", "59 s", "1 min 05 s", "59 min 59 s", "1 h 02 min"])

    def test_only_a_running_capture_has_lost_sources(self):
        self.assertEqual(lost_sources(self.snapshot(system="disconnected")),
                         [("system", "disconnected")])
        self.assertEqual(
            lost_sources(self.snapshot("paused", microphone="recovering", system="disconnected")),
            [("microphone", "recovering"), ("system", "disconnected")])
        for state in ("idle", "postprocessing", "unavailable"):
            self.assertEqual(lost_sources(self.snapshot(state, system="disconnected")), [], state)
        for healthy in ("receiving", "quiet", "waiting", "paused", "failed"):
            self.assertEqual(lost_sources(self.snapshot(system=healthy)), [], healthy)
        self.assertEqual(lost_sources({"state": "recording", "capture_health": {"system": "bad"}}), [])

    def test_each_source_writes_its_health_under_its_meter(self):
        def both(value):
            return self.snapshot(microphone=value, system=value)

        self.assertEqual(source_health("system", self.snapshot("idle", system="receiving"), "both"),
                         ("", "text_muted"))
        for value, text, role in (
                ("receiving", "Recebendo sinal", "text_muted"),
                ("quiet", "Conectado · sem som no momento", "text_muted"),
                ("waiting", "Aguardando sinal…", "text_muted"),
                ("paused", "Em pausa", "text_muted"),
                ("disconnected", "Sem áudio", "warning"),
                ("recovering", "Reconectando…", "warning"),
                ("failed", "A captura desta fonte falhou", "danger")):
            with self.subTest(value=value):
                self.assertEqual(source_health("system", both(value), "both"), (text, role))
        self.assertEqual(source_health("system", both("disconnected"), "both", 65),
                         ("Sem áudio · há 1 min 05 s", "warning"))
        # A source this recording left out says so, whatever its device does.
        self.assertEqual(source_health("system", both("receiving"), "microphone"),
                         ("Não incluído nesta gravação", "text_muted"))
        self.assertEqual(source_health("microphone", both("receiving"), "microphone")[0],
                         "Recebendo sinal")

    def test_the_status_dot_is_copper_only_while_recording(self):
        ready = {"state": "idle"}
        self.assertEqual(recording_tone(ready, True), "success")
        self.assertEqual(recording_tone(ready, False), "info")
        self.assertEqual(recording_tone({"state": "idle", "processing": True}, True), "info")
        self.assertEqual(recording_tone({"state": "recording"}, True), "accent_mark")
        self.assertEqual(recording_tone({"state": "paused"}, True), "text_muted")
        self.assertEqual(recording_tone({"state": "unavailable"}, False), "warning")
        self.assertEqual(recording_tone({"state": "failed"}, False), "danger")
        self.assertEqual(recording_tone(self.snapshot(system="disconnected"), True), "warning")

    def test_a_shortcut_is_written_the_way_people_type_it(self):
        self.assertEqual(hotkey_display("ctrl+alt+r"), "Ctrl+Alt+R")
        self.assertEqual(hotkey_display("ctrl+shift+f12"), "Ctrl+Shift+F12")
        self.assertEqual(hotkey_display(""), "")
        self.assertEqual(hotkey_display(None), "")


class BackgroundBridgeTests(unittest.TestCase):
    def setUp(self):
        self.bridge = BackgroundBridge()
        self.release = threading.Event()

    def tearDown(self):
        self.bridge.close()
        self.release.set()
        for worker in self.bridge.workers:
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())

    def test_stale_results_do_not_replace_latest_request(self):
        entered = threading.Event()
        results = []
        def first():
            entered.set()
            self.release.wait(2)
            return "old"
        self.bridge.submit("detail", first, lambda value, error: results.append(value))
        self.assertTrue(entered.wait(1))
        self.bridge.submit("detail", lambda: "new", lambda value, error: results.append(value))
        self.release.set()
        wait_for(lambda: self.bridge.results.qsize() == 2)
        self.bridge.drain()
        self.assertEqual(results, ["new"])

    def test_controls_run_during_slow_io_and_callbacks_run_only_on_caller(self):
        entered, controlled = threading.Event(), threading.Event()
        callback_threads = []
        operation_threads = []
        def slow():
            entered.set()
            self.release.wait(2)
        self.bridge.submit("disk", slow, lambda *_args: None)
        self.assertTrue(entered.wait(1))
        def stop():
            operation_threads.append(threading.get_ident())
            controlled.set()
        self.bridge.submit("control", stop, lambda *_args: callback_threads.append(threading.get_ident()), urgent=True)
        self.assertTrue(controlled.wait(1))
        wait_for(lambda: self.bridge.results.qsize() >= 1)
        self.assertEqual(callback_threads, [])
        self.bridge.drain()
        self.assertEqual(callback_threads, [threading.get_ident()])
        self.assertNotEqual(operation_threads, callback_threads)

    def test_close_blocks_late_callbacks(self):
        callback = mock.Mock()
        self.bridge.submit("detail", lambda: "result", callback)
        wait_for(lambda: self.bridge.results.qsize() == 1)
        self.bridge.close()
        self.bridge.drain()
        callback.assert_not_called()
        self.assertFalse(self.bridge.submit("late", lambda: None, callback))

    def test_job_queue_is_bounded_and_overflow_invalidates_pending_result(self):
        entered = threading.Event()
        def slow():
            entered.set()
            self.release.wait(2)
        self.bridge.submit("slow", slow, mock.Mock())
        self.assertTrue(entered.wait(1))
        for index in range(8):
            self.assertTrue(self.bridge.submit(str(index), lambda: None, mock.Mock()))
        self.assertFalse(self.bridge.submit("slow", lambda: None, mock.Mock()))
        self.assertNotIn("slow", self.bridge.tokens)

    def test_worker_errors_are_reported_without_widget_calls(self):
        callback = mock.Mock()
        def fail():
            raise ValueError("Actionable error")
        self.bridge.submit("fail", fail, callback)
        wait_for(lambda: self.bridge.results.qsize() == 1)
        self.bridge.drain()
        callback.assert_called_once_with(None, "Actionable error")

    def test_library_reads_complete_while_a_long_job_is_blocked(self):
        entered = threading.Event()
        read = mock.Mock()
        def long_job():
            entered.set()
            self.release.wait(2)
        self.assertTrue(self.bridge.submit("report", long_job, mock.Mock(), urgent="long"))
        self.assertTrue(entered.wait(1))
        self.assertTrue(self.bridge.submit("library", lambda: ["synthetic"], read))
        wait_for(lambda: self.bridge.results.qsize() == 1)
        self.bridge.drain()
        read.assert_called_once_with(["synthetic"], None)
        self.assertFalse(self.release.is_set())

    def test_cancelled_queued_chat_is_never_admitted(self):
        entered = threading.Event()
        answer, callback = mock.Mock(), mock.Mock()
        def blocking_job():
            entered.set()
            self.release.wait(2)
        self.bridge.submit("report", blocking_job, mock.Mock(), urgent="long")
        self.assertTrue(entered.wait(1))
        self.bridge.submit("ask_this_meeting", answer, callback, urgent="long")
        self.bridge.invalidate("ask_this_meeting")
        self.release.set()
        wait_for(lambda: self.bridge.jobs["long"].empty())
        self.bridge.drain()
        answer.assert_not_called()
        callback.assert_not_called()


class LongJobCancellationTests(unittest.TestCase):
    setUp = BackgroundBridgeTests.setUp
    tearDown = BackgroundBridgeTests.tearDown

    def _block_long_lane(self):
        entered = threading.Event()
        def block():
            entered.set()
            self.release.wait(2)
        self.assertTrue(self.bridge.submit("blocker", block, mock.Mock(), "long"))
        self.assertTrue(entered.wait(1))

    def _report_view(self):
        from meeting_support import MeetingController

        controller = MeetingController.__new__(MeetingController)
        controller._lock = threading.Lock()
        controller._closed = controller._retention_active = controller._processing = False
        controller._final_copy_active = controller._playback_active = False
        controller._state, controller._play_thread = "idle", None
        controller._processing_kind = controller._processing_session = None
        controller._cancel, controller._file_done = threading.Event(), threading.Event()
        view = MeetingWindow.__new__(MeetingWindow)
        view.bridge, view.controller, view.closed = self.bridge, controller, False
        view.selected, view.summary_pending, view.summary_text = "one", ("one", 1), "old summary"
        view.summary_request_cancel = threading.Event()
        view.summary_status = Variable()
        view._sync_summary_controls, view._fail = mock.Mock(), mock.Mock()
        view._summary_hint = lambda _text: "synthetic hint"
        view._show_summary, view.refresh_reports, view._summary_completed = mock.Mock(), mock.Mock(), mock.Mock()
        view._action = mock.Mock(side_effect=lambda method, *, kinds, urgent:
                                 getattr(controller, method)(kinds=kinds))
        return view

    def test_cancel_queued_report_prevents_controller_admission_and_allows_a_new_report(self):
        self._block_long_lane()
        view = self._report_view()
        generated = mock.Mock(return_value={"report_id": "old"})
        def old():
            return view.controller._file_work(generated, "one", kind="report",
                                              cancel_event=view.summary_request_cancel)
        self.bridge.submit("generate_report", old,
                           lambda value, error: view._report_generated("one", value, error, request=1), "long")
        view.cancel_summary_generation()
        self.assertNotIn("generate_report", self.bridge.tokens)
        self.assertIsNone(view.summary_pending)
        view._sync_summary_controls.assert_called_once_with()
        view._action.assert_not_called()
        view.summary_pending = ("one", 2)
        view.summary_request_cancel = threading.Event()
        view._summary_cancel_requested = None
        view._report_generated("one", {"report_id": "stale"}, None, request=1)
        self.assertEqual(view.summary_pending, ("one", 2))
        view._show_summary.assert_not_called()
        fresh = mock.Mock(return_value={"report_id": "new"})
        self.bridge.submit("generate_report", lambda: view.controller._file_work(fresh, "one", kind="report"),
                           lambda value, error: view._report_generated("one", value, error, request=2), "long")
        self.release.set()
        wait_for(lambda: self.bridge.results.qsize() == 2)
        self.bridge.drain()
        generated.assert_not_called()
        fresh.assert_called_once_with()
        view._show_summary.assert_called_once_with({"report_id": "new"})
        self.assertIsNone(view.summary_pending)

    def test_running_report_receives_request_cancellation_and_settles_on_completion(self):
        view = self._report_view()
        entered, received = threading.Event(), threading.Event()
        event = view.summary_request_cancel
        def report():
            def generate():
                entered.set()
                if event.wait(1):
                    received.set()
                return None
            return view.controller._file_work(generate, "one", kind="report", cancel_event=event)
        self.bridge.submit("generate_report", report,
                           lambda value, error: view._report_generated("one", value, error, request=1), "long")
        self.assertTrue(entered.wait(1))
        view.cancel_summary_generation()
        self.assertTrue(received.wait(1))
        view._action.assert_not_called()
        self.assertEqual(view.summary_pending, ("one", 1))
        wait_for(lambda: self.bridge.results.qsize() == 1)
        self.bridge.drain()
        self.assertIsNone(view.summary_pending)
        view._show_summary.assert_not_called()

    def test_each_gui_cancel_survives_bridge_admission_before_controller_admission(self):
        for method, handler in (("generate_report", "cancel_summary_generation"),
                                ("ask_this_meeting", "cancel_chat_answer"),
                                ("cross_question", "cancel_cross_question")):
            with self.subTest(method=method):
                view = self._report_view()
                view.controller.voice = mock.Mock()
                view.controller._intelligence = mock.Mock()
                view.controller.cancel_processing = mock.Mock(wraps=view.controller.cancel_processing)
                entered, release, event = threading.Event(), threading.Event(), threading.Event()
                view.summary_request_cancel = event
                view.ask_request_cancel, view.ask_pending = event, ("one", 1)
                view.cross_request_cancel, view.cross_pending, view.cross_request = event, 1, 1
                view.selected = "other"
                view._chat_turn, view._sync_qa_controls = lambda *_: None, mock.Mock()
                view._sync_job_cancels, view.cross_status = mock.Mock(), Variable()
                original = view.controller._file_work
                def before_controller(*args, **kwargs):
                    entered.set()
                    if not release.wait(1):
                        raise AssertionError("Admission boundary timed out")
                    return original(*args, **kwargs)
                view.controller._file_work = before_controller
                def operation():
                    if method == "generate_report":
                        return view.controller.generate_report("one", "model", cancel_event=event)
                    if method == "ask_this_meeting":
                        return view.controller.ask_this_meeting("one", "question", "model", cancel_event=event)
                    return view.controller.ask_across_meetings("question", "model", cancel_event=event)
                callback = mock.Mock()
                self.assertTrue(self.bridge.submit(method, operation, callback, "long"))
                try:
                    self.assertTrue(entered.wait(1))
                    self.assertIn(method, self.bridge.admitted)
                    getattr(view, handler)()
                    self.assertTrue(event.is_set())
                    view.controller.cancel_processing.assert_not_called()
                    view._action.assert_not_called()
                finally:
                    release.set()
                wait_for(lambda: self.bridge.results.qsize() == 1)
                self.bridge.drain()
                view.controller.voice.reserve_for_meeting.assert_not_called()
                view.controller._intelligence.assert_not_called()
                if method == "generate_report":
                    self.assertIsNotNone(callback.call_args.args[1])
                else:
                    callback.assert_not_called()

    def _check_event_job(self, key, cancel_method, event_name, *, running=False):
        if not running:
            self._block_long_lane()
        view = MeetingWindow.__new__(MeetingWindow)
        view.bridge, view.controller = self.bridge, mock.Mock()
        view._sync_job_cancels, view._refresh_summary_models, view.search = mock.Mock(), mock.Mock(), mock.Mock()
        view._fail = mock.Mock()
        view.summary_progress_lock = threading.Lock()
        view.summary_progress = ("synthetic", 1, 10)
        view.summary_model_status, view.semantic_status, view.index_status = Variable(), Variable(), Variable()
        view.rebuild_button = mock.Mock()
        view.rebuild_progress = {"done": 0, "total": None, "state": "rebuilding"}
        event, entered, received = threading.Event(), threading.Event(), threading.Event()
        setattr(view, event_name, event)
        operation = mock.Mock(return_value={"state": "ready"})
        def work():
            entered.set()
            if running:
                if event.wait(1):
                    received.set()
            return operation()
        if key == "summary_model":
            callback = lambda value, error: view._summary_model_finished({"name": "synthetic"}, error)
        elif key in {"semantic_model", "semantic_rebuild"}:
            callback = view._semantic_operation_finished
        else:
            callback = view._rebuild_finished
        completed = mock.Mock(wraps=callback)
        self.bridge.submit(key, work, completed, "long")
        if running:
            self.assertTrue(entered.wait(1))
        getattr(view, cancel_method)()
        self.assertTrue(event.is_set())
        if running:
            self.assertTrue(received.wait(1))
            self.assertIs(getattr(view, event_name), event)
            wait_for(lambda: self.bridge.results.qsize() == 1)
            self.bridge.drain()
            completed.assert_called_once()
        else:
            self.assertNotIn(key, self.bridge.tokens)
            self.assertIsNone(getattr(view, event_name))
            view._sync_job_cancels.assert_called_once_with()
            replacement = mock.Mock()
            self.bridge.submit(key, lambda: "fresh", replacement, "long")
            self.release.set()
            wait_for(lambda: self.bridge.results.qsize() == 2)
            self.bridge.drain()
            operation.assert_not_called()
            completed.assert_not_called()
            replacement.assert_called_once_with("fresh", None)
            if key == "summary_model":
                view._refresh_summary_models.assert_called_once_with()
            elif key == "rebuild_index":
                view.rebuild_button.configure.assert_called_once_with(state="normal")
                self.assertEqual(view.rebuild_progress["state"], "cancelled")

    def test_cancel_queued_summary_download_prevents_file_or_network_work(self):
        self._check_event_job("summary_model", "cancel_summary_download", "summary_download_cancel")

    def test_cancel_queued_semantic_download_prevents_file_or_network_work(self):
        self._check_event_job("semantic_model", "cancel_semantic_operation", "semantic_cancel")

    def test_cancel_queued_semantic_rebuild_settles_without_admission(self):
        self._check_event_job("semantic_rebuild", "cancel_semantic_operation", "semantic_cancel")

    def test_cancel_queued_index_rebuild_restores_controls(self):
        self._check_event_job("rebuild_index", "cancel_rebuild_index", "rebuild_cancel")

    def test_running_summary_download_receives_its_event_and_keeps_completion(self):
        self._check_event_job("summary_model", "cancel_summary_download", "summary_download_cancel", running=True)

    def test_running_semantic_download_receives_its_event_and_keeps_completion(self):
        self._check_event_job("semantic_model", "cancel_semantic_operation", "semantic_cancel", running=True)

    def test_running_semantic_rebuild_receives_its_event_and_keeps_completion(self):
        self._check_event_job("semantic_rebuild", "cancel_semantic_operation", "semantic_cancel", running=True)

    def test_running_index_rebuild_receives_its_event_and_keeps_completion(self):
        self._check_event_job("rebuild_index", "cancel_rebuild_index", "rebuild_cancel", running=True)

    def test_completed_result_waiting_for_gui_is_not_mistaken_for_queued_work(self):
        completed = mock.Mock()
        self.bridge.submit("generate_report", lambda: "finished", completed, "long")
        wait_for(lambda: self.bridge.results.qsize() == 1)
        self.assertFalse(self.bridge.cancel_queued("generate_report"))
        self.bridge.drain()
        completed.assert_called_once_with("finished", None)


class ManagerLifecycleLogicTests(unittest.TestCase):
    def test_poll_cadence_uses_controller_playback_contract(self):
        for snapshot, expected in (
            ({"state": "idle"}, 2000), ({"state": "idle", "processing": True}, 1000),
            ({"state": "postprocessing", "processing": True}, 1000),
            ({"state": "recording"}, 200), ({"state": "paused"}, 200),
            ({"state": "idle", "previewing": True}, 200),
            ({"state": "idle", "playback": {"active": True}}, 200),
        ):
            with self.subTest(snapshot=snapshot):
                self.assertEqual(manager_poll_interval(snapshot), expected)

    def test_hide_stops_poll_without_closing_or_cancelling_controller(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.closed, view.visible, view.after_id = False, True, "pending"
        view.root, view.waveform, view.controller = mock.Mock(), mock.Mock(), mock.Mock()
        view._visible_destination = lambda: "library"
        view._poll = mock.Mock()
        view.set_visible(False)
        view.root.after_cancel.assert_called_once_with("pending")
        self.assertIsNone(view.after_id)
        self.assertFalse(view.closed)
        view.controller.cancel_processing.assert_not_called()
        view.set_visible(True)
        view._poll.assert_called_once_with()

    def test_capture_keeps_bounded_python_context_and_settles_pending_chats(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.ask_conversations = {str(index): [{"status": "pending", "saving": True}]
                                  for index in range(10)}
        view._visible_destination = lambda: None
        view.query, view.transcript_style = Variable("synthetic query"), Variable("Com horários")
        view.transcript_style_box = mock.Mock()
        view.transcript_style_box.current.return_value = 1
        view.selected, view.library_cursor, view.library_back_stack = "synthetic", "cursor", [None, "cursor"]
        view.dirty, view.title, view._saved_title = True, Variable("Later draft"), "Original title"
        view.detail_sections, view.sessions = mock.Mock(), mock.Mock()
        view.detail_sections.current = "ask"
        view.sessions.yview.return_value = (0.4, 0.8)
        state = view.capture_view_state()
        self.assertIsInstance(state, ManagerViewState)
        self.assertEqual(state.title_draft, ("synthetic", "Later draft", "Original title"))
        self.assertEqual((state.destination, state.query, state.selection, state.detail_section,
                          state.transcript_style, state.list_scroll),
                         ("voice", "synthetic query", "synthetic", "ask", "timestamped", 0.4))
        self.assertEqual(len(state.chats), 8)
        self.assertTrue(all(turns[0]["status"] == "error" and "saving" not in turns[0]
                            for turns in state.chats.values()))
        self.assertEqual(view.ask_conversations["9"][0]["status"], "pending")

    def test_recording_shortcuts_validate_conflicts_and_dispatch_only_valid_states(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.raw_settings = {}
        view.controller, view._action, view.pause_resume = mock.Mock(), mock.Mock(), mock.Mock()
        view.request_start, view._fail, view._here = mock.Mock(), mock.Mock(), lambda: "recording"
        with mock.patch("meeting_gui.validate_hotkey_conflicts") as validate:
            view.controller.snapshot.return_value = {"state": "idle"}
            self.assertEqual(view._record_shortcut(False), "break")
            view.request_start.assert_called_once_with("window")
            self.assertEqual(validate.call_args.args[0]["meeting_hotkey"], "ctrl+shift+r")
            view.controller.snapshot.return_value = {"state": "paused"}
            view._record_shortcut(False)
            view._action.assert_called_once_with("stop", urgent=True)
            view._record_shortcut(True)
            view.pause_resume.assert_called_once_with()
        with mock.patch("meeting_gui.validate_hotkey_conflicts", side_effect=ValueError("conflict")):
            view.request_start.reset_mock()
            view._record_shortcut(False)
            view.request_start.assert_not_called()
            view._fail.assert_called_once()

    def test_rebuild_restores_context_and_continues_unique_chat_identifiers(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.query, view.transcript_style = Variable(), Variable()
        view._cancel_library_search, view._sync_transcript_style = mock.Mock(), mock.Mock()
        view.detail_sections, view.notebook = mock.Mock(), mock.Mock()
        view.recording_tab, view.library_tab, view.settings_tab, view.voice_tab = "record", "library", "settings", "voice"
        view.load_session, view.refresh_library = mock.Mock(), mock.Mock()
        state = ManagerViewState(destination="library", query="synthetic", selection="one",
                                 detail_section="ask", transcript_style="timestamped", list_scroll=0.4,
                                 chats={"one": [{"id": 41, "status": "complete"}]})
        view.restore_view_state(state)
        self.assertEqual(view.query.get(), "synthetic")
        self.assertEqual(view.transcript_style.get(), "Com horários")
        self.assertEqual(view.ask_request, 41)
        view.detail_sections.select.assert_called_once_with("ask")
        view.notebook.select.assert_called_once_with("library")
        view.load_session.assert_called_once_with("one")
        self.assertIsNot(view.ask_conversations, state.chats)
        self.assertIs(view._restoring_view_state, state)


class MeetingWindowSmokeTests(unittest.TestCase):
    def test_inline_timestamp_reading_measure_and_typography_on_actual_text(self):
        view, notebook = self._library_view("1600x1000")
        notebook.select(view.library_tab)
        view._show_library_detail(True)
        view.detail_sections.select("transcript")
        view.selected, view.detail_ready, view.transcript_revision = "synthetic", True, "r1"
        preview = "00:00:02 Microfone: 😀 " + "readable words " * 120 + "\n00:01:03 Sistema: next"
        view.controller.get_transcript_preview.return_value = {"text": preview, "truncated": False}
        view.transcript_style.set("Com horários")
        view._transcript_style_changed()
        self._drain(view)
        document = view.transcript_document
        self.assertTrue(document.winfo_ismapped())
        self.assertFalse(view.timed_transcript.winfo_ismapped())
        self.assertEqual(document.get("1.0", "end-1c"), preview)
        self.assertEqual(tuple(map(str, document.tag_ranges("timestamp"))), ("1.0", "1.8", "2.0", "2.8"))
        body_line = ui_theme.tkfont.Font(root=self.root, font=document.cget("font")).metrics("linespace")
        self.assertAlmostEqual(int(document.cget("spacing2")), body_line / 2, delta=0.5)
        measure = document.winfo_width() - 2 * int(document.cget("padx"))
        self.assertLessEqual(measure, view.ui.px(680) + 1)
        view._action = mock.Mock()
        document.focus_force()
        document.mark_set("insert", "2.0")
        document.event_generate("<Return>")
        self.root.update()
        view._action.assert_called_once_with("seek_playback", "synthetic", "final", start=63.0, urgent=True)
        view._toggle_transcript_audio()
        self.root.update()
        self.assertTrue(view.timed_transcript.winfo_ismapped())
        self.assertEqual(document.get("1.0", "end-1c"), preview)

    def _assert_action_surface(self, button, ui, painted):
        if ui.system == "darwin":
            # Aqua owns its bezel. Compare with an actual native control while
            # retaining the surrounding written-action/state/geometry checks.
            native = tk.Button(button.master)
            try:
                self.assertEqual(button.cget("bg"), native.cget("bg"))
                self.assertNotIsInstance(button, ui_theme._ThemedButton)
            finally:
                native.destroy()
        else:
            self.assertEqual(button.cget("bg"), painted)

    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
            cls.root.withdraw()
        except tk.TclError as exc:
            raise unittest.SkipTest(f"Tk initialization unavailable: {exc}") from exc

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()
        cls.root = None
        # Collect closed window cycles on Tk's thread before worker tests.
        gc.collect()

    def test_manager_controls_are_left_aligned_labelled_and_stacked(self):
        controller = mock.Mock()
        controller.snapshot.return_value = {
            "state": "idle", "levels": {}, "elapsed": 0, "processing": False,
        }
        controller.devices.return_value = []
        controller.list_sessions.return_value = []
        controller.read_workspace.return_value = {
            "generation": 0, "collections": [], "series": [],
        }
        window = open_meeting_window(self.root, controller, lambda: {}, mock.Mock())
        view = window._meeting_view
        try:
            self.root.update()
            if hasattr(view, "voice_boost_check"):
                self.assertEqual(view.voice_boost_check.cget("anchor"), "w")
            self.assertFalse(hasattr(view, "auto_transcribe_check"))
            self.assertFalse(hasattr(view, "auto_summary_check"))
            # Side-packed siblings squeeze the full-width answer into a corner.
            answer_siblings = view.cross_answer.master.pack_slaves()
            self.assertEqual([w for w in answer_siblings if w.pack_info()["side"] == "left"], [])

            labels = {w.cget("text") for w in descendants(view.window) if isinstance(w, tk.Label)}
            for caption in ("Coleção/projeto", "Tag", "Pessoa", "Série",
                            "Coleções/projetos", "Tags", "Pessoas"):
                self.assertNotIn(caption, labels)
        finally:
            view.close()
            self.root.update()

    # Pixel fit depends on the platform's fonts and native control metrics;
    # the minimum-width layout is verified against Windows' Segoe UI metrics.
    # macOS uses its own wider minimum, and the Linux GUI is not supported.
    @unittest.skipUnless(sys.platform == "win32", "layout fit is verified on Windows metrics")
    def test_library_controls_fit_at_the_minimum_manager_width(self):
        controller = mock.Mock()
        controller.snapshot.return_value = {
            "state": "idle", "levels": {}, "elapsed": 0, "processing": False,
        }
        controller.devices.return_value = []
        controller.list_sessions.return_value = []
        controller.read_workspace.return_value = {
            "generation": 0, "collections": [], "series": [],
        }
        # Mirror the manager: its minimum size, notebook inset by 24px.
        _geometry, min_width, min_height = ui_theme.build_theme(
            "windows", system="windows").manager_window_size
        manager = tk.Toplevel(self.root)
        manager.geometry(f"{min_width}x{min_height}")
        notebook = ttk.Notebook(manager)
        notebook.pack(fill="both", expand=True, padx=24)
        view = add_meeting_tabs(
            self.root, manager, notebook, controller, lambda: {}, mock.Mock(),
        )
        try:
            notebook.select(view.library_tab)
            # The worst case for the page's own message slot: trouble with its
            # action and Detalhes, drawn above the list at the minimum size.
            view._fail("library", "Não foi possível carregar a biblioteca",
                       "A lista abaixo pode estar desatualizada. Tente novamente.",
                       error="OSError: index is locked", action=("Tentar novamente", lambda: None))
            self.root.update()
            bar = view.message_bars["library"]
            self.assertTrue(bar.winfo_ismapped())
            self.assertLessEqual(bar.winfo_rooty() + bar.winfo_height(),
                                 manager.winfo_rooty() + manager.winfo_height())
            self.assertLessEqual(bar.winfo_rootx() + bar.winfo_width(),
                                 manager.winfo_rootx() + manager.winfo_width())
            # Pack clips an overflowing row by shrinking its last widgets below
            # their requested width rather than pushing them off the edge.
            clipped = [
                widget.cget("text") or str(widget)
                for widget in descendants(view.library_tab)
                if isinstance(widget, (tk.Button, tk.Label)) and widget.winfo_ismapped()
                and widget.cget("width") == 0
                and widget.winfo_width() < widget.winfo_reqwidth()
            ]
            self.assertEqual(clipped, [])
        finally:
            view.close_without_prompt(destroy=False)
            manager.destroy()
            self.root.update()

    def _embedded_view(self, geometry="1120x820", **options):
        controller = mock.Mock()
        controller.snapshot.return_value = {
            "state": "idle", "levels": {}, "elapsed": 0, "processing": False,
        }
        controller.devices.return_value = []
        controller.list_sessions.return_value = []
        controller.list_sessions_page.return_value = {
            "items": [], "next_cursor": None, "cursor_reset": False, "index_state": "ready",
        }
        controller.search_library.return_value = []
        controller.read_workspace.return_value = {
            "generation": 0, "collections": [], "series": [],
        }
        manager = tk.Toplevel(self.root)
        manager.geometry(geometry)
        notebook = ttk.Notebook(manager)
        notebook.pack(fill="both", expand=True, padx=24)
        view = add_meeting_tabs(
            self.root, manager, notebook, controller, lambda: {}, mock.Mock(), **options,
        )
        self.addCleanup(self.root.update)
        self.addCleanup(manager.destroy)
        self.addCleanup(view.close_without_prompt, destroy=False)
        return view, notebook

    def _poll_once(self, view, snapshot):
        """Run one manager poll against ``snapshot`` without the repeating timer."""
        if view.after_id is not None:
            self.root.after_cancel(view.after_id)
            view.after_id = None
        view.controller.snapshot.return_value = snapshot
        view._poll()
        self.root.after_cancel(view.after_id)
        view.after_id = None
        self.root.update()

    def _ready_recorder(self):
        view, notebook = self._embedded_view()
        notebook.select(view.recording_tab)
        wait_for(lambda: (self.root.update(), view.settings_loaded)[1])
        # Deliver the startup reads (settings, privacy, library) before a test
        # adds its own rows or readiness.
        wait_for(lambda: (self.root.update(), view.bridge.drain(), not view.bridge.callbacks)[2])
        view.privacy_ready = True
        return view, notebook

    def test_held_start_is_disabled_named_and_never_reads_ready(self):
        view, _notebook = self._ready_recorder()
        free = {"state": "idle", "levels": {}, "elapsed": 0, "processing": False}
        self._poll_once(view, free)
        self.assertEqual(str(view.start_button.cget("state")), "normal")
        self.assertTrue(view.record_status.get().startswith("Pronto · "))
        self.assertFalse(view.start_blocked_row.winfo_ismapped())
        self.assertEqual(view.waveform.state, "idle")

        held = (
            {"processing": True, "processing_kind": "report", "processing_session": "s1"},
            {"state": "postprocessing", "processing": True, "processing_kind": "transcription",
             "processing_session": "s1"},
            {"processing": True, "previewing": True, "processing_kind": "preview"},
            {"final_copy": True},
            {"playback": {"active": True}},
            {"retention_active": True},
            {"state": "recording", "elapsed": 5},
            {"state": "unavailable"},
        )
        for change in held:
            self._poll_once(view, {**free, **change})
            self.assertEqual(str(view.start_button.cget("state")), "disabled", change)
            self.assertFalse(view.record_status.get().startswith("Pronto"), change)
            self.assertNotEqual(view.waveform.state, "idle", change)
            if change.get("state") in (None, "postprocessing"):
                # One state per view: the caption repeats the status lead.
                lead = view.record_status.get().split(" · ")[0]
                self.assertEqual(view.waveform.status_text, f"Estado: {lead}", change)
            if change.get("state") in (None, "postprocessing"):
                self.assertTrue(view.start_blocked_row.winfo_ismapped(), change)
                self.assertTrue(view.start_blocked_reason.get().startswith("Aguardando "), change)
            else:
                self.assertFalse(view.start_blocked_row.winfo_ismapped(), change)
        for name, value in (("settings_loaded", False), ("retention_ready", False),
                            ("privacy_save_inflight", True), ("privacy_ready", False)):
            original = getattr(view, name)
            setattr(view, name, value)
            try:
                self._poll_once(view, free)
                self.assertEqual(str(view.start_button.cget("state")), "disabled", name)
                self.assertTrue(view.record_status.get().startswith("Aguardando · "), name)
                self.assertTrue(view.start_blocked_row.winfo_ismapped(), name)
                self.assertTrue(view.start_blocked_reason.get(), name)
                self.assertFalse(view.interrupt_start_button.winfo_ismapped(), name)
            finally:
                setattr(view, name, original)
        self._poll_once(view, free)
        self.assertEqual(str(view.start_button.cget("state")), "normal")

    def test_processing_hold_offers_interrupt_then_records_once_the_job_stops(self):
        view, _notebook = self._ready_recorder()
        view._library_loaded([{"id": "s1", "title": "Revisão semanal", "status": "completed"}], None)
        busy = {"state": "idle", "levels": {}, "elapsed": 0, "processing": True,
                "processing_kind": "summary", "processing_session": "s1"}
        self._poll_once(view, busy)
        self.assertEqual(view.start_blocked_reason.get(), "Aguardando o resumo de “Revisão semanal”.")
        self.assertTrue(view.interrupt_start_button.winfo_ismapped())
        self.assertEqual(str(view.interrupt_start_button.cget("state")), "normal")
        self.assertEqual(view.waveform.state, "processing")
        self.assertTrue(view.record_status.get().startswith("Processando · "))
        self.assertEqual(view.waveform.status_text, "Estado: Processando")

        view.request_start = mock.Mock(return_value=True)
        with mock.patch("meeting_gui.messagebox.askokcancel", return_value=False):
            view.interrupt_and_record()
        self.assertIsNone(view.start_after_interrupt)
        with mock.patch("meeting_gui.messagebox.askokcancel", return_value=True) as confirm:
            view.interrupt_and_record()
        message = confirm.call_args.args[1]
        self.assertIn("Interromper o resumo de “Revisão semanal” e começar a gravar?", message)
        self.assertIn("O áudio original fica preservado", message)
        self.assertIn("na Biblioteca", message)
        wait_for(lambda: view.controller.cancel_processing.called)
        view.controller.cancel_processing.assert_called_once_with()

        self._poll_once(view, busy)
        self.assertEqual(view.start_blocked_reason.get(),
                         "Interrompendo o resumo; a gravação começa em seguida.")
        self.assertEqual(str(view.interrupt_start_button.cget("state")), "disabled")
        view.request_start.assert_not_called()
        self._poll_once(view, {**busy, "processing": False, "processing_kind": None,
                               "processing_session": None})
        view.request_start.assert_called_once_with("window")
        self.assertIsNone(view.start_after_interrupt)
        self.assertFalse(view.start_blocked_row.winfo_ismapped())

    def test_interrupt_confirmation_states_the_consequence_of_each_job_class(self):
        from meeting_support import JOB_LABELS, PIPELINE_JOBS
        view, _notebook = self._ready_recorder()
        view.request_start = mock.Mock(return_value=True)
        self.assertEqual(PIPELINE_JOBS, {"final_audio", "transcription", "summary", "capture_cleanup"})
        for kind in JOB_LABELS:
            view.controller.snapshot.return_value = {
                "state": "idle", "levels": {}, "elapsed": 0, "processing": True,
                "processing_kind": kind,
            }
            with mock.patch("meeting_gui.messagebox.askokcancel", return_value=False) as confirm:
                view.interrupt_and_record()
            message = confirm.call_args.args[1]
            self.assertIn("A gravação começa assim que", message, kind)
            if kind in PIPELINE_JOBS:
                self.assertIn("O áudio original fica preservado. Refaça essa etapa depois, "
                              "na Biblioteca.", message, kind)
            else:
                # A model import, chat answer, export or source test has no
                # recording audio to keep and is not redone in the Library.
                self.assertIn("Essa tarefa para antes de terminar. Execute-a de novo depois.",
                              message, kind)
                self.assertNotIn("áudio original", message, kind)
                self.assertNotIn("Biblioteca", message, kind)
            self.assertIsNone(view.start_after_interrupt, kind)
        view.controller.cancel_processing.assert_not_called()

    def test_refused_start_names_its_blocker(self):
        view, _notebook = self._ready_recorder()
        view.controller.snapshot.return_value = {"state": "postprocessing", "processing": True,
                                                 "processing_kind": "summary"}
        view._started(False, None, "hotkey")
        kind, title, body, _details = said(view, "recording")
        self.assertEqual((kind, title), ("problem", "Gravação não iniciada"))
        self.assertEqual(body, "Aguardando o resumo. Para gravar agora, use Interromper e gravar.")
        view.controller.snapshot.return_value = {"state": "idle", "playback": {"active": True}}
        view._started(False, None, "hotkey")
        self.assertEqual(said(view, "recording")[1:3],
                         ("Gravação não iniciada", "Aguardando a reprodução."))

    def test_cancel_processing_is_offered_only_for_this_recordings_running_job(self):
        view, _notebook = self._ready_recorder()
        button = view.cancel_processing_button
        step = view.processing_job_label
        view.selected = "s1"
        base = {"state": "idle", "levels": {}, "elapsed": 0}
        view.detail_ready = True
        view.final_available = True
        # Cancel exists only while a job of this recording (or of none) runs. The
        # Library page is hidden behind the recorder tab, so read the manager
        # geometry-manager membership rather than whether a widget is on screen.
        self._poll_once(view, {**base, "processing": False})
        self.assertFalse(button.winfo_manager())
        self.assertEqual(view.processing_job_status.get(), "")
        self.assertFalse(step.grid_info())
        self._poll_once(view, {**base, "state": "postprocessing", "processing": True,
                               "processing_kind": "transcription", "processing_session": "s1"})
        self.assertEqual((bool(button.winfo_manager()), str(button.cget("state")), button.cget("text")),
                         (True, "normal", "Cancelar"))
        self.assertEqual(view.processing_job_status.get(), "Em execução: a transcrição.")
        self.assertTrue(step.grid_info())
        # Another recording's job: no Cancel here, and the line says what holds the recovery controls.
        self._poll_once(view, {**base, "processing": True, "processing_kind": "export",
                               "processing_session": "s2"})
        self.assertFalse(button.winfo_manager())
        self.assertEqual(view.processing_job_status.get(), "Aguarde a exportação terminar.")
        self.assertEqual(str(view.transcribe_button.cget("state")), "disabled")
        self._poll_once(view, {**base, "processing": True, "processing_kind": "import",
                               "processing_session": None})
        self.assertEqual((bool(button.winfo_manager()), button.cget("text")), (True, "Cancelar"))
        self.assertEqual(view.processing_job_status.get(), "Em execução: a importação de áudio.")
        for state in ("starting", "recording", "paused", "stopping"):
            self._poll_once(view, {**base, "state": state, "processing": True,
                                   "processing_kind": "export", "processing_session": "s1"})
            self.assertFalse(button.winfo_manager(), state)
            self.assertEqual(view.processing_job_status.get(), "Finalize a gravação em andamento primeiro.",
                             state)

    # Same fonts-and-metrics caveat as the library minimum-width test above.
    @unittest.skipUnless(sys.platform == "win32", "layout fit is verified on Windows metrics")
    def test_processing_cancel_names_every_job_and_fits_the_minimum_size(self):
        import i18n
        from meeting_support import JOB_LABELS
        _geometry, min_width, min_height = ui_theme.build_theme(
            "windows", system="windows").manager_window_size
        for language in ("pt-BR", "en-US"):
            i18n.set_language(language)
            self.addCleanup(i18n.set_language, i18n.DEFAULT_LANGUAGE)
            controller = mock.Mock()
            controller.snapshot.return_value = {
                "state": "idle", "levels": {}, "elapsed": 0, "processing": False,
            }
            controller.devices.return_value = []
            controller.list_sessions.return_value = []
            controller.read_workspace.return_value = {
                "generation": 0, "collections": [], "series": [],
            }
            manager = tk.Toplevel(self.root)
            manager.geometry(f"{min_width}x{min_height}")
            # The manager's sidebar takes about 211px of the minimum width.
            tk.Frame(manager, width=211).pack(side="left", fill="y")
            notebook = ttk.Notebook(manager)
            notebook.pack(side="left", fill="both", expand=True)
            view = add_meeting_tabs(
                self.root, manager, notebook, controller, lambda: {}, mock.Mock(),
            )
            try:
                notebook.select(view.library_tab)
                wait_for(lambda: (self.root.update(), view.settings_loaded)[1])
                # Deliver the startup reads first: an empty library hides the detail.
                wait_for(lambda: (self.root.update(), view.bridge.drain(),
                                  not view.bridge.callbacks)[2])
                view._show_library_detail(True)
                view.selected = "s1"
                view.detail_sections.select("transcript")
                self.root.update()
                button, step = view.cancel_processing_button, view.processing_job_label
                for kind, label in JOB_LABELS.items():
                    self._poll_once(view, {"state": "idle", "levels": {}, "elapsed": 0,
                                           "processing": True, "processing_kind": kind,
                                           "processing_session": "s1"})
                    where = (language, kind)
                    self.assertEqual(str(button.cget("state")), "normal", where)
                    self.assertTrue(button.winfo_ismapped(), where)
                    self.assertGreaterEqual(button.winfo_width(), button.winfo_reqwidth(), where)
                    self.assertTrue(step.winfo_ismapped(), where)
                    self.assertIn(i18n.tr(label), view.processing_job_status.get(), where)
                    # A wrapping line grows instead of cutting the step name.
                    self.assertGreaterEqual(step.winfo_height(), step.winfo_reqheight(), where)
                    self.assertLessEqual(step.winfo_width(), view.transcript_processing_actions.winfo_width())
                    self.assertLessEqual(
                        view.transcript_processing_actions.winfo_rooty()
                        + view.transcript_processing_actions.winfo_height(),
                        manager.winfo_rooty() + manager.winfo_height(), where)
            finally:
                view.close_without_prompt(destroy=False)
                manager.destroy()
                self.root.update()

    def test_chat_cancel_stops_only_the_pending_answer_and_restores_the_question(self):
        view, notebook = self._embedded_view(geometry="920x700")
        notebook.select(view.library_tab)
        wait_for(lambda: (self.root.update(), view.settings_loaded)[1])
        view._show_library_detail(True)
        view.selected = "synthetic-session"
        view.detail_ready = True
        view.transcript_revision = "revision-1"
        view.summary_model.set("synthetic-model")
        view.summary_model_installed = {"synthetic-model": True}
        release = threading.Event()
        self.addCleanup(release.set)

        def late_answer(*_args, **_kwargs):
            release.wait(2)
            return {"answer": "Resposta tardia.", "citations": []}

        view.controller.ask_this_meeting.side_effect = late_answer
        view.meeting_chat.set_question("Quem decide o prazo?")
        view.ask_this_meeting()
        self.root.update()
        chat = view.meeting_chat
        self.assertEqual(chat.cancel_button.winfo_manager(), "pack")
        self.assertEqual(str(chat.cancel_button.cget("state")), "normal")
        cancel_event = view.ask_request_cancel
        chat.cancel_button.invoke()
        self.assertTrue(cancel_event.is_set())
        view.controller.cancel_processing.assert_not_called()
        self.assertIsNone(view.ask_pending)
        self.assertEqual(view.ask_conversations["synthetic-session"], [])
        self.assertEqual(chat.get_question(), "Quem decide o prazo?")
        self.assertEqual(view.ask_status.get(), "Resposta cancelada. A pergunta voltou para o campo de texto.")
        self.root.update()
        self.assertEqual(chat.cancel_button.winfo_manager(), "")
        release.set()
        wait_for(lambda: (self.root.update(), "ask_this_meeting" not in view.bridge.callbacks)[1])
        self.assertEqual(view.ask_conversations["synthetic-session"], [])

    def test_settings_show_one_section_at_a_time(self):
        view, notebook = self._embedded_view()
        notebook.select(view.settings_tab)
        self.root.update()
        sections = view.settings_sections
        self.assertEqual(sections.current, "general")
        self.assertEqual([key for key, frame in sections.frames.items() if frame.winfo_ismapped()],
                         ["general"])
        self.assertFalse(view.recording_defaults_parent.winfo_ismapped())
        sections.select("recording")
        self.root.update()
        self.assertTrue(view.recording_defaults_parent.winfo_ismapped())
        sections.select("privacy")
        self.root.update()
        self.assertFalse(view.recording_defaults_parent.winfo_ismapped())
        self.assertTrue(sections.frames["privacy"].winfo_ismapped())

    def test_export_progress_updates_real_tk_widgets_and_hides_on_cancel(self):
        view, notebook = self._embedded_view(geometry="920x700")
        notebook.select(view.recording_tab)
        snapshot = {"state": "postprocessing", "processing": True, "audio_export": {
            "session_id": "session", "stage": "encoding", "percent": 42,
            "label": "Criando áudio final · 42%",
        }}
        view.controller.snapshot.return_value = snapshot
        view.snapshot = snapshot
        view.selected = "session"
        view.detail_ready = True
        view.refresh_outputs = mock.Mock()
        view._sync_audio_export_progress()
        self.root.update()
        self.assertTrue(view.audio_export_progress.winfo_ismapped())
        self.assertLessEqual(view.audio_export_progress.winfo_rooty() + view.audio_export_progress.winfo_height(),
                             view.window.winfo_rooty() + view.window.winfo_height())
        # The export holds Start: the transport (a disabled Iniciar) stays under the bar.
        self.assertFalse(view.stop_button.winfo_ismapped())
        self.assertGreater(view.start_button.winfo_rooty(), view.audio_export_progress.winfo_rooty())
        self.assertEqual(float(view.audio_export_progress.cget("value")), 42)
        self.assertEqual(float(view.final_audio_progress.cget("value")), 42)
        self.assertEqual(view.final_audio_status.get(), "Criando áudio final · 42%")
        snapshot["audio_export"].update(stage="cancelled", percent=None,
                                       label="Exportação cancelada; os originais foram preservados.")
        snapshot["processing"] = False
        view._sync_audio_export_progress()
        self.root.update()
        self.assertFalse(view.audio_export_progress.winfo_ismapped())
        self.assertFalse(view.final_audio_progress.grid_info())
        self.assertIn("cancelada", view.final_audio_status.get())

    def test_recording_screen_keeps_setup_visible_and_opens_settings(self):
        view, notebook = self._embedded_view(geometry="920x700")
        notebook.select(view.recording_tab)
        self.root.update()
        self.assertIs(view.record_title_entry.master, view.recording_activity)
        self.assertNotIn(view.recording_defaults_parent, descendants(view.recording_tab))
        self.assertLessEqual(view.recording_activity.winfo_rootx() + view.recording_activity.winfo_width(),
                             view.window.winfo_rootx() + view.window.winfo_width())
        view.show_recording_settings()
        self.root.update()
        self.assertEqual(notebook.select(), str(view.settings_tab))
        self.assertEqual(view.settings_sections.current, "recording")
        self.assertTrue(view.recording_defaults_parent.winfo_ismapped())

    def test_transcript_exposes_mouse_and_keyboard_playback_navigation(self):
        view, notebook = self._embedded_view()
        notebook.select(view.library_tab)
        self.root.update()
        self.assertTrue(view.transcript.bind("<Double-1>"))
        self.assertTrue(view.transcript.bind("<Return>"))

    def test_settings_group_transcription_and_summary_models_under_modelos(self):
        view, notebook = self._embedded_view()
        notebook.select(view.settings_tab)
        sections = view.settings_sections
        self.assertEqual(list(sections.frames), ["general", "recording", "privacy", "models"])
        self.assertEqual(sections.buttons["models"].cget("text"), "Modelos")
        sections.select("models")
        self.root.update()
        models = view.model_sections
        self.assertEqual(list(models.frames), ["transcription", "summary", "search"])
        self.assertEqual([models.buttons[key].cget("text") for key in models.frames],
                         ["Transcrição", "Resumos", "Busca"])
        self.assertTrue(view.transcription_models_parent.winfo_ismapped())
        self.assertFalse(models.frames["summary"].winfo_ismapped())
        models.select("summary")
        self.root.update()
        self.assertTrue(models.frames["summary"].winfo_ismapped())
        self.assertFalse(view.transcription_models_parent.winfo_ismapped())

    def test_library_panels_open_on_demand_without_manual_filters(self):
        view, notebook = self._embedded_view()
        notebook.select(view.library_tab)
        self.root.update()
        self.assertFalse(view.library_panels["cross"].winfo_ismapped())
        self.assertNotIn("tools", view.library_panels)
        view.library_menu.invoke(0)
        self.root.update()
        self.assertTrue(view.library_panels["cross"].winfo_ismapped())
        view.library_menu.invoke(0)
        self.root.update()
        self.assertFalse(view.library_panels["cross"].winfo_ismapped())

    def test_library_live_search_runs_once_and_escape_clears_immediately(self):
        view, _notebook = self._library_view()
        view.search = mock.Mock(wraps=view.search)
        view.refresh_library = mock.Mock()
        for value in ("m", "me", "meeting"):
            view.query.set(value)
        wait_for(lambda: (self.root.update(), view.refresh_library.call_count)[1] == 1)
        self.assertEqual(view.search.call_count, 1)
        view.window.focus_force()
        view.search_entry.focus_set()
        self.root.update()
        view.search_entry.event_generate("<Escape>")
        self.root.update()
        self.assertEqual(view.query.get(), "")
        self.assertIsNone(view._library_search_after)
        self.assertEqual(view.refresh_library.call_count, 2)

    def test_semantic_controls_live_only_in_settings_and_toolbar_is_small(self):
        view, notebook = self._library_view()
        library_widgets = descendants(view.library_tab)
        self.assertNotIn(view.semantic_cancel_button, library_widgets)
        self.assertNotIn(view.rebuild_button, library_widgets)
        self.assertIn(view.semantic_cancel_button, descendants(view.search_models_page))
        buttons = [w for w in descendants(view.library_toolbar) if isinstance(w, tk.Button)]
        self.assertEqual({w.cget("text") for w in buttons}, {"Mais", "Importar áudio…"})
        self.assertTrue(view.search_entry.winfo_ismapped())
        notebook.select(view.settings_tab)
        view.settings_sections.select("models")
        view.model_sections.select("search")
        self.root.update()
        self.assertTrue(view.rebuild_button.winfo_ismapped())

    def test_full_text_hits_replace_the_no_results_state_and_are_in_detail(self):
        view, _notebook = self._library_view()
        view.query.set("decision")
        view._cancel_library_search()
        view._library_loaded(({"items": []}, [{"session_id": "one", "title": "Planning",
                                                "source_kind": "transcript", "snippet": "decision"}],
                              view.library_filter_generation), None)
        self.root.update()
        self.assertFalse(view.library_empty_panel.winfo_ismapped())
        self.assertTrue(view.search_results_frame.winfo_ismapped())
        self.assertEqual(view.search_results_frame.master, view.library_detail_pane)
        self.assertEqual(view.page_label.get(), "1 resultado")

    def test_library_columns_fit_at_minimum_and_date_is_required_when_wide(self):
        created = "2026-10-20T12:10:00Z"
        for geometry in ("1040x700", "1120x820", "1440x900"):
            with self.subTest(geometry=geometry):
                view, _notebook = self._library_view(geometry)
                item = {"id": "one", "title": "Planning", "status": "completed",
                        "created_at": created, "duration": 65}
                view._library_loaded([item], None)
                view.selected, view.detail_state_item = "one", item
                view._show_library_detail(True)
                view._render_detail_state()
                self.root.update()
                shown = tuple(view.sessions["displaycolumns"])
                self.assertEqual(shown[:2], ("status", "title"))
                self.assertGreaterEqual(int(view.sessions.column("title", "width")), 190)
                self.assertLessEqual(sum(int(view.sessions.column(key, "width")) for key in shown),
                                     view.sessions.winfo_width())
                self.assertIn(format_local_timestamp(created), view.detail_meta.get())
                self.assertTrue(view.detail_state_label.master.winfo_ismapped())
                if geometry == "1440x900":
                    self.assertIn("date", shown)
                    self.assertGreaterEqual(int(view.sessions.column("date", "width")),
                                            view._session_font.measure(format_local_timestamp(created)))

    def test_each_detail_section_keeps_one_primary_action(self):
        view, _notebook = self._library_view()
        view._show_library_detail(True)
        for text in ("", "Saved summary"):
            view._show_summary(text)
            for section in ("transcript", "summary", "ask", "files"):
                view.detail_sections.select(section)
                for setup in (False, True):
                    if section == "summary" and bool(view.summary_setup.winfo_manager()) != setup:
                        view._toggle_summary_setup()
                    self.root.update()
                    primary = [w for w in descendants(view.detail_frame)
                               if isinstance(w, tk.Button) and w.winfo_ismapped()
                               and getattr(w, "_variant", None) == "primary"]
                    self.assertLessEqual(len(primary), 1, (text, section, setup))

    def test_delete_confirmation_hides_details_and_requires_an_explicit_choice(self):
        view, _notebook = self._library_view()
        projection = retention_plan_projection({"session_id": "one", "eligible": True,
                                                "byte_estimate": 2000,
                                                "policy": {"purge_after_days": 7}})
        view.window.focus_force()
        self.root.update()
        observed = []
        def inspect_and_choose(accept):
            dialog = next(w for w in view.window.winfo_children() if isinstance(w, tk.Toplevel))
            buttons = {w.cget("text"): w for w in descendants(dialog) if isinstance(w, tk.Button)}
            details = next(w for w in descendants(dialog) if isinstance(w, tk.Text))
            observed.append((bool(details.winfo_manager()), view.window.focus_get() == buttons["Cancelar"]))
            buttons["Detalhes"].invoke()
            observed.append(bool(details.winfo_manager()))
            buttons["Mover para a lixeira" if accept else "Cancelar"].invoke()
        for accept in (False, True):
            self.root.after(50, lambda accept=accept: inspect_and_choose(accept))
            self.assertEqual(view._confirm_trash(projection, "Planning"), accept)
        self.assertEqual(observed, [(False, True), True, (False, True), True])

    def test_keyboard_rename_cancel_and_delete_use_the_loaded_selection(self):
        view, _notebook = self._library_view()
        view.selected, view.detail_ready = "one", True
        view.loading = True
        view.title.set("Saved title")
        view._saved_title = "Saved title"
        view.loading = False
        view._show_library_detail(True)
        view.window.focus_force()
        view.sessions.focus_set()
        self.root.update()
        view.window.event_generate("<F2>")
        self.root.update()
        self.assertTrue(view.title_entry.winfo_ismapped())
        self.assertFalse(view.title_heading.winfo_ismapped())
        view.title.set("Unsaved title")
        view.title_entry.event_generate("<Escape>")
        self.root.update()
        self.assertEqual(view.title.get(), "Saved title")
        self.assertFalse(view.dirty)
        self.assertTrue(view.title_heading.winfo_ismapped())
        view.delete_selected = mock.Mock()
        view.sessions.focus_set()
        self.root.update()
        view.sessions.event_generate("<Delete>")
        view.delete_selected.assert_called_once_with()

    def test_narrow_title_uses_full_width_and_rename_survives_resize_cancel_and_save(self):
        view, _notebook = self._library_view("1040x700")
        self.root.after_cancel(view.after_id)
        view.after_id = None
        title = "Quarterly product planning and delivery review with decisions, owners, and next steps"
        view.selected, view.detail_ready = "one", True
        view.loading = True
        view.title.set(title)
        view._saved_title = title
        view.loading = False
        view._show_library_detail(True)
        for geometry in ("1040x700", "1440x900", "1040x700"):
            view.window.geometry(geometry)
            self.root.update()
            header_width = view.title_heading.master.winfo_width()
            stacked = header_width < 320 + view.rename_button.winfo_reqwidth() + view.ui.space_sm
            row = 1 if stacked else 0
            self.assertEqual(int(view.rename_button.grid_info()["row"]), row)
            self.assertTrue(view.rename_button.winfo_ismapped())
            self.assertGreaterEqual(view.rename_button.winfo_height(), 32)
            if row == 1:
                self.assertGreaterEqual(view.title_heading.winfo_width(),
                                        view.title_heading.master.winfo_width() - 2)
                line_height = ui_theme.tkfont.Font(font=view.title_heading.cget("font")).metrics("linespace")
                self.assertLessEqual(view.title_heading.winfo_height(), line_height * 5 + 4)
            view._begin_title_edit()
            self.root.update()
            self.assertTrue(view.title_entry.winfo_ismapped())
            self.assertFalse(view.title_heading.winfo_ismapped())
            self.assertTrue(view.rename_button.winfo_ismapped())
            view.title.set("Unsaved change")
            view._end_title_edit(cancel=True)
            self.root.update()
            self.assertEqual(view.title.get(), title)
            self.assertTrue(view.title_heading.winfo_ismapped())
        view._begin_title_edit()
        view.title.set("Saved replacement")
        with mock.patch.object(view, "_submit", return_value=True) as submit, \
                mock.patch.object(view, "refresh_library"):
            view._end_title_edit()
            submit.call_args.args[2](True, None)
        self.root.update()
        self.assertEqual(view._saved_title, "Saved replacement")
        self.assertFalse(view.title_entry.winfo_ismapped())
        self.assertTrue(view.title_heading.winfo_ismapped())
        self.assertTrue(view.rename_button.winfo_ismapped())

    def test_full_text_return_loads_a_trashable_detail_without_a_main_list_row(self):
        view, _notebook = self._library_view()
        self.root.after_cancel(view.after_id)
        view.after_id = None
        view.controller.retention_plan.return_value = {
            "session_id": "two", "eligible": True, "byte_estimate": 2000,
            "policy": {"purge_after_days": 7},
        }
        result = {"session_id": "two", "title": "Search-only recording", "source_kind": "session"}
        view._library_loaded(({"items": []}, [result], view.library_filter_generation), None)
        view.search_results.selection_set("0")
        view.window.focus_force()
        view.search_results.focus_set()
        self.root.update()
        with mock.patch.object(view, "_submit", return_value=True) as submit:
            view.search_results.event_generate("<Return>")
            self.root.update()
            self.assertEqual(submit.call_args.args[0], "search_result")
            submit.call_args.args[2](result, None)
            self.assertEqual(submit.call_args.args[0], "detail")
            submit.call_args.args[2](({"id": "two", "title": result["title"],
                                      "status": "completed", "duration": 60}, []), None)
            self.root.update()
            self.assertEqual(view.sessions.selection(), ())
            self.assertEqual(view.selected, "two")
            view.detail_sections.select("files")
            self.root.update()
            self.assertEqual(str(view.delete_button.cget("state")), "normal")
            view.delete_button.invoke()
            self.assertEqual(submit.call_args.args[0], "retention_plan")
            self.assertEqual(view._retention_pending[1], "two")

    def _trash_event_view(self):
        view, _notebook = self._library_view()
        self.root.after_cancel(view.after_id)
        view.after_id = None
        view.selected = "one"
        view._library_loaded([
            {"id": name, "title": name, "status": "completed", "duration": 60}
            for name in ("one", "two")
        ], None)
        view._show_library_detail(True)
        view.detail_ready = True
        view.detail_state_item = {"id": "one", "status": "completed"}
        view.loading = True
        view.title.set("Saved title")
        view._saved_title = "Saved title"
        view.loading, view.dirty = False, False
        view.delete_button.configure(state="normal")
        view.window.focus_force()
        view.sessions.focus_set()
        self.root.update()
        return view

    def test_context_mouse_event_cancel_and_pending_save_never_target_the_old_row(self):
        view = self._trash_event_view()
        for answer in (None, True):
            with self.subTest(answer=answer), \
                    mock.patch("meeting_gui.messagebox.askyesnocancel", return_value=answer), \
                    mock.patch.object(view, "_submit", return_value=True) as submit, \
                    mock.patch.object(view.session_menu, "tk_popup") as popup:
                view.sessions.selection_set("one")
                self.root.update()
                view.title.set("Unsaved title")
                _x, y, _width, height = view.sessions.bbox("two")
                view.sessions.event_generate("<Button-3>", x=20, y=y + height // 2)
                self.root.update()
                popup.assert_not_called()
                self.assertEqual(view.selected, "one")
                if answer:
                    self.assertIsNotNone(view._title_save_pending)
                    view.sessions.event_generate("<Delete>")
                    self.root.update()
                    self.assertEqual([call.args[0] for call in submit.call_args_list], ["save_title"])
                else:
                    self.assertEqual(view.sessions.selection(), ("one",))
                    submit.assert_not_called()

    def test_delete_events_release_each_unsuccessful_retention_lifecycle(self):
        view = self._trash_event_view()
        plan = {"session_id": "one", "eligible": True, "policy": {"purge_after_days": 7}}
        view.controller.retention_plan.return_value = plan
        for outcome in ("preview_refused", "preview_error", "blocked", "no_plan", "cancel",
                        "apply_refused", "apply_error", "apply_false"):
            with self.subTest(outcome=outcome), \
                    mock.patch.object(view, "_submit", return_value=outcome != "preview_refused") as submit, \
                    mock.patch.object(view, "_confirm_trash", return_value=outcome != "cancel"), \
                    mock.patch("meeting_gui.messagebox.showwarning"):
                view.sessions.event_generate("<Delete>")
                self.root.update()
                self.assertEqual(submit.call_args.args[0], "retention_plan")
                if outcome != "preview_refused":
                    if outcome == "apply_refused":
                        submit.return_value = False
                    preview_plan = False if outcome == "no_plan" else {
                        **plan, "eligible": outcome != "blocked"}
                    submit.call_args.args[2](preview_plan, RuntimeError("synthetic preview failure")
                                            if outcome == "preview_error" else None)
                    if outcome in {"apply_error", "apply_false"}:
                        self.assertEqual(submit.call_args.args[0], "retention_apply")
                        submit.call_args.args[2](outcome != "apply_false",
                                                RuntimeError("synthetic apply failure")
                                                if outcome == "apply_error" else None)
                self.assertIsNone(view._retention_pending)
                self.assertEqual(str(view.delete_button.cget("state")), "normal")
                self.assertFalse(view.undo_receipt.winfo_ismapped())

    def test_delete_key_context_and_risk_row_share_one_inflight_operation_and_undo(self):
        view = self._trash_event_view()
        plan = {"session_id": "one", "eligible": True, "policy": {"purge_after_days": 7}}
        view.controller.retention_plan.return_value = plan
        with mock.patch.object(view, "_submit", return_value=True) as submit, \
                mock.patch.object(view, "refresh_library") as refresh, \
                mock.patch.object(view, "_confirm_trash", return_value=True), \
                mock.patch.object(view.session_menu, "tk_popup"):
            view.sessions.event_generate("<Delete>")
            self.root.update()
            preview = submit.call_args.args[2]
            request = view.retention_request
            for phase in ("preview", "apply"):
                view.sessions.event_generate("<Delete>")
                view.sessions.event_generate("<Shift-F10>")
                self.root.update()
                self.assertEqual(str(view.session_menu.entrycget(0, "state")), "disabled")
                # Even a command from a menu opened before admission uses the gate.
                view.session_menu.entryconfigure(0, state="normal")
                view.session_menu.invoke(0)
                view.delete_button.invoke()
                self.assertEqual(view.retention_request, request)
                self.assertEqual(submit.call_count, 1 if phase == "preview" else 2)
                if phase == "preview":
                    preview(plan, None)
            applied = submit.call_args.args[2]
            applied(True, None)
            self.root.update()
            self.assertIsNone(view._retention_pending)
            refresh.assert_called_once_with()
            self.assertTrue(view.undo_receipt.winfo_ismapped())
            self.assertEqual(view._undo_session, "one")
            view.selected = "two"
            view.undo_button.invoke()
            self.assertEqual(submit.call_args.args[0], "library_undo")
            submit.call_args.args[1]()
            view.controller.restore_session.assert_called_once_with("one")

    def test_library_explains_empty_list_and_unselected_detail(self):
        view, notebook = self._embedded_view()
        notebook.select(view.library_tab)
        wait_for(lambda: (self.root.update(), view.library_empty.get())[1])
        self.root.update()
        self.assertTrue(view.library_empty_label.winfo_ismapped())
        self.assertIn("Nenhuma gravação ainda", view.library_empty.get())
        self.assertFalse(view.detail_placeholder.winfo_ismapped())
        self.assertFalse(view.detail_frame.winfo_ismapped())
        self.assertEqual(len(view.library_panes.panes()), 1)
        self.assertFalse(view.library_pages.winfo_ismapped())
        view._show_library_detail(True)
        self.root.update()
        self.assertTrue(view.detail_frame.winfo_ismapped())
        self.assertFalse(view.detail_placeholder.winfo_ismapped())
        self.assertEqual(view.detail_sections.current, "transcript")
        self.assertTrue(view.transcript_document.winfo_ismapped())
        # The player sits above the transcript instead of on its own tab.
        self.assertNotIn("audio", view.detail_sections.frames)
        self.assertTrue(view.play_button.winfo_ismapped())
        self.assertLess(view.playback_seek.winfo_rooty(), view.transcript_document.winfo_rooty())
        self.assertNotIn("notes", view.detail_sections.frames)
        self.assertFalse(hasattr(view, "notes"))
        self.assertFalse(hasattr(view, "bookmark_picker"))
        self.assertFalse(hasattr(view, "transcript_tools_toggle"))
        self.assertNotIn("speaker", view.transcript["columns"])
        self.assertEqual(tuple(view.transcript["columns"]), ("time", "text"))
        self.assertFalse(view.report_frame.winfo_ismapped())

    def test_library_replay_button_dispatches_saved_mix(self):
        view, notebook = self._embedded_view()
        notebook.select(view.library_tab)
        view._show_library_detail(True)
        view.selected = "synthetic-session"
        view.detail_ready = True
        view.playback_duration = 30.0
        view.playback_position.set(5.0)
        view._set_audio_capabilities({}, True)
        self.root.update()
        self.assertEqual(view.audio_source.get(), "Áudio final")
        self.assertEqual(str(view.play_button.cget("state")), "normal")
        view.play_button.invoke()
        wait_for(lambda: (self.root.update(), view.controller.seek_playback.called)[1])
        view.controller.seek_playback.assert_called_once_with(
            "synthetic-session", "final", start=5.0,
        )

    def test_playback_overview_displays_duration_for_capture_with_diagnostics(self):
        view, notebook = self._embedded_view()
        notebook.select(view.library_tab)
        wait_for(lambda: (self.root.update(), view.settings_loaded)[1])
        view.selected = "synthetic-session"
        metadata = {"status": "partial", "duration": 41, "final_available": True,
                    "tracks": {}, "summary": "", "error": "native_discontinuity",
                    "processing_details": ""}
        view._detail_loaded("synthetic-session", (metadata, []), None)
        self.root.update()
        self.assertEqual(view.audio_overview.get(), "00:00:41")
        self.assertEqual(metadata["status"], "partial")
        # The capture's diagnostics are written on Biblioteca for this recording,
        # with the cause behind Detalhes.
        kind, title, _body, details = said(view)
        self.assertEqual((kind, title), ("problem", "A captura teve problemas"))
        self.assertIn("native_discontinuity", details)
        self.assertEqual(view.messages.current("library").session, "synthetic-session")

    def test_processing_completion_preserves_another_selected_recordings_details(self):
        view, _notebook = self._embedded_view()
        wait_for(lambda: (self.root.update(), view.settings_loaded)[1])
        self.root.after_cancel(view.after_id)
        view.after_id = None
        view.selected = "new-recording"
        view.processing_target = "old-recording"
        view.previous_processing = False
        view._fail("library", "Selected recording trouble", error="Selected recording details",
                   session="new-recording")
        view.controller.snapshot.return_value["error"] = "Old recording capture diagnostic"
        view._poll()
        self.assertIsNone(view.processing_target)
        # Another recording's processing diagnostics never replace this recording's message.
        kind, title, _body, details = said(view)
        self.assertEqual((kind, title), ("problem", "Selected recording trouble"))
        self.assertIn("Selected recording details", details)
        self.assertNotIn("Old recording", details)

    def test_chat_send_button_keeps_conversation_and_composer_visible(self):
        callback_errors = []
        previous_reporter = self.root.report_callback_exception
        self.root.report_callback_exception = lambda *args: callback_errors.append(args)
        self.addCleanup(setattr, self.root, "report_callback_exception", previous_reporter)
        self.addCleanup(lambda: self.assertEqual(callback_errors, []))
        view, notebook = self._embedded_view(geometry="920x700")
        notebook.select(view.library_tab)
        wait_for(lambda: (self.root.update(), view.settings_loaded)[1])
        view._show_library_detail(True)
        view.selected = "synthetic-session"
        view.detail_ready = True
        view.transcript_revision = "revision-1"
        view.summary_model.set("synthetic-model")
        view.summary_model_installed = {"synthetic-model": True}
        view.controller.ask_this_meeting.return_value = {
            "answer": "A revisão será na sexta-feira.", "citations": ["microphone:0:1"],
            "uncertainty": "low", "revision": "revision-1",
        }
        view.detail_sections.select("ask")
        view._sync_qa_controls()
        self.root.update()
        view.meeting_chat.set_question("Qual foi a decisão?")
        view.meeting_chat.send_button.invoke()
        cancel_event = view.ask_request_cancel
        self.assertIsInstance(cancel_event, threading.Event)
        self.assertFalse(cancel_event.is_set())
        wait_for(lambda: (self.root.update(), view.ask_pending is None)[1])
        turns = view.ask_conversations[view.selected]
        self.assertEqual(turns[0]["status"], "complete")
        self.assertEqual(turns[0]["answer"], "A revisão será na sexta-feira.")
        view.controller.ask_this_meeting.assert_called_once_with(
            "synthetic-session", "Qual foi a decisão?", "synthetic-model",
            revision="revision-1", history=[], cancel_event=cancel_event,
        )
        self.assertEqual(view.detail_sections.buttons["ask"].cget("text"), "Chat")
        composer = view.meeting_chat.composer
        self.assertTrue(composer.winfo_ismapped())
        self.assertLessEqual(composer.winfo_rooty() + composer.winfo_height(),
                             view.detail_canvas.winfo_rooty() + view.detail_canvas.winfo_height())
        self.assertLessEqual(view.meeting_chat.send_button.winfo_rootx()
                             + view.meeting_chat.send_button.winfo_width(),
                             view.detail_canvas.winfo_rootx() + view.detail_canvas.winfo_width())

    def test_summary_templates_generate_in_primary_view_with_optional_focus(self):
        callback_errors = []
        previous_reporter = self.root.report_callback_exception
        self.root.report_callback_exception = lambda *args: callback_errors.append(args)
        self.addCleanup(setattr, self.root, "report_callback_exception", previous_reporter)
        self.addCleanup(lambda: self.assertEqual(callback_errors, []))
        view, notebook = self._embedded_view(geometry="920x700")
        notebook.select(view.library_tab)
        # Finish the initial empty-library response before installing a synthetic
        # selection; a late response would hide the detail pane during assertions.
        wait_for(lambda: (self.root.update(), view.settings_loaded and view.library_empty.get())[1])
        view._show_library_detail(True)
        view.selected = "synthetic-session"
        view.detail_ready = True
        view.transcript_revision = "revision-1"
        view.summary_model.set("synthetic-model")
        view.summary_model_installed = {"synthetic-model": True}
        self.assertEqual([item["id"] for item in view.report_profiles[:5]],
                         ["meeting_notes", "interview", "one_on_one", "sales", "customer_feedback"])
        self.assertEqual(view._selected_report_profile()["id"], "meeting_notes")
        hints = set()
        for label, profile in view.report_profile_by_label.items():
            if profile["id"] in {"meeting_notes", "interview", "one_on_one", "sales", "customer_feedback"}:
                view.report_profile_choice.set(label)
                view._report_profile_changed()
                hints.add(view.summary_format_hint.get())
        self.assertEqual(len(hints), 5)
        label = next(label for label, item in view.report_profile_by_label.items()
                     if item["id"] == "customer_feedback")
        view.report_profile_choice.set(label)
        view._report_profile_changed()
        view.detail_sections.select("summary")
        view.summary_setup_toggle.invoke()
        view.summary_focus_toggle.invoke()
        view.summary_focus.insert("1.0", "Priorize o acompanhamento.")
        generated = {"summary": "Conversa revisada.", "feedback": [{"text": "A busca é útil."}],
                     "action_items": [{"text": "Revisar filtros", "owner": None, "deadline": None}]}
        view.controller.generate_report.return_value = dict(generated, report_id="saved-1")
        view.controller.list_reports.return_value = [{"id": "saved-1", "kind": "report",
                                                      "profile_id": "customer_feedback"}]
        view.controller.get_report.return_value = {"id": "saved-1", "kind": "report",
                                                   "profile_id": "customer_feedback", "generated": generated}
        view._sync_summary_controls()
        self.root.update()
        self.assertFalse(view.report_frame.winfo_ismapped())
        for widget in (view.report_profile_box, view.regenerate_summary_button, view.summary_focus):
            self.assertTrue(widget.winfo_ismapped())
            self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(),
                                 view.detail_canvas.winfo_rootx() + view.detail_canvas.winfo_width())
        view.regenerate_summary_button.invoke()
        wait_for(lambda: (self.root.update(), view.summary_pending is None
                         and view.selected_report is not None)[1])
        self.assertEqual(view.controller.generate_report.call_args.kwargs["profile"]["id"], "customer_feedback")
        self.assertEqual(view.controller.generate_report.call_args.kwargs["focus"], "Priorize o acompanhamento.")
        self.assertIn("A busca é útil.", view.summary_text)
        self.assertIn("Revisar filtros", view.summary_text)
        self.assertNotIn("segment_ids", view.summary_text)
        self.assertFalse(view.report_frame.winfo_ismapped())
        self.assertTrue(view.copy_summary_button.winfo_ismapped())
        self.assertLessEqual(view.copy_summary_button.winfo_rooty() + view.copy_summary_button.winfo_height(),
                             view.detail_canvas.winfo_rooty() + view.detail_canvas.winfo_height())

    def test_every_index_state_has_a_portuguese_label(self):
        self.assertLessEqual(INDEX_STATES, set(INDEX_STATE_LABELS))

    def test_library_detail_pane_uses_scrollable_canvas_for_appended_controls(self):
        controller = mock.Mock()
        controller.snapshot.return_value = {
            "state": "idle", "levels": {}, "elapsed": 0, "processing": False,
        }
        controller.devices.return_value = []
        controller.list_sessions.return_value = []
        controller.read_workspace.return_value = {
            "generation": 0, "collections": [], "series": [],
        }
        window = open_meeting_window(self.root, controller, lambda: {}, mock.Mock())
        view = window._meeting_view
        try:
            self.root.update()
            self.assertIsInstance(view.detail_canvas, tk.Canvas)
            self.assertIs(view.detail_content.master, view.detail_canvas)
            self.assertTrue(view.detail_canvas.cget("yscrollcommand"))
            self.assertIsNotNone(view.detail_canvas.bbox("all"))
        finally:
            view.close()
            self.root.update()

    def test_meeting_tabs_attach_to_an_existing_manager_notebook(self):
        controller = mock.Mock()
        controller.snapshot.return_value = {
            "state": "idle", "levels": {}, "elapsed": 0, "processing": False,
        }
        controller.devices.return_value = []
        controller.list_sessions.return_value = []
        manager = tk.Toplevel(self.root)
        notebook = ttk.Notebook(manager)
        notebook.pack(fill="both", expand=True)
        view = add_meeting_tabs(
            self.root, manager, notebook, controller, lambda: {}, mock.Mock(),
        )
        try:
            self.root.update()
            titles = [notebook.tab(tab_id, "text") for tab_id in notebook.tabs()]
            self.assertTrue(view.embedded)
            self.assertEqual(
                tuple(widget.cget("text") for widget in view.appearance_box.winfo_children()),
                tuple(APPEARANCE_LABELS[key] for key in ("dark", "light", "system")),
            )
            self.assertIs(view.window, manager)
            self.assertIs(view.notebook, notebook)
            self.assertEqual(
                titles,
                ["Gravação", "Biblioteca", "Configurações"],
            )
            settings_widgets = descendants(view.settings_tab)
            settings_radios = [
                widget for widget in settings_widgets
                if isinstance(widget, tk.Radiobutton)
            ]
            settings_buttons = [
                str(widget.cget("text")) for widget in settings_widgets
                if isinstance(widget, tk.Button)
            ]
            self.assertEqual(len(settings_radios), 3)
            self.assertNotIn("Salvar modelo padrão", settings_buttons)
            self.assertIn("Cancelar download", settings_buttons)
            recording_buttons = [
                str(widget.cget("text")) for widget in descendants(view.recording_tab)
                if isinstance(widget, tk.Button)
            ]
            self.assertNotIn("Importar modelo local…", recording_buttons)
            library_buttons = [
                str(widget.cget("text")) for widget in descendants(view.library_tab)
                if isinstance(widget, tk.Button)
            ]
            self.assertIn("Importar áudio…", library_buttons)
            self.assertNotIn("Importar WAV…", library_buttons)
        finally:
            view.close_without_prompt(destroy=False)
            manager.destroy()
            self.root.update()

    def test_settings_mousewheel_scrolls_when_pointer_is_over_content(self):
        controller = mock.Mock()
        controller.snapshot.return_value = {
            "state": "idle", "levels": {}, "elapsed": 0, "processing": False,
        }
        controller.devices.return_value = []
        controller.list_sessions.return_value = []
        manager = tk.Toplevel(self.root)
        # Exercise a mapped settings section with a real vertical overflow.
        manager.geometry("1120x420")
        notebook = ttk.Notebook(manager)
        notebook.pack(fill="both", expand=True)
        view = add_meeting_tabs(
            self.root, manager, notebook, controller, lambda: {}, mock.Mock(),
        )
        try:
            notebook.select(view.settings_tab)
            view.settings_sections.select("recording")
            self.root.update()
            view.settings_canvas.yview_moveto(0)
            self.root.update()
            target = next(
                widget for widget in descendants(view.settings_content)
                if isinstance(widget, tk.Label) and widget.cget("text") == "Modelo de transcrição local"
            )
            self.assertTrue(target.winfo_ismapped())
            region = tuple(map(float, view.settings_canvas.cget("scrollregion").split()))
            self.assertGreater(region[3] - region[1], view.settings_canvas.winfo_height())
            self.assertLess(view.settings_canvas.yview()[1], 1)
            before = view.settings_canvas.yview()[0]

            if sys.platform.startswith(("win", "darwin")):
                target.event_generate("<MouseWheel>", delta=-120)
            else:
                target.event_generate("<Button-5>")
            self.root.update()

            self.assertGreater(view.settings_canvas.yview()[0], before)
        finally:
            view.close_without_prompt(destroy=False)
            manager.destroy()
            self.root.update()


    def _library_view(self, geometry="1120x820", **options):
        view, notebook = self._embedded_view(geometry, **options)
        notebook.select(view.library_tab)
        wait_for(lambda: (self.root.update(), view.settings_loaded)[1])
        wait_for(lambda: (self.root.update(), view.bridge.drain(), not view.bridge.callbacks)[2])
        return view, notebook

    def _drain(self, view):
        wait_for(lambda: (self.root.update(), view.bridge.drain(), not view.bridge.callbacks)[2])
        self.root.update()

    def test_library_rows_lead_with_a_written_toned_state_date_and_duration(self):
        view, _notebook = self._library_view()
        created = "2026-10-20T12:10:00Z"
        long_title = "Planejamento trimestral de operações e lançamentos " * 6
        view._library_loaded([
            {"id": "s1", "title": "Falha na captura", "status": "failed", "created_at": created,
             "duration": 2465},
            {"id": "s2", "title": "Queda de energia", "status": "interrupted", "created_at": created,
             "duration": 60},
            {"id": "s3", "title": "Fonte perdida", "status": "partial", "created_at": created,
             "duration": 61},
            {"id": "s4", "title": long_title, "status": "completed", "created_at": created,
             "duration": 3725},
            {"id": "s5", "title": "", "status": "cancelled", "created_at": None, "duration": None},
        ], None)
        self.root.update()
        tree = view.sessions
        self.assertEqual(tuple(tree["columns"]), ("status", "title", "date", "duration"))
        self.assertEqual(str(tree.column("duration", "anchor")), "e")
        # Native metrics determine which columns fit. A date disappears only
        # to preserve the readable title and remains in the selected header.
        shown = tuple(tree["displaycolumns"])
        required = 190 + view._session_column_widths["status"] + view._session_column_widths["date"] + 4
        self.assertEqual("date" in shown, tree.winfo_width() >= required)
        self.assertEqual(shown[:2], ("status", "title"))
        self.assertLessEqual(sum(int(tree.column(column, "width")) for column in shown),
                             tree.winfo_width())

        def row(iid):
            return tuple(tree.item(iid, "values")), tuple(tree.item(iid, "tags"))

        date = format_local_timestamp(created)
        view.selected = "s1"
        view.detail_state_item = view.session_items["s1"]
        view._render_detail_state()
        self.assertIn(date, view.detail_meta.get())
        self.assertEqual(row("s1"), (("Falhou", "Falha na captura", date, "00:41:05"), ("danger",)))
        self.assertEqual(row("s2"), (("Interrompida", "Queda de energia", date, "00:01:00"), ("warning",)))
        self.assertEqual(row("s3")[0][0], "Parcial")
        self.assertEqual(row("s3")[1], ("warning",))
        self.assertEqual(row("s5"), (("Cancelada", "Sem título", "Sem data", "00:00:00"), ("neutral",)))
        values, tags = row("s4")
        self.assertEqual((values[0], tags), ("Concluída", ()))
        # The cut title still fits its column, and the full title keeps naming the job.
        self.assertTrue(values[1].endswith("…"))
        self.assertLessEqual(view._session_font.measure(values[1]), int(tree.column("title", "width")))
        self.assertEqual(view.session_items["s4"]["title"], long_title)
        self.assertEqual(str(tree.tag_configure("danger", "foreground")), view.ui.danger)
        self.assertEqual(str(tree.tag_configure("warning", "foreground")), view.ui.warning)
        self.assertEqual(view.page_label.get(), "5 gravações")

        # A job working on a recording shows on its row until it ends; an
        # export does not change what the recording is.
        busy = {"state": "idle", "levels": {}, "elapsed": 0, "processing": True,
                "processing_kind": "transcription", "processing_session": "s4"}
        self._poll_once(view, busy)
        self.assertEqual(row("s4")[0][0], "Processando")
        self.assertEqual(row("s4")[1], ("info",))
        self._poll_once(view, {**busy, "processing_kind": "export"})
        self.assertEqual(row("s4")[0][0], "Concluída")
        self._poll_once(view, {**busy, "processing": False, "processing_session": None})
        self.assertEqual(row("s4"), (values, ()))

        view._library_loaded([{"id": "s1", "title": "Falha na captura", "status": "failed"}], None)
        self.assertEqual(view.page_label.get(), "1 gravação")

    def test_a_date_the_platform_cannot_convert_leaves_the_other_rows_and_the_header_intact(self):
        view, _notebook = self._library_view()
        real_parse = meeting_text._parse_timestamp

        def parse(value):
            if value != "unconvertible":
                return real_parse(value)
            moment = mock.Mock()
            moment.astimezone.side_effect = OSError(22, "Invalid argument")
            return moment

        items = [{"id": f"s{i}", "title": f"Registro {i}", "status": "completed",
                  "created_at": "unconvertible" if i == 2 else "2026-10-20T12:10:00Z", "duration": 60}
                 for i in (1, 2, 3)]
        with mock.patch("meeting_text._parse_timestamp", side_effect=parse):
            view._library_loaded(items, None)
            view.detail_state_item, view.selected = items[1], "s2"
            view._render_detail_state()
        self.assertEqual(view.sessions.get_children(), ("s1", "s2", "s3"))
        self.assertEqual(view.sessions.item("s2", "values")[2], "Sem data")
        self.assertEqual(view.sessions.item("s3", "values")[1], "Registro 3")
        self.assertEqual(view.page_label.get(), "3 gravações")
        self.assertEqual(view.detail_meta.get(), " · Sem data · 00:01:00")

    def test_failed_transcription_shows_its_reason_and_retry_and_its_own_empty_copy(self):
        view, _notebook = self._library_view()
        created = "2026-10-20T12:10:00Z"
        items = [{"id": "s1", "title": "Revisão", "status": "completed",
                  "created_at": created, "duration": 125}]
        # A job's end reloads the list, which must still hold the selected recording.
        view.controller.list_sessions_page.return_value = {
            "items": items, "next_cursor": None, "cursor_reset": False, "index_state": "ready"}
        view._library_loaded(items, None)
        view._show_library_detail(True)
        view.selected = "s1"
        view.transcribe = mock.Mock()
        view.prepare_final_audio = mock.Mock()
        view.controller.get_transcript_preview.return_value = {"text": "", "truncated": False}
        view.controller.list_reports.return_value = []
        metadata = {
            "status": "completed", "duration": 125, "created_at": created, "final_available": True,
            "tracks": {}, "summary": "", "processing_details": "transcription: runtime failed",
            "processing_issue": {"stage": "transcription", "status": "failed",
                                 "error": r"runtime failed at C:\Users\someone\model.bin"},
            "transcription_status": "failed",
        }
        view._detail_loaded("s1", (metadata, []), None)
        self._drain(view)
        slot = view.processing_issue_slot
        self.assertTrue(slot.winfo_ismapped())
        bar = slot.winfo_children()[0]
        self.assertEqual(bar.cget("bg"), view.ui.danger_bg)
        labels = [w.cget("text") for w in descendants(bar) if isinstance(w, tk.Label)]
        self.assertIn("A transcrição falhou", labels)
        buttons = {w.cget("text"): w for w in descendants(bar) if isinstance(w, tk.Button)}
        buttons["Transcrever novamente"].invoke()
        view.transcribe.assert_called_once_with()
        details = [w.get("1.0", "end-1c") for w in descendants(bar) if isinstance(w, tk.Text)]
        self.assertEqual(len(details), 1)
        self.assertIn("runtime failed", details[0])
        self.assertNotIn("C:\\Users", details[0])
        # Copiar goes through the app's clipboard backend and reports its outcome.
        copy = buttons["Copiar"]
        with mock.patch("meeting_gui.Clipboard.set_content", return_value=False) as write:
            copy.invoke()
            self._drain(view)
        write.assert_called_once_with(details[0])
        self.assertEqual(copy.cget("text"), "Copiar")
        hints = [w.cget("text") for w in descendants(bar) if isinstance(w, tk.Label)]
        self.assertIn("Selecione o texto e pressione Ctrl+C", hints)
        with mock.patch("meeting_gui.Clipboard.set_content", return_value=True) as write:
            copy.invoke()
            self._drain(view)
        write.assert_called_once_with(details[0])
        self.assertEqual(copy.cget("text"), "Copiado")
        hints = [w.cget("text") for w in descendants(bar) if isinstance(w, tk.Label)]
        self.assertNotIn("Selecione o texto e pressione Ctrl+C", hints)
        self.assertEqual((view.detail_state.get(), view.detail_meta.get()),
                         ("Concluída", f" · {format_local_timestamp(created)} · 00:02:05"))
        failed_copy = view.transcript_document_status.get()
        self.assertEqual(failed_copy, "A transcrição falhou e esta gravação não tem texto.")

        metadata.update(processing_issue=None, transcription_status="pending", processing_details="")
        view._detail_loaded("s1", (metadata, []), None)
        self._drain(view)
        self.assertFalse(slot.winfo_ismapped())
        pending_copy = view.transcript_document_status.get()
        self.assertEqual(pending_copy, "A transcrição aparecerá após o processamento com um modelo local.")
        self.assertNotEqual(failed_copy, pending_copy)

        # Without the final audio the prerequisite is the action; a cancel is a warning.
        metadata.update(final_available=False, transcription_status="cancelled",
                        processing_issue={"stage": "transcription", "status": "cancelled", "error": ""})
        view._detail_loaded("s1", (metadata, []), None)
        self._drain(view)
        bar = slot.winfo_children()[0]
        self.assertEqual(bar.cget("bg"), view.ui.warning_bg)
        labels = [w.cget("text") for w in descendants(bar) if isinstance(w, tk.Label)]
        self.assertIn("O áudio foi preservado. Transcrever de novo requer o áudio final.", labels)
        buttons = {w.cget("text"): w for w in descendants(bar) if isinstance(w, tk.Button)}
        self.assertEqual(set(buttons), {"Preparar áudio final"})
        buttons["Preparar áudio final"].invoke()
        view.prepare_final_audio.assert_called_once_with()

        # While a job works on the recording the bar steps aside and the header says so.
        busy = {"state": "idle", "levels": {}, "elapsed": 0, "processing": True,
                "processing_kind": "transcription", "processing_session": "s1"}
        self._poll_once(view, busy)
        self.assertFalse(slot.winfo_ismapped())
        self.assertEqual(view.detail_state.get(), "Processando")
        # The caption under the format row stops reporting the cancelled run.
        working_copy = "Processando esta gravação. O resultado aparecerá aqui ao terminar."
        self.assertEqual(view.transcript_document_status.get(), working_copy)
        # When it ends the failure loaded before the job is not shown again: the
        # bar and the caption wait for the store's current answer, here a new reason.
        view.controller.get_session.return_value = {"processing": {
            "transcription": {"status": "failed", "error": "novo erro depois de transcrever de novo"}}}
        self._poll_once(view, {**busy, "processing": False, "processing_session": None})
        self.assertFalse(slot.winfo_ismapped())
        self.assertEqual(view.detail_state.get(), "Concluída")
        self.assertEqual(view.transcript_document_status.get(), working_copy)
        self._drain(view)
        self.assertTrue(slot.winfo_ismapped())
        self.assertEqual(view.transcript_document_status.get(), failed_copy)
        bar = slot.winfo_children()[0]
        self.assertEqual(bar.cget("bg"), view.ui.danger_bg)
        details = [w.get("1.0", "end-1c") for w in descendants(bar) if isinstance(w, tk.Text)]
        self.assertIn("novo erro depois de transcrever de novo", details[0])

        # A job that fixed it leaves no bar and no failure caption at all.
        self._poll_once(view, busy)
        self.assertEqual(view.transcript_document_status.get(), working_copy)
        view.controller.get_session.return_value = {"processing": {"transcription": {"status": "completed"}}}
        self._poll_once(view, {**busy, "processing": False, "processing_session": None})
        self._drain(view)
        self.assertFalse(slot.winfo_ismapped())
        self.assertEqual(slot.winfo_children(), [])
        self.assertEqual(view.transcript_document_status.get(), pending_copy)

        # A recording that has text keeps its format caption through a job.
        view.transcript_document.configure(state="normal")
        view.transcript_document.insert("1.0", "Texto sintético da reunião.")
        view.transcript_document.configure(state="disabled")
        view.transcript_document_status.set("Formato aplicado. Copiar e Exportar usam esta apresentação.")
        self._poll_once(view, busy)
        self.assertEqual(view.transcript_document_status.get(),
                         "Formato aplicado. Copiar e Exportar usam esta apresentação.")
        self._poll_once(view, {**busy, "processing": False, "processing_session": None})
        self._drain(view)
        self.assertEqual(view.transcript_document_status.get(),
                         "Formato aplicado. Copiar e Exportar usam esta apresentação.")

    def test_trash_keeps_every_action_visible_at_its_default_size(self):
        view, _notebook = self._library_view()
        now = datetime.now(timezone.utc)
        moved = (now - timedelta(days=2)).isoformat()
        view.controller.list_trash.return_value = [
            {"session_id": "20261020-091500-4f1c2a9e8b", "deleted_at": moved,
             "purge_after": (now + timedelta(days=23, hours=1)).isoformat(), "byte_estimate": 48_213_377},
            {"session_id": "20260901-101500-9a7b6c5d4e", "deleted_at": (now - timedelta(days=40)).isoformat(),
             "purge_after": (now - timedelta(days=10)).isoformat(), "byte_estimate": 1_288_490_188},
        ]
        dialog = view.show_trash()
        wait_for(lambda: (self.root.update(), view.bridge.drain(), view.trash_entries)[2])
        self.root.update()
        self.assertEqual((dialog.winfo_width(), dialog.winfo_height()), (720, 420))
        refresh = next(w for w in descendants(dialog)
                       if isinstance(w, tk.Button) and w.cget("text") == "Atualizar")
        bottom = dialog.winfo_rooty() + dialog.winfo_height()
        for button in (view.trash_restore_button, view.trash_purge_button,
                       view.trash_empty_button, refresh):
            self.assertEqual(button.winfo_ismapped(), 1, button.cget("text"))
            self.assertLessEqual(button.winfo_rooty() + button.winfo_height(), bottom)
            self.assertGreaterEqual(button.winfo_width(), button.winfo_reqwidth())
        tree = view.trash_tree
        self.assertEqual(tuple(tree.item("0", "values")),
                         ("…1c2a9e8b", format_local_timestamp(moved), "em 23 d", "48,2 MB"))
        self.assertEqual(tuple(tree.item("1", "values"))[2:], ("Expirada", "1,3 GB"))
        self.assertEqual(str(tree.column("size", "anchor")), "e")
        self.assertEqual(tree.selection(), ("0",))
        self._assert_action_surface(view.trash_restore_button, view.ui, view.ui.accent)
        self.assertEqual(str(view.trash_purge_button.cget("state")), "normal")
        self.assertEqual(str(view.trash_empty_button.cget("state")), "normal")
        self.assertEqual(view.trash_status.get(), "2 gravações na lixeira · 1 expirada")
        for sequence in ("<Return>", "<KP_Enter>", "<Delete>"):
            self.assertTrue(tree.bind(sequence), sequence)
        self.assertTrue(dialog.bind("<Escape>"))

        tree.selection_set(())
        self.root.update()
        self.assertEqual(str(view.trash_restore_button.cget("state")), "disabled")
        self.assertNotEqual(view.trash_restore_button.cget("bg"), view.ui.accent)
        self.assertEqual(str(view.trash_purge_button.cget("state")), "disabled")

        # A restore's receipt leads the refreshed count.
        tree.selection_set("0")
        view.controller.restore_session.return_value = True
        view.controller.list_trash.return_value = view.controller.list_trash.return_value[1:]
        view.restore_selected_trash()
        wait_for(lambda: (self.root.update(), view.bridge.drain(), len(view.trash_entries) == 1)[2])
        self._drain(view)
        view.controller.restore_session.assert_called_once_with("20261020-091500-4f1c2a9e8b")
        self.assertEqual(view.trash_status.get(),
                         "Gravação …1c2a9e8b restaurada na Biblioteca · 1 gravação na lixeira · 1 expirada")
        view._close_trash_dialog()
        self.assertFalse(dialog.winfo_exists())

    def test_trash_says_why_emptying_is_disabled_when_nothing_expired(self):
        view, _notebook = self._library_view()
        now = datetime.now(timezone.utc)
        view.controller.list_trash.return_value = [
            {"session_id": "20261020-091500-4f1c2a9e8b", "deleted_at": (now - timedelta(days=2)).isoformat(),
             "purge_after": (now + timedelta(days=23)).isoformat(), "byte_estimate": 48_213_377},
        ]
        view.show_trash()
        wait_for(lambda: (self.root.update(), view.bridge.drain(), view.trash_entries)[2])
        self.assertEqual(str(view.trash_empty_button.cget("state")), "disabled")
        self.assertEqual(view.trash_status.get(), "1 gravação na lixeira · nenhuma expirada")
        view._close_trash_dialog()

    def test_the_trash_and_the_typed_confirmation_take_the_app_icon(self):
        # Tk's default icon never reaches a transient dialog, so each one asks the host.
        hook = mock.Mock()
        view, _notebook = self._library_view(set_window_icon=hook)
        self.assertIs(view.set_window_icon, hook)
        view.controller.list_trash.return_value = []
        dialog = view.show_trash()
        self._drain(view)
        hook.assert_called_once_with(dialog)
        hook.reset_mock()
        confirmation = view._typed_confirmation("Excluir?", "Fatos")
        self.addCleanup(lambda: confirmation.dialog.winfo_exists() and confirmation.dialog.destroy())
        hook.assert_called_once_with(confirmation.dialog)
        view._close_trash_dialog()

    def test_dialogs_open_without_an_icon_hook(self):
        view, _notebook = self._library_view()
        self.assertIsNone(view.set_window_icon)
        view.controller.list_trash.return_value = []
        self.assertTrue(view.show_trash().winfo_exists())
        self._drain(view)
        view._close_trash_dialog()

    def test_permanent_delete_needs_the_typed_word(self):
        view, _notebook = self._library_view()
        view.controller.list_trash.return_value = []
        view.show_trash()
        self._drain(view)
        confirmation = view._typed_confirmation("Excluir “…1c2a9e8b” definitivamente?", "Movida para a lixeira")
        self.addCleanup(lambda: confirmation.dialog.winfo_exists() and confirmation.dialog.destroy())
        self.root.update()
        button = confirmation.confirm_button
        self.assertEqual(str(button.cget("state")), "disabled")
        button.invoke()
        self.assertFalse(confirmation.confirmed)
        confirmation.typed.set("excluir isso")
        self.assertEqual(str(button.cget("state")), "disabled")
        confirmation.typed.set(" excluir ")
        self.assertEqual(str(button.cget("state")), "normal")
        self._assert_action_surface(button, view.ui, view.ui.danger_bg)
        confirmation.typed.set("EXCLUIR")
        button.invoke()
        self.assertTrue(confirmation.confirmed)
        self.assertFalse(confirmation.dialog.winfo_exists())

        # The blocking form returns the person's answer once the dialog closes.
        build = view._typed_confirmation
        for word, cancel, expected in (("EXCLUIR", False, True), ("EXCLUIR", True, False),
                                       ("EXCLUI", False, False)):
            def answering(question, facts, word=word, cancel=cancel):
                pending = build(question, facts)

                def answer():
                    pending.typed.set(word)
                    if cancel:
                        next(w for w in descendants(pending.dialog)
                             if isinstance(w, tk.Button) and w.cget("text") == "Cancelar").invoke()
                    else:
                        pending.confirm_button.invoke()
                        if pending.dialog.winfo_exists():
                            pending.dialog.destroy()

                self.root.after(50, answer)
                return pending

            view._typed_confirmation = answering
            self.assertEqual(view._confirm_permanent_delete("Excluir?", "Fatos"), expected, (word, cancel))
        view._close_trash_dialog()

    # -- Feedback: page messages, recording trouble, the transport swap ------------------------

    def _recording_snapshot(self, state="recording", system="receiving", system_age=0.0, **extra):
        return {"state": state, "levels": {}, "elapsed": 2754, "processing": False,
                "capture_health": {
                    "microphone": {"state": "receiving", "reason": "", "last_packet_age": 0.0},
                    "system": {"state": system, "reason": "endpoint removed",
                               "last_packet_age": system_age}},
                **extra}

    def test_a_page_message_is_drawn_on_its_page_and_leaves_with_the_user(self):
        view, notebook = self._ready_recorder()
        notebook.select(view.library_tab)
        self.root.update()
        view._fail("library", "Falhou aqui", "Corpo.", error="boom")
        self.root.update()
        bar = view.message_bars["library"]
        self.assertTrue(bar.winfo_ismapped())
        texts = [w.cget("text") for w in descendants(bar) if isinstance(w, tk.Label)]
        self.assertIn("Falhou aqui", texts)
        self.assertTrue(any(text.startswith("Erro às ") for text in texts), texts)
        for other in ("recording", "settings"):
            self.assertEqual(view.message_bars[other].winfo_manager(), "", other)
        # Navigating away clears it for good: it does not wait on another page.
        notebook.select(view.recording_tab)
        self.root.update()
        self.assertIsNone(view.messages.current("library"))
        self.assertEqual(bar.winfo_manager(), "")
        notebook.select(view.library_tab)
        self.root.update()
        self.assertEqual(bar.winfo_manager(), "")

    def test_a_message_written_while_the_user_is_elsewhere_waits_on_its_page(self):
        view, notebook = self._ready_recorder()
        notebook.select(view.settings_tab)
        self.root.update()
        view._fail("library", "Falhou em segundo plano", "Corpo.")
        self.root.update()
        self.assertEqual(view.message_bars["library"].winfo_manager(), "pack")
        notebook.select(view.library_tab)
        self.root.update()
        self.assertEqual(view.messages.current("library").title, "Falhou em segundo plano")
        self.assertTrue(view.message_bars["library"].winfo_ismapped())

    def test_a_library_load_error_is_inline_with_a_retry_and_ends_when_it_loads(self):
        view, notebook = self._ready_recorder()
        notebook.select(view.library_tab)
        view.refresh_library = mock.Mock()
        view._library_loaded(None, "OSError: index is locked")
        self.root.update()
        kind, title, body, details = said(view)
        self.assertEqual((kind, title), ("problem", "Não foi possível carregar a biblioteca"))
        self.assertIn("pode estar desatualizada", body)
        self.assertIn("index is locked", details)
        bar = view.message_bars["library"]
        self.assertTrue(bar.winfo_ismapped())
        retry = next(w for w in descendants(bar) if isinstance(w, tk.Button)
                     and w.cget("text") == "Tentar novamente")
        retry.invoke()
        view.refresh_library.assert_called_once_with()
        view._library_loaded([{"id": "s1", "title": "Revisão", "status": "completed"}], None)
        self.root.update()
        self.assertIsNone(view.messages.current("library"))
        self.assertEqual(bar.winfo_manager(), "")

    def test_a_failed_start_is_written_on_the_recording_page_and_a_new_start_clears_it(self):
        view, notebook = self._ready_recorder()
        notebook.select(view.recording_tab)
        view._started(None, "WASAPI 0x88890004", "window")
        self.root.update()
        kind, title, body, details = said(view, "recording")
        self.assertEqual((kind, title), ("problem", "Não foi possível iniciar a gravação"))
        self.assertIn("WASAPI", details)
        self.assertNotIn("aba", body)
        self.assertTrue(view.message_bars["recording"].winfo_ismapped())
        view._submit = mock.Mock(return_value=True)
        settings = view._current_settings()
        view._submit_start(settings, "")
        self.assertIsNone(view.messages.current("recording"))
        self.assertEqual(view._recording_sources, settings.sources)

    def test_a_polling_failure_is_written_once_and_ends_when_polling_recovers(self):
        view, _notebook = self._ready_recorder()
        self.root.after_cancel(view.after_id)
        view.controller.snapshot.side_effect = RuntimeError("synthetic poll failure")
        view._poll()
        self.root.after_cancel(view.after_id)
        first = view.messages.current("recording")
        self.assertEqual((first.kind, first.key), ("problem", "poll"))
        self.assertIn("synthetic poll failure", first.details)
        view._poll()
        self.root.after_cancel(view.after_id)
        self.assertIs(view.messages.current("recording"), first)
        view.controller.snapshot.side_effect = None
        view._poll()
        self.root.after_cancel(view.after_id)
        view.after_id = None
        self.assertIsNone(view.messages.current("recording"))

    def test_transport_shows_only_the_controls_that_can_act(self):
        view, _notebook = self._ready_recorder()
        free = {"state": "idle", "levels": {}, "elapsed": 0, "processing": False}

        def shown():
            return {name: (button.winfo_ismapped(), str(button.cget("state")))
                    for name, button in (("start", view.start_button), ("pause", view.pause_button),
                                         ("stop", view.stop_button))}

        self._poll_once(view, free)
        self.assertEqual(shown(), {"start": (True, "normal"), "pause": (False, "disabled"),
                                   "stop": (False, "disabled")})
        self._assert_action_surface(view.start_button, view.ui, view.ui.accent)
        start_x = view.start_button.winfo_rootx()
        # While recording, Parar e salvar takes the primary's place, with Pausar beside it.
        self._poll_once(view, self._recording_snapshot())
        self.assertEqual(shown(), {"start": (False, "disabled"), "pause": (True, "normal"),
                                   "stop": (True, "normal")})
        self.assertEqual(view.stop_button.cget("text"), "Parar e salvar")
        self._assert_action_surface(view.stop_button, view.ui, view.ui.accent)
        self.assertEqual(view.stop_button.winfo_rootx(), start_x)
        self.assertEqual(view.pause_button.cget("text"), "Pausar")
        self.assertNotEqual(view.pause_button.cget("bg"), view.ui.accent)
        self._poll_once(view, self._recording_snapshot("paused"))
        self.assertEqual(view.pause_button.cget("text"), "Retomar")
        self.assertTrue(view.pause_button.winfo_ismapped())
        # Starting and stopping have nothing to pause.
        for state in ("starting", "stopping"):
            self._poll_once(view, self._recording_snapshot(state))
            self.assertEqual((view.stop_button.winfo_ismapped(), view.pause_button.winfo_ismapped(),
                              view.start_button.winfo_ismapped()), (True, False, False), state)
        # Work holding Start keeps the transport on Iniciar, disabled and not copper.
        self._poll_once(view, {**free, "state": "postprocessing", "processing": True,
                               "processing_kind": "summary", "processing_session": "s1"})
        self.assertEqual(shown()["start"], (True, "disabled"))
        self.assertFalse(shown()["stop"][0])
        self.assertNotEqual(view.start_button.cget("bg"), view.ui.accent)
        self._poll_once(view, free)
        self.assertEqual(shown()["start"], (True, "normal"))
        self._assert_action_surface(view.start_button, view.ui, view.ui.accent)

    def test_the_shortcut_is_named_beside_the_transport_when_one_is_set(self):
        import dataclasses
        view, _notebook = self._ready_recorder()
        self.assertEqual(view.shortcut_hint.cget("text"), "")
        view.settings = dataclasses.replace(view.settings, hotkey="ctrl+alt+r")
        view._sync_shortcut_hint()
        self.root.update()
        self.assertEqual(view.shortcut_hint.cget("text"), "Atalho: Ctrl+Alt+R")

        def beside(button):
            hint = view.shortcut_hint
            # On the button's own row, to its right: the shortcut costs no height.
            self.assertGreaterEqual(hint.winfo_rootx(), button.winfo_rootx() + button.winfo_width())
            self.assertLess(abs((hint.winfo_rooty() + hint.winfo_height() // 2)
                                - (button.winfo_rooty() + button.winfo_height() // 2)),
                            button.winfo_height() // 2)

        beside(view.start_button)
        self._poll_once(view, self._recording_snapshot())
        beside(view.pause_button)
        view.settings = dataclasses.replace(view.settings, hotkey="")
        view._sync_shortcut_hint()
        self.assertEqual(view.shortcut_hint.cget("text"), "")

    def test_the_status_chip_and_the_line_under_it_follow_the_recorder(self):
        view, _notebook = self._ready_recorder()
        dot = next(w for w in view.record_chip.winfo_children() if isinstance(w, tk.Canvas))

        def tone():
            return dot.itemcget(dot.find_all()[0], "fill")

        free = {"state": "idle", "levels": {}, "elapsed": 0, "processing": False}
        self._poll_once(view, free)
        self.assertEqual((view.record_chip.label.cget("text"), tone()),
                         ("Pronto · 00:00:00", view.ui.success))
        self._poll_once(view, self._recording_snapshot())
        self.assertEqual((view.record_chip.label.cget("text"), tone()),
                         ("Gravando · 00:45:54", view.ui.accent_mark))
        self.assertEqual(view.preview_status.get(), RECORDING_HINT)
        self._poll_once(view, self._recording_snapshot(system="disconnected", system_age=9.0))
        self.assertEqual(view.record_chip.label.cget("text"),
                         "Gravando · 00:45:54 · Áudio do sistema: sem áudio")
        self.assertEqual(tone(), view.ui.warning)
        self._poll_once(view, free)
        self.assertEqual(view.preview_status.get(), SOURCE_TEST_HINT)

    def test_a_lost_source_raises_a_warning_that_names_it_and_counts_the_silence(self):
        view, _notebook = self._ready_recorder()
        view._recording_sources = "both"
        trouble = view.recording_trouble
        view._sync_recording_trouble(self._recording_snapshot(), now=100.0)
        self.assertIsNone(trouble.kind)
        self.assertEqual({track: label.cget("text") for track, label in view.source_captions.items()},
                         {"microphone": "Recebendo sinal", "system": "Recebendo sinal"})
        # A moment of silence is a device switch mending itself: the caption says it, no bar yet.
        view._sync_recording_trouble(self._recording_snapshot(system="disconnected"), now=101.0)
        self.assertIsNone(trouble.kind)
        self.assertEqual(view.source_captions["system"].cget("text"), "Sem áudio")
        # Past the grace period the warning names the source and how long it has been silent.
        view._sync_recording_trouble(self._recording_snapshot(system="disconnected"), now=165.0)
        self.root.update()
        self.assertEqual(trouble.kind, "problem")
        texts = [w.cget("text") for w in descendants(trouble) if isinstance(w, (tk.Label, tk.Button))]
        self.assertIn("Sem áudio do sistema há 1 min 04 s", texts)
        self.assertIn("Trocar dispositivo", texts)
        self.assertEqual(view.source_captions["system"].cget("text"), "Sem áudio · há 1 min 04 s")
        self.assertEqual(view.source_captions["microphone"].cget("text"), "Recebendo sinal")
        self.assertEqual(str(view.source_captions["system"].cget("fg")), view.ui.warning)
        bar = trouble.winfo_children()[0]
        # The silence keeps counting in the same bar instead of rebuilding it.
        view._sync_recording_trouble(self._recording_snapshot(system="disconnected"), now=166.0)
        self.assertIs(trouble.winfo_children()[0], bar)
        self.assertEqual(bar.title_label.cget("text"), "Sem áudio do sistema há 1 min 05 s")
        # The action goes to the selector and says when a new choice applies.
        next(w for w in descendants(trouble) if isinstance(w, tk.Button)
             and w.cget("text") == "Trocar dispositivo").invoke()
        self.assertIn("a partir da próxima gravação", view.preview_status.get())
        # The source returns: the bar and the warning caption go.
        view._sync_recording_trouble(self._recording_snapshot(), now=170.0)
        self.root.update()
        self.assertIsNone(trouble.kind)
        self.assertEqual(trouble.winfo_manager(), "")
        self.assertEqual(view.source_captions["system"].cget("text"), "Recebendo sinal")

    def test_both_lost_sources_are_named_and_a_capture_that_ended_clears_the_captions(self):
        view, _notebook = self._ready_recorder()
        view._recording_sources = "both"
        snapshot = self._recording_snapshot(system="recovering", system_age=20.0)
        snapshot["capture_health"]["microphone"] = {"state": "disconnected", "reason": "",
                                                    "last_packet_age": 14.0}
        view._sync_recording_trouble(snapshot, now=500.0)
        texts = [w.cget("text") for w in descendants(view.recording_trouble)
                 if isinstance(w, tk.Label)]
        self.assertIn("Sem áudio do microfone e do sistema · maior interrupção: 20 s", texts)
        self.assertEqual(view.source_captions["microphone"].cget("text"), "Sem áudio · há 14 s")
        self.assertEqual(view.source_captions["system"].cget("text"), "Reconectando… · há 20 s")
        view._sync_recording_trouble({**snapshot, "state": "idle"}, now=501.0)
        self.assertIsNone(view.recording_trouble.kind)
        self.assertEqual([label.cget("text") for label in view.source_captions.values()], ["", ""])

    def test_a_source_left_out_of_the_recording_is_captioned_as_such(self):
        view, _notebook = self._ready_recorder()
        view._recording_sources = "microphone"
        view._sync_recording_trouble(self._recording_snapshot(), now=10.0)
        self.assertEqual(view.source_captions["system"].cget("text"), "Não incluído nesta gravação")
        self.assertEqual(view.source_captions["microphone"].cget("text"), "Recebendo sinal")

    def test_a_capture_error_stays_dismissed_only_while_it_lasts(self):
        view, _notebook = self._ready_recorder()
        view._recording_sources = "both"
        snapshot = self._recording_snapshot(error="Verifique a captura de áudio: gap")
        view._sync_recording_trouble(snapshot, now=10.0)
        trouble = view.recording_trouble
        self.assertEqual(trouble.kind, "problem")
        self.assertIn("Detalhes técnicos: Verifique a captura de áudio: gap",
                      [w.get("1.0", "end-1c") for w in descendants(trouble) if isinstance(w, tk.Text)])
        next(w for w in descendants(trouble) if isinstance(w, tk.Button)
             and w.cget("text") == "Fechar").invoke()
        self.assertIsNone(trouble.kind)
        # The same trouble stays dismissed while it lasts ...
        view._sync_recording_trouble(snapshot, now=11.0)
        self.assertIsNone(trouble.kind)
        # ... and a later recording that hits it again shows it again.
        view._sync_recording_trouble({**snapshot, "error": ""}, now=12.0)
        view._sync_recording_trouble(snapshot, now=13.0)
        self.assertEqual(trouble.kind, "problem")

    def test_cancel_controls_exist_only_while_their_job_runs(self):
        view, _notebook = self._ready_recorder()
        for button_name, attribute, running in (
                ("semantic_cancel_button", "semantic_cancel", threading.Event()),
                ("rebuild_cancel_button", "rebuild_cancel", threading.Event()),
                ("cross_cancel_button", "cross_pending", 3),
                ("cancel_summary_button", "summary_pending", ("s1", 1)),
                ("summary_download_cancel_button", "summary_download_cancel", threading.Event())):
            with self.subTest(button=button_name):
                button = getattr(view, button_name)
                self.assertEqual(button.winfo_manager(), "")
                setattr(view, attribute, running)
                view._sync_job_cancels()
                self.assertNotEqual(button.winfo_manager(), "")
                self.assertEqual(str(button.cget("state")), "normal")
                setattr(view, attribute, None)
                view._sync_job_cancels()
                self.assertEqual(button.winfo_manager(), "")

    def test_the_download_cancel_comes_and_goes_without_moving_what_is_below_it(self):
        view, notebook = self._ready_recorder()
        notebook.select(view.settings_tab)
        view.settings_sections.select("models")
        view.model_sections.select("summary")
        self.root.update()
        button = view.summary_download_cancel_button
        row = button.master
        height = row.winfo_height()
        before = [w.winfo_rooty() for w in row.master.pack_slaves()]
        view.summary_download_cancel = threading.Event()
        view._sync_job_cancels()
        self.root.update()
        self.assertTrue(button.winfo_ismapped())
        self.assertEqual(row.winfo_height(), height)
        self.assertEqual([w.winfo_rooty() for w in row.master.pack_slaves()], before)
        view.summary_download_cancel = None
        view._sync_job_cancels()
        self.root.update()
        self.assertFalse(button.winfo_ismapped())
        self.assertEqual(row.winfo_height(), height)

    def test_every_job_cancel_reserves_its_width_and_height_while_hidden(self):
        view, _notebook = self._ready_recorder()
        for name in ("semantic_cancel_button", "rebuild_cancel_button", "cross_cancel_button",
                     "cancel_summary_button", "summary_download_cancel_button",
                     "cancel_processing_button"):
            with self.subTest(button=name):
                button = getattr(view, name)
                row = button.master
                self.root.update_idletasks()
                before = (row.winfo_reqwidth(), row.winfo_reqheight())
                siblings = [(sibling, sibling.winfo_x(), sibling.winfo_width())
                            for sibling in row.winfo_children() if sibling is not button]
                view._show_cancel(button, True)
                self.root.update_idletasks()
                self.assertEqual((row.winfo_reqwidth(), row.winfo_reqheight()), before)
                self.assertEqual([(sibling, sibling.winfo_x(), sibling.winfo_width())
                                  for sibling, _x, _width in siblings], siblings)
                view._show_cancel(button, False)
                self.root.update_idletasks()
                self.assertEqual((row.winfo_reqwidth(), row.winfo_reqheight()), before)

    def test_every_parked_cancel_is_painted_above_its_slot_in_its_page(self):
        view, notebook = self._ready_recorder()
        self.root.after_cancel(view.after_id)
        view.after_id = None
        previous_topmost = view.window.attributes("-topmost")
        try:
            # Keep the synthetic fixture above the editor while checking real
            # desktop hit targets; a foreign window can otherwise cover it.
            view.window.attributes("-topmost", True)
            view.window.lift()
            for name, panel, section in (
                    ("semantic_cancel_button", None, "search"),
                    ("cross_cancel_button", "cross", None),
                    ("rebuild_cancel_button", None, "search"),
                    ("cancel_processing_button", None, "transcript"),
                    ("cancel_summary_button", None, "summary"),
                    ("summary_download_cancel_button", None, "models")):
                with self.subTest(button=name):
                    for key, widget in view.library_panels.items():
                        if widget.winfo_manager():
                            view._toggle_library_panel(key)
                    notebook.select(view.settings_tab if section in {"models", "search"} else view.library_tab)
                    if panel:
                        view._toggle_library_panel(panel)
                    if section in {"models", "search"}:
                        view.settings_sections.select("models")
                        view.model_sections.select("search" if section == "search" else "summary")
                    elif section:
                        view._show_library_detail(True)
                        view.detail_sections.select(section)
                        if section == "summary" and not view.summary_setup.winfo_manager():
                            view.summary_setup_toggle.invoke()
                    button = getattr(view, name)
                    view._show_cancel(button, True)
                    self.root.update()
                    if section in {"models", "search", "summary"}:
                        canvas = view.settings_canvas if section in {"models", "search"} else view.detail_canvas
                        canvas.yview_moveto(1)
                        self.root.update()
                    self.assertTrue(button.winfo_ismapped())
                    center = (button.winfo_rootx() + button.winfo_width() // 2,
                              button.winfo_rooty() + button.winfo_height() // 2)
                    # Mapped is insufficient: a later-created sibling can cover
                    # the label and intercept clicks while all size checks pass.
                    self.assertIs(self.root.winfo_containing(*center), button)
                    view._show_cancel(button, False)
                    self.assertFalse(button.winfo_manager())
        finally:
            view.window.attributes("-topmost", previous_topmost)

    def test_readiness_strip_is_one_quiet_line_when_healthy_and_names_trouble(self):
        view, _notebook = self._ready_recorder()
        view._submit = mock.Mock(return_value=True)
        view.readiness_probe.settle({"profile": "balanced", "voice": {"enabled": True}}, None)
        view._summary_inventory_state = "ready"
        view._readiness_next_render = 0
        view._sync_readiness(now=0)
        self.assertEqual(len(view.readiness_strip.winfo_children()), 1)
        self.assertEqual(view._readiness_rows[0].key, "healthy")
        view.retention_ready = False
        view.startup_status = "Synthetic startup recovery"
        view._summary_inventory_state = "unknown"
        view.readiness_probe.settle(None, "synthetic inventory read error")
        view._sync_readiness(now=1)
        self.assertEqual([row.key for row in view._readiness_rows], ["startup", "inventory", "summary"])
        self.assertEqual(len([w for w in descendants(view.readiness_strip) if isinstance(w, tk.Button)]), 3)

    def test_processing_banner_is_only_for_its_selected_session_and_audio_percent(self):
        view, notebook = self._ready_recorder()
        self.root.after_cancel(view.after_id)
        view.after_id = None
        notebook.select(view.library_tab)
        view._show_library_detail(True)
        view.selected = "one"
        live = {"processing": True, "processing_session": "one", "processing_kind": "transcription",
                "postprocess": "Synthetic transcription"}
        view._sync_processing_banner(live, now=10)
        view._sync_processing_banner(live, now=25)
        self.root.update()
        self.assertTrue(view.processing_banner.winfo_ismapped())
        self.assertIn("00:00:15", view.processing_banner_label.cget("text"))
        self.assertFalse(view.detail_processing_progress.winfo_manager())
        cancel = next(widget for widget in descendants(view.processing_banner)
                      if isinstance(widget, tk.Button) and widget.cget("text") == "Cancelar")
        cancel.invoke()
        wait_for(lambda: view.controller.cancel_processing.called)
        view.controller.cancel_processing.assert_called_once_with()
        live["audio_export"] = {"session_id": "one", "stage": "encoding", "percent": 42}
        view._sync_processing_banner(live, now=26)
        self.assertEqual(float(view.detail_processing_progress.cget("value")), 42)
        self.assertIn("42%", view.processing_banner_label.cget("text"))
        view.selected = "other"
        view._sync_processing_banner(live, now=27)
        self.assertFalse(view.processing_banner.winfo_manager())

    def test_summary_defaults_to_result_and_preserves_an_explicit_detail_choice(self):
        view, notebook = self._ready_recorder()
        notebook.select(view.library_tab)
        view._show_library_detail(True)
        view.selected = "one"
        view._load_transcript_document = mock.Mock()
        view.refresh_reports = mock.Mock()
        metadata = {"id": "one", "title": "Synthetic meeting", "status": "completed", "duration": 45,
                    "summary": "Synthetic saved summary", "tracks": {}, "final_available": False}
        view._detail_loaded("one", (metadata, []), None)
        self.assertEqual(view.detail_sections.current, "summary")
        self.assertFalse(view.summary_setup.winfo_manager())
        view.detail_sections.buttons["files"].invoke()
        view._submit = mock.Mock(return_value=True)
        view.load_session("other")
        self.assertEqual(view.detail_sections.current, "files")
        view._detail_loaded("other", ({**metadata, "id": "other"}, []), None)
        self.assertEqual(view.detail_sections.current, "files")

    def test_summary_completion_respects_navigation_and_explicit_section_changes(self):
        view, notebook = self._ready_recorder()
        notebook.select(view.library_tab)
        self.root.update()
        view.selected, view.summary_text = "one", "Synthetic summary"
        context = view._current_completion_context()
        view._summary_completed("one", context)
        self.assertEqual(view.detail_sections.current, "summary")
        view.detail_sections.buttons["files"].invoke()
        view._summary_completed("one", context)
        self.assertEqual(view.detail_sections.current, "files")
        notebook.select(view.settings_tab)
        self.root.update()
        view._summary_completed("one", context)
        self.assertEqual(notebook.select(), str(view.settings_tab))

    def test_waveform_lanes_hide_when_idle_and_return_for_source_preview(self):
        view, _notebook = self._ready_recorder()
        free = {"state": "idle", "processing": False, "levels": {}, "elapsed": 0}
        self._poll_once(view, free)
        self.assertFalse(view.waveform.winfo_manager())
        self._poll_once(view, {**free, "previewing": True})
        self.assertEqual(view.waveform.winfo_manager(), "grid")
        self._poll_once(view, self._recording_snapshot())
        self.assertEqual(view.waveform.winfo_manager(), "grid")

    def test_disabled_recovery_controls_name_what_they_wait_for(self):
        view, _notebook = self._ready_recorder()
        view.selected, view.detail_ready = "s1", True
        view.final_available, view.recovery_needed, view.playback_choices = False, True, ()
        base = {"state": "idle", "levels": {}, "elapsed": 0, "processing": False}
        self._poll_once(view, base)
        self.assertEqual(str(view.transcribe_button.cget("state")), "disabled")
        self.assertEqual(view.processing_job_status.get(),
                         "Transcrever de novo requer o áudio final. Use Preparar áudio final, em Arquivos.")
        self.assertEqual(str(view.play_button.cget("state")), "disabled")
        self.assertEqual(view.playback_status.get(), "Esta gravação ainda não tem áudio final para ouvir.")
        self.assertEqual(str(view.recover_audio_button.cget("state")), "normal")
        # A job of another recording holds all three, and the page names it.
        self._poll_once(view, {**base, "processing": True, "processing_kind": "summary",
                               "processing_session": "s2"})
        self.assertEqual(str(view.recover_audio_button.cget("state")), "disabled")
        self.assertEqual(view.recover_audio_note.cget("text"), "Aguarde o resumo terminar.")
        self.assertEqual(view.playback_status.get(), "Aguarde o resumo terminar.")
        # Nothing to recover is also said, not just greyed out.
        view.final_available, view.recovery_needed, view.playback_choices = True, False, ("Áudio final",)
        self._poll_once(view, base)
        self.assertEqual(view.recover_audio_note.cget("text"),
                         "O áudio final está completo; não há nada a recuperar.")
        self.assertEqual(str(view.recover_audio_button.cget("state")), "disabled")
        self.assertEqual((str(view.transcribe_button.cget("state")), str(view.play_button.cget("state"))),
                         ("normal", "normal"))
        self.assertEqual(view.playback_status.get(), "Pronto para ouvir.")


if __name__ == "__main__":
    unittest.main()
