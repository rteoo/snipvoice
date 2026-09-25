"""In-process llama.cpp runtime for structured meeting summaries."""

import json


MAX_OUTPUT_BYTES = 1024 * 1024


class SummaryRuntime:
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
