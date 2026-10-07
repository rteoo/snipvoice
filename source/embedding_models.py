"""Explicit, hash-verified installation of the text-only embedding GGUF."""

import os

import app_paths
from i18n import tr
from meeting_index import _has_link_component
from voice_models import download_model, installed_model_path


MODEL_REVISION = "ba3888272494be64ed88c9eb536ddc61a1be73d5"
MODEL = {
    "id": "embeddinggemma-2-bf16", "profile": "embeddinggemma-2-bf16",
    "name": "EmbeddingGemma 2 (text, BF16)",
    "filename": "embeddinggemma-2-BF16.gguf",
    "url": "https://huggingface.co/unsloth/embeddinggemma-2-GGUF/resolve/"
           + MODEL_REVISION + "/embeddinggemma-2-BF16.gguf",
    "sha256": "f315cbbb30dd487e44d501c8902abe88808755e43753a96beed1964f0a48aa4f",
    "size_bytes": 557950240,
    "license_id": "Apache-2.0", "upstream_model": "google/embeddinggemma-2",
}


def embedding_cache_dir():
    return os.path.join(app_paths.configured_models_dir(), "embedding-models")


def _safe_cache(cache_dir=None):
    path = os.path.abspath(os.fspath(cache_dir or embedding_cache_dir()))
    if any(_has_link_component(target) for target in (
        path, os.path.join(path, MODEL["id"]),
        os.path.join(path, MODEL["id"], MODEL["filename"]),
        os.path.join(path, MODEL["id"], MODEL["filename"] + ".partial"),
        os.path.join(path, MODEL["id"], "manifest.json"),
    )):
        raise ValueError(tr("O cache de embeddings não pode conter links ou junctions."))
    return path


def embedding_model_path(cache_dir=None):
    return installed_model_path(MODEL, _safe_cache(cache_dir))


def download_embedding_model(*, cache_dir=None, progress=None, cancel_event=None, opener=None):
    """Called only by an explicit install action; automatic work never downloads."""
    return download_model(MODEL, _safe_cache(cache_dir), progress=progress,
                          cancel_event=cancel_event, opener=opener)
