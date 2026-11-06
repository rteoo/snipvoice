"""Busca for the Tauri manager (``manager_search``).

The controller is a fake; the repair thread is awaited through events with
timeouts, never bare sleeps.
"""

import os
import sys
import threading
import unittest

SOURCE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, SOURCE_DIR)

import i18n
from manager_bridge import RpcFailure
from manager_search import SearchActions

WAIT = 5.0


class FakeMeetings:
    def __init__(self):
        self.index = "ready"
        self.repair_started = threading.Event()
        self.block_repair = False

    def index_state(self):
        if isinstance(self.index, Exception):
            raise self.index
        return self.index

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

        def notify(method, params):
            self.notified.append((method, params))
            if method == "search.finished":
                self.finished.set()

        self.actions = SearchActions(meetings=self.meetings, notify=notify)

    def tearDown(self):
        i18n.set_language("pt-BR")

    def test_the_state_names_the_word_index(self):
        self.assertEqual(self.actions.state({}), {"index": "Índice de busca: pronto", "repair": None})

    def test_semantic_search_methods_are_gone(self):
        self.assertEqual(set(self.actions.methods()), {"search.state", "search.repair", "search.cancelRepair"})

    def test_an_unreadable_index_fails_with_its_detail(self):
        self.meetings.index = OSError("disk")
        with self.assertRaises(RpcFailure) as caught:
            self.actions.state({})
        self.assertIn("disk", caught.exception.detail)

    def test_a_repair_runs_off_the_request_and_reports_success(self):
        self.assertEqual(self.actions.repair({}), {"status": "started"})
        self.assertTrue(self.finished.wait(WAIT))
        self.assertEqual(self.notified[-1], ("search.finished", {"ok": True}))
        self.assertIsNone(self.actions.state({})["repair"])

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

    def test_cancelling_without_a_repair_is_invalid(self):
        with self.assertRaises(RpcFailure):
            self.actions.cancel_repair({})


if __name__ == "__main__":
    unittest.main()
