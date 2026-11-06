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


_FAKE_GGML = (
    "class _Function:\n"
    " def __init__(self, value): self.value = value\n"
    " def __call__(self, *args): return self.value(*args) if callable(self.value) else self.value\n"
    "class _Library: pass\n"
    "libggml = _Library()\n"
    "libggml.ggml_backend_dev_count = _Function(1)\n"
    "libggml.ggml_backend_dev_get = _Function(1)\n"
    "libggml.ggml_backend_dev_name = _Function(b'CPU')\n"
    "libggml.ggml_backend_dev_description = _Function(b'CPU')\n"
    "libggml.ggml_backend_dev_type = _Function(0)\n"
    "libggml.ggml_backend_dev_memory = _Function(None)\n"
    "libggml_base_path = '.'\n"
)
CPU_ONLY = [{"name": "CPU", "description": "CPU", "type": "cpu", "free": 0, "total": 0}]
WITH_GPU = CPU_ONLY + [{"name": "Vulkan0", "description": "Test GPU", "type": "gpu", "free": 8, "total": 8}]


def fake_llama_cpp(llama):
    return types.SimpleNamespace(Llama=llama, LLAMA_SPLIT_MODE_NONE=0)


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
    def setUp(self):
        backends = mock.patch("llama_runtime.load_backends", return_value=CPU_ONLY)
        backends.start()
        self.addCleanup(backends.stop)
        SummaryRuntime._gpu_failed = False
        self.addCleanup(setattr, SummaryRuntime, "_gpu_failed", False)

    def _fake_worker_package(self):
        root = Path(__file__).parent / "tmp"
        root.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=root)
        package = Path(temporary.name) / "llama_cpp"
        package.mkdir()
        (package / "_ggml.py").write_text(_FAKE_GGML, encoding="utf-8")
        (package / "_ctypes_extensions.py").write_text(
            "def load_shared_library(name, path):\n"
            " from llama_cpp._ggml import libggml\n"
            " return libggml\n",
            encoding="utf-8",
        )
        (package / "__init__.py").write_text(
            "import time\n"
            "LLAMA_SPLIT_MODE_NONE = 0\n"
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
        with mock.patch.dict(sys.modules, {"llama_cpp": fake_llama_cpp(FakeLlama)}):
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
        with mock.patch.dict(sys.modules, {"llama_cpp": fake_llama_cpp(FakeLlama)}):
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
        with mock.patch.dict(sys.modules, {"llama_cpp": fake_llama_cpp(FakeLlama)}):
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
        with mock.patch.dict(sys.modules, {"llama_cpp": fake_llama_cpp(FakeLlama)}):
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
        with mock.patch.dict(sys.modules, {"llama_cpp": fake_llama_cpp(FakeLlama)}):
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
        with mock.patch.dict(sys.modules, {"llama_cpp": fake_llama_cpp(FakeLlama)}):
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
        with mock.patch.dict(sys.modules, {"llama_cpp": fake_llama_cpp(FakeLlama)}):
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

        with mock.patch.dict(sys.modules, {"llama_cpp": fake_llama_cpp(LongReportLlama)}):
            runtime = NativeSummaryRuntime("local.gguf", 4096)
        try:
            self.assertEqual(json.loads(runtime.generate("system", [])), json.loads(document))
        finally:
            runtime.close()

    def test_token_limit_does_not_return_a_partial_report(self):
        with mock.patch.dict(sys.modules, {"llama_cpp": fake_llama_cpp(FakeLlama)}):
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
        with mock.patch.dict(sys.modules, {"llama_cpp": fake_llama_cpp(FakeLlama)}):
            serve(requests, responses)
        lines = [json.loads(line) for line in responses.getvalue().splitlines()]
        self.assertEqual(lines[0], {"ok": True, "device": "CPU"})
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
        with mock.patch.dict(sys.modules, {"llama_cpp": fake_llama_cpp(LimitedLlama)}):
            serve(requests, responses)
        lines = [json.loads(line) for line in responses.getvalue().splitlines()]
        self.assertEqual(lines[0], {"ok": True, "device": "CPU"})
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
        with mock.patch.dict(sys.modules, {"llama_cpp": fake_llama_cpp(RetryLlama)}):
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
        with mock.patch.dict(sys.modules, {"llama_cpp": fake_llama_cpp(ErrorLlama)}):
            serve(requests, responses)
        lines = [json.loads(line) for line in responses.getvalue().splitlines()]
        self.assertEqual([line.get("ok") for line in lines], [True, False])
        self.assertNotIn("error_code", lines[1])

    def test_native_byte_limit_uses_the_recoverable_error(self):
        class LargeLlama(FakeLlama):
            def create_chat_completion(self, **kwargs):
                return iter([{"choices": [{"delta": {"content": "x" * 101}}]}])
        with mock.patch.dict(sys.modules, {"llama_cpp": fake_llama_cpp(LargeLlama)}):
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

    def test_gpu_is_used_when_ggml_found_one(self):
        with mock.patch("llama_runtime.load_backends", return_value=WITH_GPU), \
                mock.patch.dict(sys.modules, {"llama_cpp": fake_llama_cpp(FakeLlama)}):
            runtime = NativeSummaryRuntime("local.gguf", 4096)
        self.assertEqual(runtime._llama.init["n_gpu_layers"], -1)
        self.assertNotIn("main_gpu", runtime._llama.init)
        self.assertEqual(runtime.device, "Test GPU")

    def test_gpu_that_cannot_open_the_model_falls_back_to_cpu(self):
        class GpuFailsLlama(FakeLlama):
            def __init__(self, **kwargs):
                if kwargs["n_gpu_layers"]:
                    raise ValueError("out of device memory")
                super().__init__(**kwargs)

        with mock.patch("llama_runtime.load_backends", return_value=WITH_GPU), \
                mock.patch.dict(sys.modules, {"llama_cpp": fake_llama_cpp(GpuFailsLlama)}):
            runtime = NativeSummaryRuntime("local.gguf", 4096)
        self.assertEqual((runtime._llama.init["n_gpu_layers"], runtime._llama.init["main_gpu"],
                          runtime._llama.init["split_mode"]), (0, -1, 0))
        self.assertEqual(runtime.device, "CPU")

    def test_cpu_mode_never_offloads(self):
        with mock.patch("llama_runtime.load_backends", return_value=WITH_GPU) as backends, \
                mock.patch.dict(sys.modules, {"llama_cpp": fake_llama_cpp(FakeLlama)}):
            runtime = NativeSummaryRuntime("local.gguf", 4096, device="cpu")
        # Registration still runs so llama.cpp never scans the working directory.
        backends.assert_called_once_with()
        self.assertEqual((runtime._llama.init["n_gpu_layers"], runtime._llama.init["main_gpu"]), (0, -1))

    def test_worker_that_dies_on_the_gpu_is_reopened_on_the_cpu_for_the_session(self):
        class Recording(io.StringIO):
            def close(self):
                self.recorded = self.getvalue()
                super().close()

        class FakeProcess:
            def __init__(self, output):
                self.stdin = Recording()
                self.stdout = io.StringIO(output)
                self.terminated = False
            def poll(self): return None if not self.terminated else 0
            def terminate(self): self.terminated = True
            def wait(self, timeout=None): return 0
            def kill(self): self.terminated = True

        spawned = []

        def spawn():
            # The first worker reports a GPU, then dies mid-generation.
            output = ('{"ok":true,"device":"Test GPU"}\n' if not spawned
                      else '{"ok":true,"device":"CPU"}\n{"ok":true,"text":"{\\"summary\\":\\"ok\\"}"}\n'
                      '{"ok":true,"device":"CPU"}\n')
            spawned.append(FakeProcess(output))
            return spawned[-1]

        runtime = SummaryRuntime("local.gguf", spawn_worker=spawn)
        try:
            self.assertEqual(runtime.device, "Test GPU")
            self.assertEqual(json.loads(runtime.generate("system", [])), {"summary": "ok"})
            self.assertEqual(runtime.device, "CPU")
        finally:
            runtime.close()
        opens = [json.loads(process.stdin.recorded.splitlines()[0]) for process in spawned]
        self.assertEqual([request["device"] for request in opens], ["auto", "cpu"])
        later = SummaryRuntime("local.gguf", spawn_worker=spawn)
        later.close()
        self.assertEqual(json.loads(spawned[-1].stdin.recorded.splitlines()[0])["device"], "cpu")


class LlamaBackendTests(unittest.TestCase):
    class FakeGgml:
        def __init__(self, registered, loadable):
            self.registered, self.loadable, self.loaded_from = registered, loadable, None
            fields = {
                "ggml_backend_dev_count": lambda: len(self.registered),
                "ggml_backend_load_all_from_path": self._load,
                "ggml_backend_dev_get": lambda index: index,
                "ggml_backend_dev_name": lambda index: self.registered[index][0].encode(),
                "ggml_backend_dev_description": lambda index: self.registered[index][0].encode(),
                "ggml_backend_dev_type": lambda index: self.registered[index][1],
                "ggml_backend_dev_memory": lambda index, free, total: None,
            }
            for name, function in fields.items():
                setattr(self, name, self._Function(function))

        class _Function:
            def __init__(self, function):
                self.function = function
            def __call__(self, *args):
                return self.function(*args)

        def _load(self, path):
            self.loaded_from = path
            self.registered = list(self.loadable)

    def test_static_build_is_used_as_is(self):
        from llama_runtime import load_backends
        ggml = self.FakeGgml([("CPU", 0)], [("Vulkan0", 1)])
        self.assertEqual([device["type"] for device in load_backends(ggml, "lib")], ["cpu"])
        self.assertIsNone(ggml.loaded_from)

    def test_dynamic_backends_load_from_the_package_library_folder(self):
        from llama_runtime import load_backends, offload_devices
        ggml = self.FakeGgml([], [("CPU", 0), ("Vulkan1", 2), ("Vulkan0", 1)])
        devices = load_backends(ggml, os.path.join("pkg", "lib"))
        self.assertEqual(ggml.loaded_from, os.fsencode(os.path.join("pkg", "lib")))
        self.assertEqual([device["name"] for device in offload_devices(devices)], ["Vulkan0"])
        self.assertEqual(offload_devices([device for device in devices if device["name"] != "Vulkan0"])[0]["type"],
                         "igpu")

    def test_missing_backends_fail_loudly(self):
        from llama_runtime import load_backends
        with self.assertRaisesRegex(RuntimeError, "no compute backend"):
            load_backends(self.FakeGgml([], []), "lib")


if __name__ == "__main__":
    unittest.main()
