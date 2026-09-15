"""Pinned llama.cpp models for local meeting summaries."""


DEFAULT_SUMMARY_MODEL = "qwen3-1.7b-q4"

SUMMARY_CATALOG = (
    {
        "id": DEFAULT_SUMMARY_MODEL,
        "profile": DEFAULT_SUMMARY_MODEL,
        "name": "Qwen3 1.7B",
        "description": "Recomendado · melhor equilíbrio para português e inglês",
        "filename": "Qwen3-1.7B-Q4_K_M.gguf",
        "url": "https://huggingface.co/ggml-org/Qwen3-1.7B-GGUF/resolve/main/Qwen3-1.7B-Q4_K_M.gguf",
        "sha256": "d2387ca2dbfee2ffabce7120d3770dadca0b293052bc2f0e138fdc940d9bc7b5",
        "size_bytes": 1_282_439_264,
        "parameters": "1.7B",
        "context_length": 4096,
        "license_id": "Apache-2.0",
        "license_url": "https://huggingface.co/Qwen/Qwen3-1.7B/blob/main/LICENSE",
        "upstream_model": "Qwen/Qwen3-1.7B",
        "requires_acceptance": False,
        "disable_thinking": True,
    },
    {
        "id": "granite-3.3-2b-q4",
        "profile": "granite-3.3-2b-q4",
        "name": "IBM Granite 3.3 2B",
        "description": "Especializado em documentos e resumos de reuniões",
        "filename": "granite-3.3-2b-instruct-Q4_K_M.gguf",
        "url": "https://huggingface.co/ibm-granite/granite-3.3-2b-instruct-GGUF/resolve/main/granite-3.3-2b-instruct-Q4_K_M.gguf",
        "sha256": "ac71e9e32c0bea919b409c5918f69ca74339854b0319c5065e4e9fb6d95c4852",
        "size_bytes": 1_545_303_328,
        "parameters": "2B",
        "context_length": 4096,
        "license_id": "Apache-2.0",
        "license_url": "https://huggingface.co/ibm-granite/granite-3.3-2b-instruct/blob/main/LICENSE",
        "upstream_model": "ibm-granite/granite-3.3-2b-instruct",
        "requires_acceptance": False,
        "disable_thinking": False,
    },
    {
        "id": "gemma-3-1b-q4",
        "profile": "gemma-3-1b-q4",
        "name": "Gemma 3 1B",
        "description": "Mais leve · qualidade em português deve ser avaliada",
        "filename": "gemma-3-1b-it-Q4_K_M.gguf",
        "url": "https://huggingface.co/ggml-org/gemma-3-1b-it-GGUF/resolve/main/gemma-3-1b-it-Q4_K_M.gguf",
        "sha256": "8ccc5cd1f1b3602548715ae25a66ed73fd5dc68a210412eea643eb20eb75a135",
        "size_bytes": 806_058_240,
        "parameters": "1B",
        "context_length": 4096,
        "license_id": "Gemma Terms of Use",
        "license_url": "https://ai.google.dev/gemma/terms",
        "upstream_model": "google/gemma-3-1b-it",
        "requires_acceptance": True,
        "disable_thinking": False,
    },
)


def summary_catalog():
    return tuple(dict(entry) for entry in SUMMARY_CATALOG)


def summary_catalog_entry(model_id):
    return next((dict(entry) for entry in SUMMARY_CATALOG if entry["id"] == model_id), None)


def is_known_summary_model(model_id):
    return summary_catalog_entry(model_id) is not None


def format_model_size(size_bytes):
    return f"{size_bytes / (1024 ** 3):.2f} GB"
