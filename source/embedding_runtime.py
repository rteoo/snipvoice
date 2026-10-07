"""Bounded local embeddings in an isolated, cancellable native process."""

import math
import os
from pathlib import Path
import subprocess
import sys
import threading

from embedding_models import MODEL_REVISION, embedding_model_path
from i18n import tr
from meeting_index import IndexCancelled
from summary_runtime import _WorkerClient


DIMENSION = 768
PREPROCESSING_VERSION = "search-document-v1"
MAX_TEXT_CHARS = 8000
MAX_BATCH = 16


def normalize_vector(value):
    if not isinstance(value, (list, tuple)) or len(value) != DIMENSION:
        raise ValueError(tr("O vetor de embeddings deve ter 768 dimensões."))
    if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in value):
        raise ValueError(tr("O vetor de embeddings contém valores inválidos."))
    try:
        numeric = [float(item) for item in value]
    except OverflowError as error:
        raise ValueError(tr("O vetor de embeddings contém valores inválidos.")) from error
    if any(not math.isfinite(item) for item in numeric):
        raise ValueError(tr("O vetor de embeddings contém valores inválidos."))
    norm = math.hypot(*numeric)
    if not math.isfinite(norm) or norm == 0:
        raise ValueError(tr("O vetor de embeddings não pode ser normalizado."))
    return [item / norm for item in numeric]


def check_cancelled(event):
    if event is not None and event.is_set():
        raise IndexCancelled(tr("A busca semântica foi cancelada."))


def _text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_TEXT_CHARS:
        raise ValueError(tr("O texto de embeddings está vazio ou excede o limite de 8000 caracteres."))
    return value


def query_text(query):
    return "task: search result | query: " + _text(query)


def document_text(text, title=""):
    if not isinstance(title, str) or len(title) > 512:
        raise ValueError(tr("O título de embeddings é inválido."))
    return f"title: {title or 'none'} | text: {_text(text)}"


def _spawn_worker():
    frozen = getattr(sys, "frozen", False)
    executable = sys.executable
    options = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                   text=True, encoding="utf-8", bufsize=1)
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
        if not frozen and sys.prefix != sys.base_prefix:
            # CPython's Windows venv launcher creates another process. Launch
            # the interpreter directly (as multiprocessing does) so cancellation
            # and RSS measurement target native inference, preserving the venv.
            executable = sys._base_executable
            options["env"] = {**os.environ, "__PYVENV_LAUNCHER__": sys.executable}
    command = ([executable, "--embedding-worker"] if frozen
               else [executable, "-u", str(Path(__file__).with_name("embedding_runtime_worker.py"))])
    return subprocess.Popen(command, **options)


class EmbeddingRuntime:
    model_revision = MODEL_REVISION

    def __init__(self, *, cancel_event=None, cache_dir=None):
        check_cancelled(cancel_event)
        path = embedding_model_path(cache_dir)
        if path is None:
            raise RuntimeError(tr("Instale o EmbeddingGemma 2 para usar a busca semântica. A busca por palavras continua disponível."))
        self._lock = threading.RLock()
        self._worker = _WorkerClient(spawn_worker=_spawn_worker)
        try:
            self._request({"type": "open", "model_path": path}, cancel_event)
        except Exception:
            self._worker.close(force=True)
            raise

    def _request(self, payload, cancel_event=None):
        try:
            # ceiling: 120 seconds per native batch including load; increase
            # only after measuring legitimate work on representative hardware.
            return self._worker.request(payload, cancel_event=cancel_event, timeout=120)
        except RuntimeError as error:
            check_cancelled(cancel_event)
            raise RuntimeError(tr("O runtime local de embeddings falhou ou não suporta este modelo. A busca por palavras continua disponível.")) from error

    def _embed(self, texts, cancel_event=None):
        if not 1 <= len(texts) <= MAX_BATCH:
            raise ValueError(tr("O lote de embeddings é inválido."))
        with self._lock:
            check_cancelled(cancel_event)
            response = self._request({"type": "embed", "texts": texts}, cancel_event)
            values = response.get("vectors")
            if not isinstance(values, list) or len(values) != len(texts):
                raise ValueError(tr("O runtime de embeddings retornou um lote inválido."))
            return [normalize_vector(value) for value in values]

    def embed_query(self, query, cancel_event=None):
        return self._embed([query_text(query)], cancel_event)[0]

    def embed_documents(self, records, cancel_event=None):
        return self._embed([document_text(item["text"], item.get("title", ""))
                            for item in records], cancel_event)

    def close(self):
        with self._lock:
            self._worker.close(force=True)
