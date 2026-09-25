"""Isolated llama.cpp runtime for structured meeting summaries."""

import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time


MAX_OUTPUT_BYTES = 1024 * 1024
MAX_WIRE_BYTES = 8 * MAX_OUTPUT_BYTES


class NativeSummaryRuntime:
    def __init__(self, model_path, context_length=4096):
        try:
            from llama_cpp import Llama
        except (ImportError, OSError) as exc:
            raise RuntimeError(
                "O runtime llama.cpp incluído no SnipVoice não está disponível. Reinstale o aplicativo."
            ) from exc
        try:
            self._llama = Llama(model_path=model_path, n_ctx=context_length,
                                n_gpu_layers=-1, verbose=False)
        except Exception as exc:
            raise RuntimeError(f"Não foi possível abrir o modelo local de resumo: {exc}") from exc

    def generate(self, system_prompt, evidence, cancel_event=None, disable_thinking=False):
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("O resumo foi cancelado; o resumo anterior foi preservado.")
        prompt = system_prompt + ("\n/no_think" if disable_thinking else "")
        try:
            stream = self._llama.create_chat_completion(
                messages=[{"role": "system", "content": prompt},
                          {"role": "user", "content": json.dumps(evidence, ensure_ascii=False)}],
                response_format={"type": "json_object"}, temperature=0,
                max_tokens=512, stream=True,
            )
            chunks = []
            size = 0
            for item in stream:
                if cancel_event is not None and cancel_event.is_set():
                    raise RuntimeError("O resumo foi cancelado; o resumo anterior foi preservado.")
                choices = item.get("choices") if isinstance(item, dict) else None
                delta = choices[0].get("delta", {}) if isinstance(choices, list) and choices else {}
                text = delta.get("content", "") if isinstance(delta, dict) else ""
                if not isinstance(text, str):
                    raise ValueError("O runtime local retornou conteúdo inválido.")
                size += len(text.encode("utf-8"))
                if size > MAX_OUTPUT_BYTES:
                    raise ValueError("O runtime local retornou uma resposta maior que o limite permitido.")
                chunks.append(text)
            return "".join(chunks)
        except RuntimeError as exc:
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError(
                    "O resumo foi cancelado; o resumo anterior foi preservado."
                ) from exc
            raise RuntimeError(f"O llama.cpp não conseguiu gerar o resumo local: {exc}") from exc
        except Exception as exc:
            raise RuntimeError(f"O llama.cpp não conseguiu gerar o resumo local: {exc}") from exc

    def close(self):
        llama, self._llama = self._llama, None
        close = getattr(llama, "close", None)
        if callable(close):
            close()


def _spawn_worker():
    if getattr(sys, "frozen", False):
        command = [sys.executable, "--summary-worker"]
    else:
        command = [sys.executable, "-u", str(Path(__file__).with_name("summary_runtime_worker.py"))]
    options = {
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.DEVNULL,
        "text": True,
        "encoding": "utf-8",
        "bufsize": 1,
    }
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.Popen(command, **options)


class _WorkerClient:
    def __init__(self):
        try:
            self.process = _spawn_worker()
        except OSError as exc:
            raise RuntimeError("Não foi possível iniciar o runtime local de resumo.") from exc
        self.responses = queue.Queue()
        self.closed = False
        self.reader = threading.Thread(target=self._read, name="SummaryWorkerReader", daemon=True)
        self.reader.start()

    def _read(self):
        try:
            for line in self.process.stdout:
                if len(line.encode("utf-8")) > MAX_WIRE_BYTES:
                    self.responses.put(None)
                    return
                self.responses.put(line)
        except (OSError, ValueError):
            pass
        self.responses.put(None)

    def request(self, payload, *, cancel_event=None, timeout=120):
        if self.closed:
            raise RuntimeError("O runtime local de resumo foi encerrado.")
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("O resumo foi cancelado; o resultado anterior foi preservado.")
        try:
            line = json.dumps(payload, ensure_ascii=False, allow_nan=False)
            if len(line.encode("utf-8")) > MAX_OUTPUT_BYTES:
                raise ValueError("A solicitação de resumo excedeu o limite seguro.")
            self.process.stdin.write(line + "\n")
            self.process.stdin.flush()
        except (OSError, ValueError, TypeError) as exc:
            self.close()
            raise RuntimeError("Não foi possível enviar a solicitação ao runtime local de resumo.") from exc
        deadline = time.monotonic() + timeout
        while True:
            if cancel_event is not None and cancel_event.is_set():
                self.close(force=True)
                raise RuntimeError("O resumo foi cancelado; o resultado anterior foi preservado.")
            try:
                response = self.responses.get(timeout=0.1)
            except queue.Empty:
                if time.monotonic() >= deadline:
                    self.close(force=True)
                    raise RuntimeError("O runtime local de resumo demorou além do limite permitido.")
                continue
            if response is None:
                self.close(force=True)
                raise RuntimeError("O runtime local de resumo encerrou antes de responder.")
            try:
                value = json.loads(response)
            except ValueError as exc:
                self.close(force=True)
                raise RuntimeError("O runtime local de resumo retornou uma resposta inválida.") from exc
            if not isinstance(value, dict) or not isinstance(value.get("ok"), bool):
                self.close(force=True)
                raise RuntimeError("O runtime local de resumo retornou uma resposta inválida.")
            if not value["ok"]:
                raise RuntimeError(value.get("error") or "O runtime local de resumo falhou.")
            return value

    def close(self, force=False):
        if self.closed:
            return
        self.closed = True
        if not force and self.process.poll() is None:
            try:
                self.process.stdin.write('{"type":"close"}\n')
                self.process.stdin.flush()
                self.process.wait(timeout=5)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
        if self.process.poll() is None:
            try:
                self.process.terminate()
            except OSError:
                pass
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    self.process.kill()
                except OSError:
                    pass
                self.process.wait(timeout=2)
        self.process.stdin.close()
        self.process.stdout.close()


class SummaryRuntime:
    """Keep llama.cpp out of the process that loads transcribe.cpp's GGML DLLs."""

    def __init__(self, model_path, context_length=4096):
        self._worker = _WorkerClient()
        try:
            self._worker.request({
                "type": "open", "model_path": os.fspath(model_path),
                "context_length": context_length,
            })
        except Exception:
            self._worker.close(force=True)
            raise

    def generate(self, system_prompt, evidence, cancel_event=None, disable_thinking=False):
        # ceiling: one model request is capped at 60 minutes; extend only after
        # measuring a legitimate longer local generation on supported hardware.
        response = self._worker.request({
            "type": "generate", "prompt": system_prompt, "evidence": evidence,
            "disable_thinking": disable_thinking,
        }, cancel_event=cancel_event, timeout=3600)
        text = response.get("text")
        if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_OUTPUT_BYTES:
            raise RuntimeError("O runtime local de resumo retornou conteúdo inválido.")
        return text

    def close(self):
        self._worker.close()


def probe_summary_isolation():
    """Exercise the actual worker path after voice GGML has loaded in this process."""
    worker = _WorkerClient()
    try:
        worker.request({"type": "probe"})
    finally:
        worker.close()
