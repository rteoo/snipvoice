"""One local JSON-lines channel for the llama.cpp summary subprocess."""

import json
import io
import os
import sys

from summary_runtime import MAX_OUTPUT_BYTES, MAX_WIRE_BYTES, NativeSummaryRuntime


def _send(stream, response):
    line = json.dumps(response, ensure_ascii=False, allow_nan=False)
    if len(line.encode("utf-8")) > MAX_WIRE_BYTES:
        line = json.dumps({"ok": False, "error": "A resposta do runtime local excedeu o limite seguro."})
    stream.write(line + "\n")
    stream.flush()


def serve(input_stream, output_stream):
    runtime = None
    try:
        while True:
            line = input_stream.readline(MAX_OUTPUT_BYTES + 2)
            if not line:
                return
            if len(line.encode("utf-8")) > MAX_OUTPUT_BYTES or not line.endswith("\n"):
                _send(output_stream, {"ok": False, "error": "A solicitação de resumo excedeu o limite seguro."})
                return
            try:
                request = json.loads(line)
            except ValueError:
                _send(output_stream, {"ok": False, "error": "A solicitação de resumo é inválida."})
                return
            if not isinstance(request, dict):
                _send(output_stream, {"ok": False, "error": "A solicitação de resumo é inválida."})
                return
            kind = request.get("type")
            if kind == "close":
                return
            if kind == "probe" and runtime is None:
                try:
                    from llama_cpp import Llama
                    if not callable(Llama):
                        raise ImportError("Llama is unavailable")
                except (ImportError, OSError):
                    _send(output_stream, {"ok": False, "error": "O runtime local de resumo não está disponível."})
                    return
                _send(output_stream, {"ok": True})
                continue
            if kind == "open" and runtime is None:
                path = request.get("model_path")
                context = request.get("context_length")
                if (not isinstance(path, str) or not 0 < len(path) <= 4096
                        or isinstance(context, bool) or not isinstance(context, int)
                        or not 1 <= context <= 131072):
                    _send(output_stream, {"ok": False, "error": "O modelo local de resumo é inválido."})
                    return
                try:
                    runtime = NativeSummaryRuntime(path, context)
                except Exception:
                    _send(output_stream, {"ok": False, "error": "Não foi possível abrir o modelo local de resumo."})
                    return
                _send(output_stream, {"ok": True})
                continue
            if kind == "generate" and runtime is not None:
                prompt = request.get("prompt")
                evidence = request.get("evidence")
                disable = request.get("disable_thinking")
                if not isinstance(prompt, str) or not isinstance(evidence, list) or not isinstance(disable, bool):
                    _send(output_stream, {"ok": False, "error": "A solicitação de resumo é inválida."})
                    return
                try:
                    result = runtime.generate(prompt, evidence, disable_thinking=disable)
                    if len(result.encode("utf-8")) > MAX_OUTPUT_BYTES:
                        raise ValueError("output too large")
                except Exception:
                    _send(output_stream, {"ok": False, "error": "O runtime local não conseguiu gerar o resumo."})
                    return
                _send(output_stream, {"ok": True, "text": result})
                continue
            _send(output_stream, {"ok": False, "error": "A solicitação de resumo é inválida."})
            return
    finally:
        if runtime is not None:
            runtime.close()


def _inherited_stream(number, mode):
    """Recover redirected pipes hidden by a windowed Python/PyInstaller bootloader."""
    if os.name == "nt":
        import ctypes
        import msvcrt
        kernel = ctypes.windll.kernel32
        kernel.GetStdHandle.argtypes = [ctypes.c_uint32]
        kernel.GetStdHandle.restype = ctypes.c_void_p
        handle = kernel.GetStdHandle(0xFFFFFFF6 if number == 0 else 0xFFFFFFF5)
        if handle in (None, 0, ctypes.c_void_p(-1).value):
            return None
        descriptor = msvcrt.open_osfhandle(handle, os.O_BINARY | (os.O_RDONLY if number == 0 else os.O_WRONLY))
    else:
        descriptor = os.dup(number)
    binary = os.fdopen(descriptor, mode + "b", buffering=0)
    return io.TextIOWrapper(binary, encoding="utf-8", newline="\n", write_through=True)


def main():
    input_stream = sys.stdin or _inherited_stream(0, "r")
    output_stream = sys.stdout or _inherited_stream(1, "w")
    if input_stream is None or output_stream is None:
        return 1
    serve(input_stream, output_stream)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
