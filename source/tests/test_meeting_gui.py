"""Workspace concurrency, selection, persistence, and shared-root smoke checks."""

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
    BackgroundBridge, BOOKMARK_LIMIT, MAX_PAGE_BACKSTACK, MeetingWindow, NOTES_LIMIT,
    TRANSCRIPT_PAGE_SIZE,
    add_meeting_tabs, destination_display, endpoint_options, format_recording_status,
    format_time, open_meeting_window, validated_settings,
)
from meeting_settings import EndpointSelection, resolve_meeting_settings


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

    def test_library_filters_normalize_multi_value_labels_without_file_access(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.collection_filter = Variable("project-1, project-2")
        view.tag_filter = Variable("planning")
        view.people_filter = Variable("Alice, Bob")
        view.series_filter = Variable("")
        view.date_from_filter = Variable("2026-09-01")
        view.date_to_filter = Variable("2026-09-30")

        self.assertEqual(view._library_filters(), {
            "collection": ["project-1", "project-2"],
            "tag": "planning", "person": ["Alice", "Bob"], "series": None,
            "date_from": "2026-09-01", "date_to": "2026-09-30",
        })

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
        view.status_filter = Variable("Concluídos")
        view.cross_cancel_button = mock.Mock()
        view.cross_status = Variable()
        view.cross_request = 0
        view.closed = False
        view._library_filters = mock.Mock(return_value={"tag": "planning"})
        view.controller = mock.Mock()
        view._submit = mock.Mock()

        view.ask_across_meetings()
        operation = view._submit.call_args.args[1]
        operation()

        view.controller.ask_across_meetings.assert_called_once_with(
            "What was decided?", "local-model",
            filters={"tag": "planning", "status": "completed"},
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
        view.truncated, view.dirty = False, True
        view.title, view.notes = Variable("Title"), Text("Notes")
        view.bookmarks = []
        view.status = Variable()
        view.controller = mock.Mock()
        view.refresh_library = mock.Mock()
        view._submit = mock.Mock(return_value=True)
        view.delete_button = mock.Mock()
        return view

    def test_failed_save_preserves_unsaved_notes_and_does_not_navigate(self):
        view = self.make_edit_view()
        after = mock.Mock()
        view.save_notes(after)
        callback = view._submit.call_args.args[2]
        callback(False, None)
        self.assertTrue(view.dirty)
        after.assert_not_called()
        self.assertIn("salvar", view.status.get())

    def test_edit_during_save_is_not_cleared_or_discarded(self):
        view = self.make_edit_view()
        after = mock.Mock()
        view.save_notes(after)
        callback = view._submit.call_args.args[2]
        view.bookmarks.append({"timestamp": 2, "label": "new"})
        callback(True, None)
        self.assertTrue(view.dirty)
        after.assert_not_called()

    def test_loading_detail_prevents_saving_old_notes_to_new_session(self):
        view = self.make_edit_view()
        view.detail_ready = False
        view.save_notes()
        view._submit.assert_not_called()

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
        self.assertIn("excluída", view.status.get())

    def test_delete_selected_cancel_preserves_the_recording(self):
        view = self.make_edit_view()
        view.window = mock.Mock()
        view.delete_button = mock.Mock()

        with mock.patch("meeting_gui.messagebox.askyesno", return_value=False):
            view.delete_selected()

        view._submit.assert_not_called()
        view.controller.delete_session.assert_not_called()

    def test_detail_projection_bounds_notes_bookmarks_and_transcript_text(self):
        view = self.make_edit_view()
        view.bridge, view.transcript = mock.Mock(), mock.Mock()
        view.transcript.get_children.return_value = []
        view.segment_text = Text()
        view.segments = {}
        view.controller.get_session.return_value = {
            "id": "two", "notes": "x" * (NOTES_LIMIT + 1),
            "bookmarks": [{"timestamp": 1}] * (BOOKMARK_LIMIT + 1),
            "events": ["private audio descriptors"],
        }
        view.controller.get_transcript.return_value = [{"text": "a" * 10000, "track": "microphone", "start": 0}]
        view.load_session("two")
        self.assertFalse(view.detail_ready)
        self.assertEqual(view.notes.state, "disabled")
        metadata, segments = view._submit.call_args.args[1]()
        self.assertEqual(len(metadata["notes"]), NOTES_LIMIT)
        self.assertEqual(len(metadata["bookmarks"]), BOOKMARK_LIMIT)
        self.assertTrue(metadata["truncated"])
        self.assertNotIn("events", metadata)
        self.assertEqual(len(segments[0]["text"]), 8000)

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

    def test_manual_speaker_label_overrides_generated_projection(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.speaker_labels = {
            "speaker-1": {"segment_id": "segment-1", "label": "Manual label"},
        }
        self.assertEqual(
            view._speaker_for_segment({"id": "segment-1", "speaker": "Generated label"}),
            "Manual label",
        )

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
        manager.geometry("1120x820")
        notebook = ttk.Notebook(manager)
        notebook.pack(fill="both", expand=True)
        view = add_meeting_tabs(
            self.root, manager, notebook, controller, lambda: {}, mock.Mock(),
        )
        try:
            notebook.select(view.settings_tab)
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
