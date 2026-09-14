import io
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
        self.store.add_transcript(self.sid, self.revision, {"id": "s1", "track": "microphone", "start": 0, "end": 30, "text": text})
        self.store.finish_revision(self.sid, self.revision)

    @staticmethod
    def model():
        return {"details": {"format": "gguf"}, "model_info": {"llama.context_length": 4096}, "capabilities": ["completion"]}

    @staticmethod
    def result(ids=("s1",)):
        return {"summary": "Revisão do relatório.", "segment_ids": list(ids), "decisions": [],
                "action_items": [{"text": "Revisar o relatório", "owner": None, "deadline": None, "segment_ids": list(ids)}]}

    def responder(self, path, payload, cancel_event=None):
        if path == "/api/show":
            return self.model()
        evidence = json.loads(payload["messages"][1]["content"])
        ids = [item["id"] for item in evidence] if "id" in evidence[0] else evidence[0]["segment_ids"]
        return {"done": True, "message": {"content": json.dumps(self.result((ids[0],)))}}

    def test_structured_summary_has_evidence_and_preserves_unknown_fields(self):
        self.transcript()
        with mock.patch.object(meeting_summary, "_request", side_effect=self.responder) as request:
            result = meeting_summary.summarize_meeting(self.store, self.sid, "qwen-local")
        self.assertEqual(result["revision"], self.revision)
        self.assertEqual(result["segment_ids"], ["s1"])
        self.assertIsNone(result["action_items"][0]["owner"])
        self.assertIsNone(result["action_items"][0]["deadline"])
        self.assertFalse(result["offline_verified"])
        self.assertEqual(self.store.get(self.sid)["summary"], result)
        self.assertEqual([call.args[0] for call in request.call_args_list], ["/api/show", "/api/chat"])
        chat = request.call_args_list[1].args[1]
        self.assertFalse(chat["stream"])
        self.assertNotIn("tools", chat)

    def test_empty_transcript_never_contacts_runtime(self):
        self.store.finish_revision(self.sid, self.revision)
        with mock.patch.object(meeting_summary, "_request") as request:
            with self.assertRaisesRegex(ValueError, "texto"):
                meeting_summary.summarize_meeting(self.store, self.sid, "local")
        request.assert_not_called()

    def test_cloud_variants_and_urls_fail_before_model_request(self):
        self.transcript()
        for model in ("gpt:120b-cloud", "gpt:cloud", "https://host/model", "cloud-alias", "", "invalid name"):
            with self.subTest(model=model), mock.patch.object(meeting_summary, "_request") as request:
                with self.assertRaises(ValueError):
                    meeting_summary.summarize_meeting(self.store, self.sid, model)
                request.assert_not_called()

    def test_model_metadata_remote_flags_fail_closed(self):
        self.transcript()
        for fields in ({"remote_host": "https://ollama.com"}, {"remote_model": "remote"}, {"capabilities": ["completion", "cloud"]}, {"model_info": {}}, {"details": {"format": "unknown"}}):
            shown = dict(self.model(), **fields)
            with self.subTest(fields=fields), mock.patch.object(meeting_summary, "_request", return_value=shown) as request:
                with self.assertRaises(ValueError):
                    meeting_summary.summarize_meeting(self.store, self.sid, "local")
                self.assertEqual(request.call_count, 1)
        self.assertEqual(self.store.get(self.sid)["summary"], {"summary": "Prior summary"})

    def test_invented_references_and_owners_preserve_prior_summary(self):
        self.transcript()
        for modification in ({"segment_ids": ["invented"]}, {"action_items": [{"text": "Task", "owner": "Invented Person", "deadline": None, "segment_ids": ["s1"]}]}):
            document = dict(self.result(), **modification)
            with self.subTest(modification=modification), mock.patch.object(meeting_summary, "_request", side_effect=[self.model(), {"done": True, "message": {"content": json.dumps(document)}}]):
                with self.assertRaises(ValueError):
                    meeting_summary.summarize_meeting(self.store, self.sid, "local")
            self.assertEqual(self.store.get(self.sid)["summary"], {"summary": "Prior summary"})

    def test_cancel_between_requests_preserves_prior(self):
        self.transcript()
        cancellation = threading.Event()
        def cancel_after_show(*_):
            cancellation.set()
            return self.model()
        with mock.patch.object(meeting_summary, "_request", side_effect=cancel_after_show) as request:
            with self.assertRaisesRegex(RuntimeError, "cancelado"):
                meeting_summary.summarize_meeting(self.store, self.sid, "local", cancellation)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(self.store.get(self.sid)["summary"], {"summary": "Prior summary"})

    def test_large_transcript_uses_bounded_hierarchical_reduction(self):
        self.transcript("Report evidence. " * 2000)
        with mock.patch.object(meeting_summary, "_request", side_effect=self.responder) as request:
            result = meeting_summary.summarize_meeting(self.store, self.sid, "local")
        self.assertGreater(result["chunks_processed"], 1)
        chat_calls = [call for call in request.call_args_list if call.args[0] == "/api/chat"]
        self.assertGreater(len(chat_calls), result["chunks_processed"])
        for call in chat_calls:
            self.assertLessEqual(len(call.args[1]["messages"][1]["content"].encode("utf-8")), 2560)

    def test_http_request_is_fixed_loopback_and_does_not_follow_redirects(self):
        response = mock.Mock(status=302)
        connection = mock.Mock()
        connection.getresponse.return_value = response
        with mock.patch.dict(os.environ, {"HTTP_PROXY": "http://external.invalid", "OLLAMA_HOST": "https://external.invalid"}), mock.patch.object(meeting_summary.http.client, "HTTPConnection", return_value=connection) as create:
            with self.assertRaisesRegex(RuntimeError, "redirecionar"):
                meeting_summary._request("/api/show", {"model": "local"})
        create.assert_called_once_with("127.0.0.1", 11434, timeout=30)
        self.assertEqual(connection.request.call_count, 1)
        self.assertEqual(connection.request.call_args.args[3], {"Content-Type": "application/json"})
        connection.close.assert_called_once()

    def test_http_response_size_and_invalid_json_are_bounded(self):
        for body, declared, expected in ((b"{}", str(meeting_summary.MAX_RESPONSE + 1), RuntimeError), (b"not JSON", None, RuntimeError), (b"x" * (meeting_summary.MAX_RESPONSE + 1), None, RuntimeError)):
            with self.subTest(length=len(body)):
                reader = io.BytesIO(body)
                response = mock.Mock(status=200)
                response.getheader.return_value = declared
                response.read1.side_effect = reader.read1
                connection = mock.Mock()
                connection.getresponse.return_value = response
                with mock.patch.object(meeting_summary.http.client, "HTTPConnection", return_value=connection):
                    with self.assertRaises(expected):
                        meeting_summary._request("/api/show", {"model": "local"})
                connection.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
