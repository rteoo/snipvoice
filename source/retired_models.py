"""Files left behind by models and features SnipVoice no longer ships.

Settings list what is still on disk so a person can reclaim the space. Nothing
here runs on its own: every removal is an explicit, confirmed action. A target
that is itself a link or junction is neither listed nor removed, so removal
never reaches outside the folder that owns it.
"""

import glob
import os

import app_paths
from i18n import N_, tr
from voice_models import delete_model

# Catalog directories are named after the entry id (``voice_models.model_dir``).
RETIRED_VOICE = (
    ("whisper-large-v3-turbo-q8", "Whisper Large v3 Turbo"),
    ("qwen3-asr-0.6b-q8", "Qwen3-ASR 0.6B"),
    ("qwen3-asr-1.7b-q8", "Qwen3-ASR 1.7B"),
)
RETIRED_SUMMARY = (
    ("qwen3.5-0.8b-q4", "Qwen3.5 0.8B"),
    ("lfm2.5-2.6b-q4", "LFM2.5 2.6B"),
    ("qwen3-1.7b-q4", "Qwen3 1.7B"),
    ("granite-3.3-2b-q4", "Granite 3.3 2B"),
    ("granite-4.0-1b-q4", "Granite 4.0 1B"),
    ("granite-4.2-3b-q4", "Granite 4.2 3B"),
    ("gemma-3-1b-q4", "Gemma 3 1B"),
)
EMBEDDING_FOLDER = "embedding-models"
EMBEDDING_ID = "embeddinggemma-2-bf16"
SEMANTIC_INDEX = "semantic.sqlite"
SEMANTIC_INDEX_ID = "semantic-index"
# The removed semantic index wrote these beside the database.
_SEMANTIC_SUFFIXES = ("", "-wal", "-shm", "-journal", ".lock")


def _is_link(path):
    return os.path.islink(path) or getattr(os.path, "isjunction", lambda _path: False)(path)


def _size(path):
    if os.path.isfile(path):
        return os.path.getsize(path)
    total = 0
    for folder, folders, files in os.walk(path):
        folders[:] = [name for name in folders if not _is_link(os.path.join(folder, name))]
        total += sum(os.path.getsize(os.path.join(folder, name)) for name in files
                     if not _is_link(os.path.join(folder, name)))
    return total


def _semantic_files(data_root):
    names = [os.path.join(data_root, SEMANTIC_INDEX + suffix) for suffix in _SEMANTIC_SUFFIXES]
    names += glob.glob(os.path.join(glob.escape(data_root), SEMANTIC_INDEX + ".*.tmp"))
    return [path for path in names if os.path.lexists(path)]


class Leftovers:
    """``voice_cache``/``summary_cache``/``data_root`` are folders; the embedding
    cache lives under the configured models folder unless ``models_root`` is given."""

    def __init__(self, *, voice_cache, summary_cache, data_root, models_root=None):
        embedding_root = os.path.join(models_root or app_paths.configured_models_dir(), EMBEDDING_FOLDER)
        self._models = {}
        for prefix, cache, entries in (("voice", voice_cache, RETIRED_VOICE),
                                       ("summary", summary_cache, RETIRED_SUMMARY),
                                       ("embedding", embedding_root, ((EMBEDDING_ID, "EmbeddingGemma 2"),))):
            for model_id, name in entries:
                self._models[f"{prefix}:{model_id}"] = (os.path.abspath(cache), model_id, name)
        self._embedding_root = os.path.abspath(embedding_root)
        self._data_root = os.path.abspath(data_root)

    def _model_target(self, key):
        cache, model_id, _name = self._models[key]
        return os.path.join(cache, model_id)

    def find(self):
        """``[{id, name, detail, size_bytes}]`` for each leftover still on disk."""
        found = []
        for key, (_cache, _model_id, name) in self._models.items():
            target = self._model_target(key)
            if os.path.isdir(target) and not _is_link(target):
                detail = (N_("Modelo da busca semântica removida") if key.startswith("embedding:")
                          else N_("Modelo que o SnipVoice não oferece mais"))
                found.append({"id": key, "name": name, "detail": tr(detail), "size_bytes": _size(target)})
        files = [path for path in _semantic_files(self._data_root) if not _is_link(path)]
        if files:
            found.append({"id": SEMANTIC_INDEX_ID, "name": tr("Índice da busca semântica"),
                          "detail": tr("Trechos das transcrições guardados pela busca semântica removida"),
                          "size_bytes": sum(os.path.getsize(path) for path in files)})
        return found

    def name(self, key):
        if key == SEMANTIC_INDEX_ID:
            return tr("Índice da busca semântica")
        if key in self._models:
            return self._models[key][2]
        return None

    def remove(self, key):
        """Delete one leftover; raise ``ValueError`` for an unknown id or a linked target."""
        if key == SEMANTIC_INDEX_ID:
            files = _semantic_files(self._data_root)
            if any(_is_link(path) for path in files):
                raise ValueError(tr("Recusa em apagar um link ou junction."))
            for path in files:
                os.remove(path)
            return
        if key not in self._models:
            raise ValueError(tr("Pedido inválido."))
        cache, model_id, _name = self._models[key]
        if _is_link(self._model_target(key)):
            raise ValueError(tr("Recusa em apagar um link ou junction."))
        delete_model({"id": model_id}, cache)
        if key.startswith("embedding:") and os.path.isdir(self._embedding_root) \
                and not _is_link(self._embedding_root) and not os.listdir(self._embedding_root):
            os.rmdir(self._embedding_root)
