"""Workspace concurrency, selection, persistence, and shared-root smoke checks."""

import os
import threading
import time
import tkinter as tk
import unittest
from tkinter import ttk
from unittest import mock

from meeting_gui import (
    BackgroundBridge, BOOKMARK_LIMIT, MeetingWindow, NOTES_LIMIT, add_meeting_tabs,
    endpoint_options, format_time, open_meeting_window, validated_settings,
)
from meeting_settings import EndpointSelection, resolve_meeting_settings


def wait_for(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Timed out waiting for background GUI work")
        time.sleep(0.005)


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
                     "summary_model", "summary_display", "status", "destination"):
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
                ["Gravação", "Biblioteca", "Settings"],
            )
        finally:
            view.close_without_prompt(destroy=False)
            manager.destroy()
            self.root.update()


if __name__ == "__main__":
    unittest.main()
