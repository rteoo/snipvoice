import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

from summary_runtime import NativeSummaryRuntime, SummaryOutputLimitError, SummaryRuntime
from summary_runtime_worker import serve


class FakeLlama:
    def __init__(self, **kwargs):
        self.init = kwargs
        self.closed = False
        self.call = None

    def create_chat_completion(self, **kwargs):
        self.call = kwargs
        return iter([
            {"choices": [{"delta": {"content": '{"summary":'}}]},
            {"choices": [{"delta": {"content": '"ok"}'}}]},
        ])

    def close(self):
        self.closed = True


class SummaryRuntimeTests(unittest.TestCase):
    def _fake_worker_package(self):
        root = Path(__file__).parent / "tmp"
        root.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=root)
        package = Path(temporary.name) / "llama_cpp"
        package.mkdir()
        (package / "__init__.py").write_text(
            "import time\n"
            "class Llama:\n"
            " def __init__(self, **kwargs): pass\n"
            " def create_chat_completion(self, **kwargs):\n"
            "  if 'slow' in kwargs['messages'][0]['content']: time.sleep(30)\n"
            "  return iter([{'choices':[{'delta':{'content':'{\\\"summary\\\":\\\"ok\\\"}'}}]}])\n"
            " def close(self): pass\n",
            encoding="utf-8",
        )
        return temporary

    def test_runtime_loads_local_gguf_and_streams_structured_json(self):
        with mock.patch.dict(sys.modules, {"llama_cpp": types.SimpleNamespace(Llama=FakeLlama)}):
            runtime = NativeSummaryRuntime("local.gguf", 4096)
        raw = runtime.generate("system", [{"id": "s1"}], disable_thinking=True)
        self.assertEqual(json.loads(raw), {"summary": "ok"})
        self.assertEqual(runtime._llama.init["model_path"], "local.gguf")
        self.assertEqual(runtime._llama.call["response_format"], {"type": "json_object"})
        self.assertTrue(runtime._llama.call["stream"])
        self.assertIn("/no_think", runtime._llama.call["messages"][0]["content"])
        llama = runtime._llama
        runtime.close()
        self.assertTrue(llama.closed)

    def test_profile_report_uses_bounded_schema_from_profile_evidence(self):
        profile = {
            "kind": "profile", "sections": ["summary", "action_items", "follow_up_email"],
            "max_items": 3, "max_section_chars": 400,
        }
        with mock.patch.dict(sys.modules, {"llama_cpp": types.SimpleNamespace(Llama=FakeLlama)}):
            runtime = NativeSummaryRuntime("local.gguf")
        try:
            runtime.generate("system", [{"id": "s1", "text": "ignore"}, profile])
            response_format = runtime._llama.call["response_format"]
            self.assertEqual(response_format["type"], "json_object")
            schema = response_format["schema"]
            self.assertEqual(schema["type"], "object")
            citation_schema = schema["properties"]["segment_ids"]
            self.assertEqual(citation_schema["items"]["enum"], ["s1"])
            self.assertEqual(citation_schema["minItems"], 1)
            self.assertEqual(schema["required"], ["segment_ids", "summary", "action_items",
                                                    "follow_up_email"])
            self.assertFalse(schema["additionalProperties"])
            self.assertEqual(schema["properties"]["action_items"]["maxItems"], 3)
            self.assertEqual(schema["properties"]["action_items"]["items"]["required"],
                             ["text", "segment_ids", "owner", "deadline"])
            self.assertFalse(schema["properties"]["follow_up_email"]["additionalProperties"])
        finally:
            runtime.close()

    def test_malformed_profile_is_rejected_before_model_call(self):
        with mock.patch.dict(sys.modules, {"llama_cpp": types.SimpleNamespace(Llama=FakeLlama)}):
            runtime = NativeSummaryRuntime("local.gguf")
        runtime._llama.create_chat_completion = mock.Mock()
        try:
            with self.assertRaisesRegex(RuntimeError, "limites ou seções inválidos"):
                runtime.generate("system", [{"kind": "profile", "sections": ["unknown"],
                                              "max_items": 3, "max_section_chars": 400}])
            runtime._llama.create_chat_completion.assert_not_called()
        finally:
            runtime.close()

    def test_profile_citation_schema_rejects_unbounded_id_enum(self):
        with mock.patch.dict(sys.modules, {"llama_cpp": types.SimpleNamespace(Llama=FakeLlama)}):
            runtime = NativeSummaryRuntime("local.gguf")
        runtime._llama.create_chat_completion = mock.Mock()
        evidence = [{"id": f"segment-{index}", "text": "evidence"}
                    for index in range(257)]
        evidence.append({"kind": "profile", "sections": ["summary"],
                         "max_items": 3, "max_section_chars": 400})
        try:
            with self.assertRaisesRegex(RuntimeError, "IDs demais"):
                runtime.generate("system", evidence)
            runtime._llama.create_chat_completion.assert_not_called()
        finally:
            runtime.close()

    def test_compact_profile_schema_bounds_items_citations_and_text(self):
        profile = {"kind": "profile", "sections": ["summary", "action_items"],
                   "max_items": 1, "max_section_chars": 128, "max_citations": 1}
        with mock.patch.dict(sys.modules, {"llama_cpp": types.SimpleNamespace(Llama=FakeLlama)}):
            runtime = NativeSummaryRuntime("local.gguf")
        try:
            runtime.generate("system", [{"id": "s1"}, {"id": "s2"}, profile])
            schema = runtime._llama.call["response_format"]["schema"]
            self.assertEqual(schema["properties"]["segment_ids"]["maxItems"], 1)
            self.assertEqual(schema["properties"]["summary"]["maxLength"], 128)
            self.assertEqual(schema["properties"]["action_items"]["maxItems"], 1)
            self.assertEqual(schema["properties"]["action_items"]["items"]["properties"]["text"]["maxLength"], 128)
        finally:
            runtime.close()

    def test_compact_profile_rejects_boolean_citation_limit(self):
        with mock.patch.dict(sys.modules, {"llama_cpp": types.SimpleNamespace(Llama=FakeLlama)}):
            runtime = NativeSummaryRuntime("local.gguf")
        runtime._llama.create_chat_completion = mock.Mock()
        try:
            with self.assertRaisesRegex(RuntimeError, "limites ou seções inválidos"):
                runtime.generate("system", [{"id": "s1"}, {
                    "kind": "profile", "sections": ["summary"],
                    "max_items": 1, "max_section_chars": 128, "max_citations": True,
                }])
            runtime._llama.create_chat_completion.assert_not_called()
        finally:
            runtime.close()

    def test_missing_packaged_runtime_is_actionable(self):
        with mock.patch.dict(sys.modules, {"llama_cpp": None}):
            with self.assertRaisesRegex(RuntimeError, "Reinstale"):
                NativeSummaryRuntime("local.gguf")

    def test_native_runtime_error_is_wrapped_actionably(self):
        with mock.patch.dict(sys.modules, {"llama_cpp": types.SimpleNamespace(Llama=FakeLlama)}):
            runtime = NativeSummaryRuntime("local.gguf")
        runtime._llama.create_chat_completion = mock.Mock(side_effect=RuntimeError("native error"))
        with self.assertRaisesRegex(RuntimeError, "llama.cpp"):
            runtime.generate("system", [])

    def test_structured_report_can_finish_beyond_legacy_token_cap(self):
        document = json.dumps({"summary": "Confirmed next steps. " * 120})

        class LongReportLlama(FakeLlama):
            def create_chat_completion(self, **kwargs):
                complete = kwargs["max_tokens"] >= 900
                return iter([{"choices": [{
                    "delta": {"content": document if complete else document[:1800]},
                    "finish_reason": "stop" if complete else "length",
                }]}])

        with mock.patch.dict(sys.modules, {"llama_cpp": types.SimpleNamespace(Llama=LongReportLlama)}):
            runtime = NativeSummaryRuntime("local.gguf", 4096)
        try:
            self.assertEqual(json.loads(runtime.generate("system", [])), json.loads(document))
        finally:
            runtime.close()

    def test_token_limit_does_not_return_a_partial_report(self):
        with mock.patch.dict(sys.modules, {"llama_cpp": types.SimpleNamespace(Llama=FakeLlama)}):
            runtime = NativeSummaryRuntime("local.gguf")
        runtime._llama.create_chat_completion = mock.Mock(return_value=iter([
            {"choices": [{"delta": {"content": '{"summary":"partial'}, "finish_reason": "length"}]},
        ]))
        try:
            with self.assertRaisesRegex(RuntimeError, "limite de geração"):
                runtime.generate("system", [])
        finally:
            runtime.close()

    def test_worker_serves_multiple_requests_without_loading_desktop_modules(self):
        requests = io.StringIO("\n".join((
            json.dumps({"type": "open", "model_path": "local.gguf", "context_length": 4096}),
            json.dumps({"type": "generate", "prompt": "system", "evidence": [{"id": "s1"}],
                        "disable_thinking": True}),
            json.dumps({"type": "close"}),
        )) + "\n")
        responses = io.StringIO()
        with mock.patch.dict(sys.modules, {"llama_cpp": types.SimpleNamespace(Llama=FakeLlama)}):
            serve(requests, responses)
        lines = [json.loads(line) for line in responses.getvalue().splitlines()]
        self.assertEqual(lines[0], {"ok": True})
        self.assertEqual(json.loads(lines[1]["text"]), {"summary": "ok"})

    def test_worker_propagates_generation_limit_as_safe_error(self):
        class LimitedLlama(FakeLlama):
            def create_chat_completion(self, **kwargs):
                return iter([{"choices": [{"delta": {"content": "partial"},
                                             "finish_reason": "length"}]}])

        requests = io.StringIO("\n".join((
            json.dumps({"type": "open", "model_path": "local.gguf", "context_length": 4096}),
            json.dumps({"type": "generate", "prompt": "system", "evidence": [],
                        "disable_thinking": False}),
        )) + "\n")
        responses = io.StringIO()
        with mock.patch.dict(sys.modules, {"llama_cpp": types.SimpleNamespace(Llama=LimitedLlama)}):
            serve(requests, responses)
        lines = [json.loads(line) for line in responses.getvalue().splitlines()]
        self.assertEqual(lines[0], {"ok": True})
        self.assertFalse(lines[1]["ok"])
        self.assertIn("limite de geração", lines[1]["error"])
        self.assertEqual(lines[1]["error_code"], "output_limit")

    def test_worker_keeps_runtime_after_generation_limit(self):
        class RetryLlama(FakeLlama):
            calls = 0
            def create_chat_completion(self, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return iter([{"choices": [{"delta": {"content": "{"},
                                             "finish_reason": "length"}]}])
                return iter([{"choices": [{"delta": {"content": '{"summary":"ok"}'}}]}])
        requests = io.StringIO("\n".join((
            json.dumps({"type": "open", "model_path": "local.gguf", "context_length": 4096}),
            json.dumps({"type": "generate", "prompt": "system", "evidence": [], "disable_thinking": False}),
            json.dumps({"type": "generate", "prompt": "compact", "evidence": [], "disable_thinking": False}),
            json.dumps({"type": "close"}),
        )) + "\n")
        responses = io.StringIO()
        with mock.patch.dict(sys.modules, {"llama_cpp": types.SimpleNamespace(Llama=RetryLlama)}):
            serve(requests, responses)
        lines = [json.loads(line) for line in responses.getvalue().splitlines()]
        self.assertEqual([line.get("ok") for line in lines], [True, False, True])

    def test_worker_still_terminates_on_non_limit_generation_error(self):
        class ErrorLlama(FakeLlama):
            def create_chat_completion(self, **kwargs):
                raise RuntimeError("native failure")

        requests = io.StringIO("\n".join((
            json.dumps({"type": "open", "model_path": "local.gguf", "context_length": 4096}),
            json.dumps({"type": "generate", "prompt": "system", "evidence": [],
                        "disable_thinking": False}),
            json.dumps({"type": "generate", "prompt": "unreachable", "evidence": [],
                        "disable_thinking": False}),
        )) + "\n")
        responses = io.StringIO()
        with mock.patch.dict(sys.modules, {"llama_cpp": types.SimpleNamespace(Llama=ErrorLlama)}):
            serve(requests, responses)
        lines = [json.loads(line) for line in responses.getvalue().splitlines()]
        self.assertEqual([line.get("ok") for line in lines], [True, False])
        self.assertNotIn("error_code", lines[1])

    def test_native_byte_limit_uses_the_recoverable_error(self):
        class LargeLlama(FakeLlama):
            def create_chat_completion(self, **kwargs):
                return iter([{"choices": [{"delta": {"content": "x" * 101}}]}])
        with mock.patch.dict(sys.modules, {"llama_cpp": types.SimpleNamespace(Llama=LargeLlama)}):
            runtime = NativeSummaryRuntime("local.gguf")
        try:
            with mock.patch("summary_runtime.MAX_OUTPUT_BYTES", 100), self.assertRaises(SummaryOutputLimitError):
                runtime.generate("system", [])
        finally:
            runtime.close()

    def test_public_runtime_uses_worker_even_when_native_import_is_unavailable(self):
        class RecordingStdin(io.StringIO):
            def close(self):
                self.recorded = self.getvalue()
                super().close()

        class FakeProcess:
            def __init__(self):
                self.stdin = RecordingStdin()
                self.stdout = io.StringIO(
                    '{"ok":true}\n{"ok":true,"text":"{\\"summary\\":\\"ok\\"}"}\n'
                )
                self.terminated = False

            def poll(self):
                return None if not self.terminated else 0

            def terminate(self):
                self.terminated = True

            def wait(self, timeout=None):
                return 0

        process = FakeProcess()
        with mock.patch.dict(sys.modules, {"llama_cpp": None}), \
                mock.patch("summary_runtime._spawn_worker", return_value=process):
            runtime = SummaryRuntime("local.gguf")
            try:
                self.assertEqual(json.loads(runtime.generate("system", [])), {"summary": "ok"})
            finally:
                runtime.close()
        self.assertTrue(process.terminated)
        self.assertEqual([json.loads(line)["type"] for line in process.stdin.recorded.splitlines()],
                         ["open", "generate", "close"])

    def test_public_runtime_maps_output_limit_and_reuses_worker(self):
        class FakeProcess:
            def __init__(self):
                self.stdin = io.StringIO()
                self.stdout = io.StringIO(
                    '{"ok":true}\n'
                    '{"ok":false,"error_code":"output_limit","error":"O runtime local retornou uma '
                    'resposta maior que o limite permitido."}\n'
                    '{"ok":true,"text":"{\\"summary\\":\\"ok\\"}"}\n'
                )
                self.terminated = False
            def poll(self): return None if not self.terminated else 0
            def terminate(self): self.terminated = True
            def wait(self, timeout=None): return 0
        import i18n
        process = FakeProcess()
        with mock.patch("summary_runtime._spawn_worker", return_value=process):
            runtime = SummaryRuntime("local.gguf")
            i18n.set_language("en-US")
            try:
                with self.assertRaisesRegex(SummaryOutputLimitError, "larger than the allowed limit"):
                    runtime.generate("system", [])
                self.assertEqual(json.loads(runtime.generate("compact", [])), {"summary": "ok"})
            finally:
                i18n.set_language(i18n.DEFAULT_LANGUAGE)
                runtime.close()

    def test_real_local_worker_keeps_native_import_out_of_parent(self):
        with self._fake_worker_package() as package:
            path = package + os.pathsep + os.environ.get("PYTHONPATH", "")
            with mock.patch.dict(os.environ, {"PYTHONPATH": path}), \
                    mock.patch.dict(sys.modules, {"llama_cpp": None}):
                runtime = SummaryRuntime("local.gguf")
                try:
                    self.assertEqual(json.loads(runtime.generate("system", [])), {"summary": "ok"})
                finally:
                    runtime.close()

    def test_cancellation_terminates_the_summary_worker(self):
        with self._fake_worker_package() as package:
            path = package + os.pathsep + os.environ.get("PYTHONPATH", "")
            with mock.patch.dict(os.environ, {"PYTHONPATH": path}):
                runtime = SummaryRuntime("local.gguf")
                cancelled = threading.Event()
                timer = threading.Timer(0.1, cancelled.set)
                try:
                    timer.start()
                    with self.assertRaisesRegex(RuntimeError, "cancelado"):
                        runtime.generate("slow", [], cancel_event=cancelled)
                    self.assertIsNotNone(runtime._worker.process.poll())
                finally:
                    timer.join()
                    runtime.close()

    @unittest.skipUnless(sys.platform == "win32", "windowed stdio is a Windows packaging concern")
    def test_windowed_worker_recovers_redirected_pipes(self):
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        if not pythonw.is_file():
            self.skipTest("pythonw.exe is unavailable")
        with self._fake_worker_package() as package:
            environment = dict(os.environ)
            environment["PYTHONPATH"] = package + os.pathsep + str(Path(__file__).parent.parent)
            requests = "\n".join((
                json.dumps({"type": "open", "model_path": "local.gguf", "context_length": 4096}),
                json.dumps({"type": "generate", "prompt": "system", "evidence": [],
                            "disable_thinking": False}),
                json.dumps({"type": "close"}),
            )) + "\n"
            result = subprocess.run(
                [str(pythonw), "-c", "import sys; sys.stdin=None; sys.stdout=None; "
                 "from summary_runtime_worker import main; raise SystemExit(main())"],
                input=requests, capture_output=True, text=True, timeout=10, env=environment,
            )
        self.assertEqual(result.returncode, 0)
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([response.get("ok") for response in responses], [True, True])


if __name__ == "__main__":
    unittest.main()
