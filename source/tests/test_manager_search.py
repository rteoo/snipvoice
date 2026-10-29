"""Busca for the Tauri manager (``manager_search``).

The controller and embedding module are fakes; the repair and download
threads are awaited through events with timeouts, never bare sleeps.
"""

import os
import sys
import threading
import types
import unittest

SOURCE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, SOURCE_DIR)

import i18n
import manager_search
from manager_bridge import RpcFailure
from manager_search import SearchActions

WAIT = 5.0


class FakeMeetings:
    def __init__(self):
        self.enabled = False
        self.semantic = "disabled"
        self.calls = []
        self.repair_started = threading.Event()
        self.block_repair = False

    def semantic_info(self):
        return {"enabled": self.enabled, "installed": False, "state": self.semantic}

    def index_state(self):
        return "ready"

    def set_semantic_search(self, enabled):
        self.calls.append(("set", enabled))
        self.enabled = enabled
        self.semantic = "unavailable" if enabled else "disabled"

    def rebuild_semantic_index(self):
        self.calls.append(("reindex",))
        return {"state": "rebuilding"}

    def cancel_semantic_index(self):
        self.calls.append(("cancel_index",))

    def semantic_state(self):
        return self.semantic

    def rebuild_index(self, *, cancel_event, progress):
        self.repair_started.set()
        progress(1, 2)
        if self.block_repair:
            cancel_event.wait(WAIT)
            raise RuntimeError("cancelled")
        progress(2, 2)
        return {"state": "ready", "sessions": 2}


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.meetings = FakeMeetings()
        self.notified = []
        self.finished = threading.Event()
        self.warnings = []
        self.downloads = []

        def notify(method, params):
            self.notified.append((method, params))
            if method == "search.finished":
                self.finished.set()

        embedding = types.SimpleNamespace(
            MODEL={"name": "EmbeddingGemma", "size_bytes": 500_000_000, "license_id": "Apache-2.0"},
            download_embedding_model=lambda *, cancel_event, progress: self.downloads.append(True),
        )
        self.actions = SearchActions(meetings=self.meetings, embedding_models=embedding, notify=notify,
                                     logger=types.SimpleNamespace(warning=self.warnings.append))
        self.interval = manager_search.WATCH_INTERVAL
        manager_search.WATCH_INTERVAL = 0.01

    def tearDown(self):
        manager_search.WATCH_INTERVAL = self.interval
        i18n.set_language("pt-BR")

    def test_word_search_is_the_default(self):
        state = self.actions.state({})
        self.assertEqual(state["status"], "Busca por palavras ativa.")
        self.assertEqual(state["index"], "Índice de busca: pronto")
        self.assertFalse(state["enabled"])

    def test_reindexing_needs_semantic_search_on(self):
        with self.assertRaises(RpcFailure):
            self.actions.reindex({})
        state = self.actions.set_semantic({"enabled": True})
        self.assertTrue(state["enabled"])
        self.assertIn("indisponível", state["status"])
        self.actions.reindex({})
        self.assertIn(("reindex",), self.meetings.calls)

    def test_the_toggle_requires_a_boolean(self):
        with self.assertRaises(RpcFailure):
            self.actions.set_semantic({"enabled": "yes"})

    def test_the_model_download_is_confirmed_then_runs_off_the_request(self):
        self.assertEqual(self.actions.download_model({})["status"], "confirm")
        self.assertEqual(self.downloads, [])
        self.assertEqual(self.actions.download_model({"confirmed": True}), {"status": "started"})
        self.assertTrue(self.finished.wait(WAIT))
        self.assertEqual(self.downloads, [True])
        self.assertTrue(self.notified[-1][1]["ok"])

    def test_a_cancelled_repair_reports_cancelled_and_frees_the_slot(self):
        self.meetings.block_repair = True
        self.actions.repair({})
        self.assertTrue(self.meetings.repair_started.wait(WAIT))
        with self.assertRaises(RpcFailure):
            self.actions.repair({})
        self.assertIsNotNone(self.actions.state({})["repair"])
        self.actions.cancel_repair({})
        self.assertTrue(self.finished.wait(WAIT))
        self.assertTrue(self.notified[-1][1]["cancelled"])
        self.assertIsNone(self.actions.state({})["repair"])

    def test_the_watcher_pushes_state_changes_and_stops(self):
        changed = threading.Event()
        original = self.actions.notify

        def notify(method, params):
            original(method, params)
            if method == "search.changed" and params["semantic"] == "ready":
                changed.set()

        self.actions.notify = notify
        self.meetings.enabled, self.meetings.semantic = True, "rebuilding"
        self.actions.reindex({})
        self.meetings.semantic = "ready"
        self.assertTrue(changed.wait(WAIT))
        self.assertEqual(self.warnings, [])


if __name__ == "__main__":
    unittest.main()
