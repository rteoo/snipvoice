"""Private JSON-lines embedding worker; never logs transcript or native errors."""

import json

from embedding_runtime import DIMENSION, MAX_BATCH, MAX_TEXT_CHARS, normalize_vector
from i18n import N_
from summary_runtime_worker import _inherited_stream, _send


def _open_model(path):
    from llama_cpp import Llama, LLAMA_POOLING_TYPE_MEAN, llama_model_n_embd_out

    class ProjectedEmbeddings(Llama):
        def n_embd(self):
            # The text encoder has 512 hidden channels and a 768-dimensional
            # output projection. Llama.embed uses this method to size its
            # result; the generic wrapper otherwise reads only hidden channels.
            return llama_model_n_embd_out(self._model.model)

    # BF16 GGUF, text encoder only; no multimodal projector.
    # ceiling: 8192 tokens per input; reject overflow instead of truncating
    # authoritative transcript passages.
    return ProjectedEmbeddings(model_path=path, embedding=True, pooling_type=LLAMA_POOLING_TYPE_MEAN,
                               n_ctx=8192, n_batch=8192, n_ubatch=8192,
                               n_threads=2, n_threads_batch=2, n_gpu_layers=0, verbose=False)


def serve(input_stream, output_stream):
    model = None
    try:
        while True:
            line = input_stream.readline(640_002)
            if not line:
                return
            try:
                if len(line.encode("utf-8")) > 640_000 or not line.endswith("\n"):
                    raise ValueError("wire limit")
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise ValueError("invalid request")
                kind = request.get("type")
                if kind == "close":
                    return
                if kind == "open" and model is None:
                    path = request.get("model_path")
                    if not isinstance(path, str) or not 0 < len(path) <= 4096:
                        raise ValueError("model path")
                    model = _open_model(path)
                    if model.n_embd() != DIMENSION:
                        raise ValueError("embedding projection incompatible")
                    _send(output_stream, {"ok": True})
                    continue
                if kind == "embed" and model is not None:
                    texts = request.get("texts")
                    if (not isinstance(texts, list) or not 1 <= len(texts) <= MAX_BATCH
                            or any(not isinstance(text, str) or not text
                                   or len(text) > MAX_TEXT_CHARS + 560 for text in texts)):
                        raise ValueError("invalid batch")
                    vectors = [normalize_vector(value) for value in model.embed(
                        texts, normalize=False, truncate=False)]
                    _send(output_stream, {"ok": True, "vectors": vectors})
                    continue
                raise ValueError("unsupported request")
            except Exception:
                _send(output_stream, {"ok": False, "error": N_("O runtime local de embeddings está indisponível ou recebeu dados inválidos.")})
                return
    finally:
        if model is not None:
            model.close()


def main():
    import sys
    input_stream = sys.stdin or _inherited_stream(0, "r")
    output_stream = sys.stdout or _inherited_stream(1, "w")
    if input_stream is None or output_stream is None:
        return 1
    serve(input_stream, output_stream)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
