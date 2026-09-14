"""Workspace concurrency, selection, persistence, and shared-root smoke checks."""

import threading
import time
import tkinter as tk
import unittest
from unittest import mock

from meeting_gui import (
    BackgroundBridge, BOOKMARK_LIMIT, MeetingWindow, NOTES_LIMIT,
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
    def test_missing_manual_device_stays_pinned(self):
        selection = EndpointSelection("manual", "opaque-id")
        options = endpoint_options([], "system", selection)
        self.assertEqual(options[-1][1], selection)
        self.assertIn("Indisponível", options[-1][0])
        self.assertEqual(options[1][1].argument(), "default:communications")

    def test_device_ids_disambiguate_names_and_sources(self):
        options = endpoint_options([
            {"id": "a", "name": "Same", "kind": "microphone"},
            {"id": "b", "name": "Same", "kind": "microphone"},
            {"id": "c", "name": "Output", "kind": "system"},
        ], "microphone", EndpointSelection())
        self.assertEqual([selection.endpoint_id for _, selection in options[2:]], ["a", "b"])
        self.assertNotEqual(options[2][0], options[3][0])

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

    def test_initial_saved_manual_selection_overrides_constructor_defaults(self):
        view = MeetingWindow.__new__(MeetingWindow)
        view.raw_settings = {}
        view.settings = resolve_meeting_settings({})
        view.devices = []
        view.options = {"microphone": [("old default", EndpointSelection())]}
        view.endpoint_vars = {track: Variable("old default") for track in ("microphone", "system")}
        view.endpoint_boxes = {track: mock.Mock() for track in ("microphone", "system")}
        for name in ("sources", "profile", "language", "profile_display", "language_display", "hotkey", "summary_model", "status"):
            setattr(view, name, Variable())
        view.language_box = mock.Mock()
        view._settings_loaded({"meeting_microphone": {"mode": "manual", "endpoint_id": "missing"}}, None)
        self.assertTrue(view.settings_loaded)
        self.assertIn("Indisponível", view.endpoint_vars["microphone"].get())
        self.assertEqual(dict(view.options["microphone"])[view.endpoint_vars["microphone"].get()].endpoint_id, "missing")

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


if __name__ == "__main__":
    unittest.main()
