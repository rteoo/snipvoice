"""Workspace concurrency, selection, persistence, and shared-root smoke checks."""

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

from meeting_gui import (
    APPEARANCE_LABELS, BackgroundBridge, INDEX_STATE_LABELS, MAX_PAGE_BACKSTACK,
    MeetingWindow, PROFILE_LABELS,
    TRANSCRIPT_PAGE_SIZE,
    add_meeting_tabs, destination_display, endpoint_options, format_recording_status,
    format_time, meter_value, open_meeting_window, playback_sources,
    retention_plan_projection, validated_settings,
)
from meeting_index import INDEX_STATES
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


class ReplayChoiceTests(unittest.TestCase):
    def test_final_mix_is_primary_even_after_raw_retention(self):
        tracks = {"microphone": {"available": False, "raw_removed": True},
                  "system": {"available": True}}
        self.assertEqual(playback_sources(tracks, True), ("Áudio final", "Sistema"))
        self.assertEqual(playback_sources(tracks, False), ("Sistema",))
        self.assertEqual(playback_sources({}, False), ())

    def test_direct_replay_starts_saved_mix_at_selected_position(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.selected = "meeting"
        view.detail_ready = True
        view.snapshot = {"state": "idle"}
        view.playback_choices = ("Áudio final", "Microfone")
        view.audio_source = Variable("Áudio final")
        view.playback_position = Variable(12.5)
        view.playback_duration = 30.0
        view.playback_status = Variable()
        view._action = mock.Mock()
        view.play_selected_recording()
        self.assertEqual(view._action.call_args.args[:3],
                         ("seek_playback", "meeting", "final"))
        self.assertEqual(view._action.call_args.kwargs["start"], 12.5)


class Text:
    def __init__(self, text=""):
        self.text = text
        self.state = "normal"

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


class MeetingGuiLogicTests(unittest.TestCase):
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
        view.track = Variable("Microfone")
        view.position = Variable("12.5")
        view.raw_unavailable_tracks = set()
        view._action = mock.Mock()
        view.play()
        view._action.assert_called_once_with(
            "seek_playback", "meeting", "microphone", start=12.5, urgent=True,
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
        view = MeetingWindow.__new__(MeetingWindow)
        view.record_details_button = mock.Mock()
        view._remember_operation_error(
            r"failed C:\Users\Alice Smith\meeting; then \\server\share\Bob Jones\audio"
        )
        self.assertNotIn("Alice", view.record_details)
        self.assertNotIn("server", view.record_details)
        self.assertNotIn("Bob", view.record_details)
        self.assertEqual(view.record_details.count("[caminho local]"), 2)

    def test_start_waits_for_inflight_privacy_save(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.closed = False
        view.privacy_save_inflight = True
        view.status = Variable()
        view._validated_recording_request = mock.Mock()
        self.assertFalse(view.request_start("hotkey"))
        view._validated_recording_request.assert_not_called()
        self.assertIn("privacidade", view.status.get())

    def test_startup_failure_disables_recording_and_retention_controls(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.closed = False
        view.start_button = mock.Mock()
        view.delete_button = mock.Mock()
        view.raw_remove_button = mock.Mock()
        view.status = Variable("Preparando privacidade e recuperação local…")
        view.privacy_status = Variable()
        view.set_startup_status(False, r"failed at C:\Users\private\meeting")
        view.start_button.configure.assert_called_with(state="disabled")
        view.delete_button.configure.assert_called_with(state="disabled")
        view.raw_remove_button.configure.assert_called_with(state="disabled")
        self.assertNotIn("C:\\Users", view.status.get())

    def test_shared_start_path_routes_window_hotkey_and_tray_to_notice(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.closed = False
        view.retention_ready = True
        view.privacy_ready = True
        view.controller = mock.Mock()
        view.controller.recording_notice_required.return_value = True
        view._validated_recording_request = mock.Mock(return_value=("settings", "title"))
        view._show_recording_notice = mock.Mock()
        view.status = Variable()
        for origin in ("window", "hotkey", "tray"):
            view.request_start(origin)
        self.assertEqual([item.args[0] for item in view._show_recording_notice.call_args_list],
                         ["window", "hotkey", "tray"])

    def test_start_consent_cancel_never_submits_and_confirm_is_one_start(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.closed = False
        view.retention_ready = True
        view.privacy_ready = True
        view.controller = mock.Mock()
        view.controller.recording_notice_required.return_value = False
        view._validated_recording_request = mock.Mock(return_value=("settings", "title"))
        view._submit = mock.Mock(return_value=True)
        view.status = Variable()
        self.assertTrue(view.request_start("hotkey"))
        operation = view._submit.call_args.args[1]
        operation()
        view.controller.start.assert_called_once_with("settings", title="title")
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

    def test_raw_capability_gating_keeps_transcript_surface_available(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.raw_unavailable_tracks = set()
        view.raw_capabilities = {}
        view.raw_remove_microphone = Variable()
        view.raw_remove_system = Variable()
        view.raw_remove_microphone_check = mock.Mock()
        view.raw_remove_system_check = mock.Mock()
        view.play_button = mock.Mock()
        view.transcribe_button = mock.Mock()
        view.export_audio_button = mock.Mock()
        view.raw_remove_button = mock.Mock()
        view.audio_capability_status = Variable()
        view.track = Variable("Microfone")
        view._set_audio_capabilities({
            "microphone": {"available": False, "raw_removed": True},
            "system": {"available": True},
        })
        self.assertEqual(view.raw_unavailable_tracks, {"microphone"})
        self.assertEqual(view.playback_choices, ("Sistema",))
        view.play_button.configure.assert_called_with(state="normal")
        view.transcribe_button.configure.assert_called_with(state="normal")
        self.assertIn("transcrição", view.audio_capability_status.get().lower())

    def test_raw_capability_gating_keeps_remaining_source_actions_available(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.retention_ready = True
        view.raw_remove_microphone = Variable()
        view.raw_remove_system = Variable()
        view.raw_remove_microphone_check = mock.Mock()
        view.raw_remove_system_check = mock.Mock()
        view.play_button = mock.Mock()
        view.transcribe_button = mock.Mock()
        view.export_audio_button = mock.Mock()
        view.raw_remove_button = mock.Mock()
        view.audio_capability_status = Variable()
        view.track = Variable("Sistema")
        view._set_audio_capabilities({
            "microphone": {"available": False, "raw_removed": True},
            "system": {"available": True},
        })
        view.play_button.configure.assert_called_with(state="normal")
        view.transcribe_button.configure.assert_called_with(state="normal")
        view.export_audio_button.configure.assert_called_with(state="normal")

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
        view = MeetingWindow.__new__(MeetingWindow)
        view.summary_text = "Resumo legível\n\nDecisões:\n• Enviar relatório"
        view.status = Variable()
        view._submit = mock.Mock(return_value=True)
        with mock.patch("meeting_gui.Clipboard.set_content", return_value=True) as copy:
            view.copy_summary()
            key, operation, callback = view._submit.call_args.args
            self.assertEqual(key, "copy_summary")
            operation()
            callback(True, None)
        copy.assert_called_once_with(view.summary_text)
        self.assertEqual(view.status.get(), "Resumo copiado.")

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
        view = MeetingWindow.__new__(MeetingWindow)
        view.closed = False
        view.selected = "meeting-1"
        view.detail_ready = True
        view.retention_ready = True
        view.retention_request = 0
        view.controller = mock.Mock()
        view.title = Variable("Planning")
        view.delete_button = mock.Mock()
        view.status = Variable()
        view.window = object()
        view._remember_operation_error = mock.Mock()
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
        with mock.patch("meeting_gui.messagebox.askyesno", return_value=True) as confirm:
            preview_call.args[2](plan, None)
        prompt = confirm.call_args.args[1]
        self.assertNotIn("C:\\Users", prompt)
        self.assertNotIn("D:\\exports", prompt)
        apply_call = view._submit.call_args_list[-1]
        self.assertEqual(apply_call.args[0], "retention_apply")
        apply_call.args[1]()
        view.controller.apply_retention.assert_called_once_with(plan, confirm=True)

    def test_trash_restore_purge_and_expired_empty_use_worker_confirmations(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.closed = False
        view.trash_request = 7
        view.trash_tree = mock.Mock()
        view.trash_tree.selection.return_value = ("0",)
        view.trash_entries = [{"session_id": "meeting-1"}]
        view.trash_status = Variable()
        view.trash_dialog = object()
        view.controller = mock.Mock()
        view._submit = mock.Mock(return_value=True)

        view.restore_selected_trash()
        restore_call = view._submit.call_args
        self.assertEqual(restore_call.args[0], "trash_restore")
        restore_call.args[1]()
        view.controller.restore_session.assert_called_once_with("meeting-1")

        with mock.patch("meeting_gui.messagebox.askyesno", return_value=True):
            view.purge_selected_trash()
            purge_call = view._submit.call_args
            self.assertEqual(purge_call.args[0], "trash_purge")
            purge_call.args[1]()
            view.empty_expired_trash()
            empty_call = view._submit.call_args
            self.assertEqual(empty_call.args[0], "trash_empty_expired")
            empty_call.args[1]()
        view.controller.purge_session.assert_called_once_with("meeting-1", confirm=True)
        view.controller.empty_trash.assert_called_once_with(confirm=True)

    def test_raw_track_removal_preview_applies_only_selected_track_plan(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.closed = False
        view.selected = "meeting-1"
        view.detail_ready = True
        view.retention_ready = True
        view.retention_request = 0
        view.raw_remove_button = mock.Mock()
        view.status = Variable()
        view.window = object()
        view._remember_operation_error = mock.Mock()
        view._submit = mock.Mock(return_value=True)
        view.controller = mock.Mock()
        plan = {
            "session_id": "meeting-1", "operation": "raw_tracks", "eligible": True,
            "byte_estimate": 64, "targets": [], "raw_tracks": ["microphone"],
            "lost_capabilities": ["playback:microphone"],
        }
        view.controller.plan_raw_tracks.return_value = plan

        view.preview_raw_tracks(("microphone",))
        preview_call = view._submit.call_args
        self.assertEqual(preview_call.args[0], "raw_retention_plan")
        self.assertEqual(preview_call.args[1](), plan)
        with mock.patch("meeting_gui.messagebox.askyesno", return_value=True):
            preview_call.args[2](plan, None)
        apply_call = view._submit.call_args_list[-1]
        self.assertEqual(apply_call.args[0], "raw_retention_apply")
        apply_call.args[1]()
        view.controller.plan_raw_tracks.assert_called_once_with("meeting-1", tracks=("microphone",))
        view.controller.apply_raw_tracks.assert_called_once_with(plan, confirm=True)

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

        stale = MeetingWindow.__new__(MeetingWindow)
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
        operation()

        view.controller.ask_across_meetings.assert_called_once_with(
            "What was decided?", "local-model",
            filters={},
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

        self.assertEqual(
            status,
            "Parcial · 00:00:31 · Gravação preservada · "
            "Uma fonte de áudio foi interrompida · Não foi possível concluir uma etapa",
        )
        self.assertNotIn("opaque-id", status)
        self.assertNotIn("HRESULT", status)

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

    def test_processing_errors_are_hidden_behind_recording_details(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.status = Variable()
        view.record_details = ""
        view.record_details_button = mock.Mock()

        view._processing_launched("meeting", False, "HRESULT 0x88890004")

        self.assertEqual(
            view.status.get(),
            "Não foi possível iniciar o processamento local. Veja os detalhes na aba Gravação.",
        )
        self.assertNotIn("HRESULT", view.status.get())
        self.assertIn("HRESULT", view.record_details)
        view.record_details_button.configure.assert_called_with(state="normal")

    def test_summary_inventory_result_is_applied_only_on_gui_callback(self):
        view = MeetingWindow.__new__(MeetingWindow)
        button = mock.Mock()
        view.summary_model_buttons = {"qwen": button}
        view.summary_model_status = Variable()
        view._summary_inventory_loaded({"qwen": True}, None)
        self.assertEqual(view.summary_model_installed, {"qwen": True})
        button.configure.assert_called_once_with(text="Remover", state="normal")

    def test_summary_download_shows_license_and_uses_background_job(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.window = mock.Mock()
        view.status = Variable()
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
        view.status = Variable()
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
                     "summary_model", "summary_display", "status", "destination",
                     "destination_label"):
            setattr(view, name, Variable())
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
        view = MeetingWindow.__new__(MeetingWindow)
        view.selected, view.detail_ready = "one", True
        view.dirty = True
        view.title = Variable("Title")
        view.status = Variable()
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
        self.assertIn("salvar", view.status.get())

    def test_edit_during_save_is_not_cleared_or_discarded(self):
        view = self.make_edit_view()
        after = mock.Mock()
        view.save_title(after)
        callback = view._submit.call_args.args[2]
        view.title.set("New title")
        callback(True, None)
        self.assertTrue(view.dirty)
        after.assert_not_called()

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
        self.assertIn("lixeira", view.status.get())

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
        view.controller.get_session.return_value = {
            "id": "two", "notes": "Legacy notes",
            "bookmarks": [{"timestamp": 1, "label": "Legacy bookmark"}],
            "tracks": {"microphone": {"available": True, "raw_removed": False}},
            "events": ["private audio descriptors"],
        }
        view.controller.get_transcript.return_value = [{"text": "a" * 10000, "track": "microphone", "start": 0}]
        view.load_session("two")
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
        )

    def test_audio_export_defaults_to_mp3_and_keeps_wav_available(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.selected = "meeting-1"
        view.raw_tracks_present = {"microphone"}
        view.raw_unavailable_tracks = set()
        view.window = mock.Mock()
        view.status = Variable()
        view._current_settings = mock.Mock(return_value=types.SimpleNamespace(
            destination="", voice_boost=True,
        ))
        view._action = mock.Mock()
        with mock.patch("meeting_gui.filedialog.asksaveasfilename", return_value="recording.mp3") as dialog:
            view.export_audio()
        self.assertEqual(dialog.call_args.kwargs["defaultextension"], ".mp3")
        self.assertEqual(dialog.call_args.kwargs["filetypes"],
                         (("Áudio MP3", "*.mp3"), ("Áudio WAV", "*.wav")))
        view._action.assert_called_once_with(
            "export_mixdown", "meeting-1", "recording.mp3", enhance_microphone=True,
        )

    def test_summary_display_is_readable_and_read_only(self):
        view = MeetingWindow.__new__(MeetingWindow)
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

    def test_regenerate_summary_dispatches_for_selected_recording(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.generate_report = mock.Mock()
        view.summarize()
        view.generate_report.assert_called_once_with()

    def test_adjust_audio_regenerates_final_audio_for_microphone_recording(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.selected = "meeting-1"
        view.detail_ready = True
        view.raw_tracks_present = {"microphone"}
        view.raw_unavailable_tracks = set()
        view.controller = mock.Mock()
        view._action = mock.Mock(
            side_effect=lambda method, session_id, **_kwargs:
            view.controller.regenerate_final_audio(session_id)
        )
        view.adjust_audio()

        self.assertEqual(view._action.call_args.args[:2],
                         ("regenerate_final_audio", "meeting-1"))
        self.assertIn("callback", view._action.call_args.kwargs)
        view.controller.regenerate_final_audio.assert_called_once_with("meeting-1")

    def test_adjust_audio_marks_only_the_successful_recording_for_final_selection(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.selected = "meeting-1"
        view.detail_ready = True
        view.raw_tracks_present = {"microphone"}
        view.raw_unavailable_tracks = set()
        view._action = mock.Mock()
        view._processing_launched = mock.Mock()
        view.adjust_audio()
        callback = view._action.call_args.kwargs["callback"]
        callback(True, None)
        self.assertEqual(view._adjusted_audio_target, "meeting-1")

    def test_refresh_outputs_selects_final_audio_only_for_adjusted_recording(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.selected = "meeting-1"
        view.detail_ready = True
        view.transcript_revision = "revision-1"
        view.transcript_offset = 0
        view.audio_source = Variable("Microfone")
        view.status = Variable()
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
        view._submit = mock.Mock()
        with mock.patch("meeting_gui.os.path.isfile", return_value=True):
            view.refresh_outputs("meeting-1")
            operation, callback = view._submit.call_args.args[1:]
            callback(operation(), None)
        self.assertEqual(view.audio_source.get(), "Áudio final")
        self.assertIsNone(view._adjusted_audio_target)

    def test_recording_finished_opens_clean_new_recording_and_tracks_processing(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.dirty = False
        view.processing_target = None
        view.refresh_library = mock.Mock()
        view.notebook = mock.Mock()
        view.library_tab = mock.Mock()
        view.load_session = mock.Mock()
        view._recording_finished({"session_id": "meeting-2", "processing": True})
        view.refresh_library.assert_called_once_with()
        view.notebook.select.assert_called_once_with(view.library_tab)
        view.load_session.assert_called_once_with("meeting-2")
        self.assertEqual(view.processing_target, "meeting-2")

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
        view.status = Variable()
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
        view.status = Variable()
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
        with mock.patch("meeting_gui.simpledialog.askstring", return_value="custom-copy"):
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
            "meeting_gui.simpledialog.askstring",
            side_effect=["Perfil", "custom-profile"],
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


class MeetingWindowSmokeTests(unittest.TestCase):
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

    def test_shared_root_build_and_background_settings_devices(self):
        controller = mock.Mock()
        controller.snapshot.return_value = {"state": "idle", "levels": {}, "elapsed": 0, "processing": False}
        controller.devices.return_value = [{"id": "input", "name": "Mic", "kind": "microphone"}]
        controller.list_sessions.return_value = []
        window = open_meeting_window(self.root, controller, lambda: {}, mock.Mock())
        view = window._meeting_view
        try:
            deadline = time.monotonic() + 2
            while not view.settings_loaded and time.monotonic() < deadline:
                self.root.update()
                time.sleep(0.01)
            self.assertIs(window.master, self.root)
            self.assertTrue(view.settings_loaded)
            self.assertEqual(view.hotkey.get(), "")
            self.assertIn("input", [selection.endpoint_id for _, selection in view.options["microphone"]])
        finally:
            view.close()
            self.root.update()

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
            self.root.update()
            self.assertTrue(view.status_footer.winfo_ismapped())
            self.assertLessEqual(
                view.status_footer.winfo_rooty() + view.status_footer.winfo_height(),
                manager.winfo_rooty() + manager.winfo_height(),
            )
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

    def _embedded_view(self, geometry="1120x820"):
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
            self.root, manager, notebook, controller, lambda: {}, mock.Mock(),
        )
        self.addCleanup(self.root.update)
        self.addCleanup(manager.destroy)
        self.addCleanup(view.close_without_prompt, destroy=False)
        return view, notebook

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
        self.assertEqual(list(models.frames), ["transcription", "summary"])
        self.assertEqual([models.buttons[key].cget("text") for key in models.frames],
                         ["Transcrição", "Resumos"])
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
        self.assertFalse(view.library_panels["tools"].winfo_ismapped())
        view.library_panel_toggles["cross"].invoke()
        self.root.update()
        self.assertTrue(view.library_panels["cross"].winfo_ismapped())
        view.library_panel_toggles["cross"].invoke()
        self.root.update()
        self.assertFalse(view.library_panels["cross"].winfo_ismapped())

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
        self.assertFalse(view.play_button.winfo_ismapped())
        self.assertNotIn("notes", view.detail_sections.frames)
        self.assertFalse(hasattr(view, "notes"))
        self.assertFalse(hasattr(view, "bookmark_picker"))
        self.assertFalse(hasattr(view, "transcript_tools_toggle"))
        self.assertNotIn("speaker", view.transcript["columns"])
        self.assertEqual(tuple(view.transcript["columns"]), ("time", "track", "text"))
        self.assertFalse(view.report_frame.winfo_ismapped())

    def test_library_replay_button_dispatches_saved_mix(self):
        view, notebook = self._embedded_view()
        notebook.select(view.library_tab)
        view._show_library_detail(True)
        view.selected = "synthetic-session"
        view.detail_ready = True
        view.playback_duration = 30.0
        view.playback_position.set(5.0)
        view._set_audio_capabilities({"microphone": {"available": True}}, True)
        self.root.update()
        self.assertEqual(view.audio_source.get(), "Áudio final")
        self.assertEqual(str(view.play_button.cget("state")), "normal")
        view.play_button.invoke()
        wait_for(lambda: (self.root.update(), view.controller.seek_playback.called)[1])
        view.controller.seek_playback.assert_called_once_with(
            "synthetic-session", "final", start=5.0,
        )

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
        wait_for(lambda: (self.root.update(), view.ask_pending is None)[1])
        turns = view.ask_conversations[view.selected]
        self.assertEqual(turns[0]["status"], "complete")
        self.assertEqual(turns[0]["answer"], "A revisão será na sexta-feira.")
        view.controller.ask_this_meeting.assert_called_once_with(
            "synthetic-session", "Qual foi a decisão?", "synthetic-model",
            revision="revision-1", history=[],
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
        wait_for(lambda: (self.root.update(), view.settings_loaded)[1])
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
                tuple(view.appearance_box.cget("values")),
                tuple(APPEARANCE_LABELS.values()),
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
            self.assertEqual(settings_radios, [])
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
        # Short enough that the summary-model section overflows its viewport.
        manager.geometry("1120x560")
        notebook = ttk.Notebook(manager)
        notebook.pack(fill="both", expand=True)
        view = add_meeting_tabs(
            self.root, manager, notebook, controller, lambda: {}, mock.Mock(),
        )
        try:
            notebook.select(view.settings_tab)
            view.settings_sections.select("models")
            view.model_sections.select("summary")
            self.root.update()
            view.settings_canvas.yview_moveto(0)
            self.root.update()
            target = next(
                widget for widget in descendants(view.settings_content)
                if isinstance(widget, tk.Label) and "Qwen3.5 4B" in widget.cget("text")
            )
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


if __name__ == "__main__":
    unittest.main()
