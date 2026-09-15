import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import meeting_summary
from meeting_store import MeetingStore
from summary_catalog import DEFAULT_SUMMARY_MODEL


class MeetingSummaryTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).resolve().parent / "tmp"
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.store = MeetingStore(self.temp.name)
        self.sid = self.store.begin({})
        self.store.finish(self.sid)
        self.revision = self.store.begin_revision(self.sid, "local", "pt")
        self.store.save_summary(self.sid, {"summary": "Prior summary"})

    def transcript(self, text="We agreed to review the report."):
        self.store.add_transcript(self.sid, self.revision, {
            "id": "s1", "track": "microphone", "start": 0, "end": 30, "text": text,
        })
        self.store.finish_revision(self.sid, self.revision)

    @staticmethod
    def result(ids=("s1",)):
        return {"summary": "Revisão do relatório.", "segment_ids": list(ids), "decisions": [],
                "action_items": [{"text": "Revisar o relatório", "owner": None,
                                  "deadline": None, "segment_ids": list(ids)}]}

    def runtime(self, responder=None):
        runtime = mock.Mock()
        if responder is None:
            def responder(_prompt, evidence, *_args, **_kwargs):
                identifier = evidence[0].get("id") or evidence[0]["segment_ids"][0]
                return json.dumps(self.result((identifier,)))
        runtime.generate.side_effect = responder
        return runtime

    def installed(self, runtime):
        return mock.patch.multiple(
            meeting_summary, summary_model_path=mock.Mock(return_value="model.gguf"),
            SummaryRuntime=mock.Mock(return_value=runtime),
        )

    def test_structured_summary_uses_packaged_runtime_and_preserves_unknown_fields(self):
        self.transcript()
        runtime = self.runtime()
        with self.installed(runtime):
            result = meeting_summary.summarize_meeting(self.store, self.sid, DEFAULT_SUMMARY_MODEL)
        self.assertEqual(result["revision"], self.revision)
        self.assertEqual(result["segment_ids"], ["s1"])
        self.assertIsNone(result["action_items"][0]["owner"])
        self.assertTrue(result["offline_verified"])
        self.assertEqual(result["runtime"], "llama.cpp")
        self.assertEqual(self.store.get(self.sid)["summary"], result)
        runtime.close.assert_called_once_with()
        self.assertFalse(runtime.generate.call_args.kwargs["disable_thinking"])

    def test_empty_transcript_never_opens_model_or_runtime(self):
        self.store.finish_revision(self.sid, self.revision)
        with mock.patch.object(meeting_summary, "summary_model_path") as path, \
                mock.patch.object(meeting_summary, "SummaryRuntime") as runtime:
            with self.assertRaisesRegex(ValueError, "texto"):
                meeting_summary.summarize_meeting(self.store, self.sid, DEFAULT_SUMMARY_MODEL)
        path.assert_not_called()
        runtime.assert_not_called()

    def test_unknown_or_missing_catalog_model_fails_before_runtime(self):
        self.transcript()
        with mock.patch.object(meeting_summary, "SummaryRuntime") as runtime:
            with self.assertRaisesRegex(ValueError, "catálogo"):
                meeting_summary.summarize_meeting(self.store, self.sid, "ollama:latest")
            with mock.patch.object(meeting_summary, "summary_model_path", return_value=None):
                with self.assertRaisesRegex(ValueError, "Baixe"):
                    meeting_summary.summarize_meeting(self.store, self.sid, DEFAULT_SUMMARY_MODEL)
        runtime.assert_not_called()

    def test_invented_references_and_owners_preserve_prior_summary(self):
        self.transcript()
        for modification in (
            {"segment_ids": ["invented"]},
            {"action_items": [{"text": "Task", "owner": "Invented Person",
                               "deadline": None, "segment_ids": ["s1"]}]},
        ):
            document = dict(self.result(), **modification)
            runtime = self.runtime(lambda *_args, **_kwargs: json.dumps(document))
            with self.subTest(modification=modification), self.installed(runtime):
                with self.assertRaises(ValueError):
                    meeting_summary.summarize_meeting(self.store, self.sid, DEFAULT_SUMMARY_MODEL)
            runtime.close.assert_called_once_with()
            self.assertEqual(self.store.get(self.sid)["summary"], {"summary": "Prior summary"})

    def test_cancel_during_generation_preserves_prior_and_closes_runtime(self):
        self.transcript()
        cancellation = threading.Event()

        def cancel(*_args, **_kwargs):
            cancellation.set()
            raise RuntimeError("O resumo foi cancelado; o resumo anterior foi preservado.")

        runtime = self.runtime(cancel)
        with self.installed(runtime), self.assertRaisesRegex(RuntimeError, "cancelado"):
            meeting_summary.summarize_meeting(
                self.store, self.sid, DEFAULT_SUMMARY_MODEL, cancellation,
            )
        runtime.close.assert_called_once_with()
        self.assertEqual(self.store.get(self.sid)["summary"], {"summary": "Prior summary"})

    def test_large_transcript_uses_bounded_hierarchical_reduction(self):
        self.transcript("Report evidence. " * 2000)
        payload_sizes = []

        def responder(_prompt, evidence, *_args, **_kwargs):
            payload_sizes.append(len(json.dumps(evidence, ensure_ascii=False).encode("utf-8")))
            identifier = evidence[0].get("id") or evidence[0]["segment_ids"][0]
            return json.dumps(self.result((identifier,)))

        runtime = self.runtime(responder)
        with self.installed(runtime):
            result = meeting_summary.summarize_meeting(self.store, self.sid, DEFAULT_SUMMARY_MODEL)
        self.assertGreater(result["chunks_processed"], 1)
        self.assertGreater(runtime.generate.call_count, result["chunks_processed"])
        self.assertLessEqual(max(payload_sizes), meeting_summary.MAX_CONTEXT - 1536)


if __name__ == "__main__":
    unittest.main()
