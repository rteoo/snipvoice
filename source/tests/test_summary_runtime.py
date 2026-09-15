import json
import sys
import types
import unittest
from unittest import mock

from summary_runtime import SummaryRuntime


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
    def test_runtime_loads_local_gguf_and_streams_structured_json(self):
        with mock.patch.dict(sys.modules, {"llama_cpp": types.SimpleNamespace(Llama=FakeLlama)}):
            runtime = SummaryRuntime("local.gguf", 4096)
        raw = runtime.generate("system", [{"id": "s1"}], disable_thinking=True)
        self.assertEqual(json.loads(raw), {"summary": "ok"})
        self.assertEqual(runtime._llama.init["model_path"], "local.gguf")
        self.assertEqual(runtime._llama.call["response_format"], {"type": "json_object"})
        self.assertTrue(runtime._llama.call["stream"])
        self.assertIn("/no_think", runtime._llama.call["messages"][0]["content"])
        llama = runtime._llama
        runtime.close()
        self.assertTrue(llama.closed)

    def test_missing_packaged_runtime_is_actionable(self):
        with mock.patch.dict(sys.modules, {"llama_cpp": None}):
            with self.assertRaisesRegex(RuntimeError, "Reinstale"):
                SummaryRuntime("local.gguf")

    def test_native_runtime_error_is_wrapped_actionably(self):
        with mock.patch.dict(sys.modules, {"llama_cpp": types.SimpleNamespace(Llama=FakeLlama)}):
            runtime = SummaryRuntime("local.gguf")
        runtime._llama.create_chat_completion = mock.Mock(side_effect=RuntimeError("native error"))
        with self.assertRaisesRegex(RuntimeError, "llama.cpp"):
            runtime.generate("system", [])


if __name__ == "__main__":
    unittest.main()
