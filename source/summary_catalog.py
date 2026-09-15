"""Pinned llama.cpp models for local meeting summaries."""


DEFAULT_SUMMARY_MODEL = "qwen3.5-2b-q4"

SUMMARY_CATALOG = (
    {
        "id": DEFAULT_SUMMARY_MODEL,
        "profile": DEFAULT_SUMMARY_MODEL,
        "name": "Qwen3.5 2B",
        "description": "Recomendado · melhor equilíbrio para português, inglês e uso local",
        "filename": "Qwen3.5-2B-Q4_K_M.gguf",
        "url": (
            "https://huggingface.co/lmstudio-community/Qwen3.5-2B-GGUF/resolve/"
            "bb84e11355a036e28f080c7793fa6d22b7c4e344/Qwen3.5-2B-Q4_K_M.gguf"
        ),
        "sha256": "0bfe35afc9f05b7fac3fa04925e051ac7939a42a8a17ea11afc99701bea826cc",
        "size_bytes": 1_270_808_032,
        "parameters": "2B",
        "context_length": 4096,
        "license_id": "Apache-2.0",
        "license_url": "https://huggingface.co/Qwen/Qwen3.5-2B/blob/main/LICENSE",
        "upstream_model": "Qwen/Qwen3.5-2B",
        "quant_source": "lmstudio-community/Qwen3.5-2B-GGUF",
        "requires_acceptance": False,
        "disable_thinking": False,
    },
    {
        "id": "qwen3.5-4b-q4",
        "profile": "qwen3.5-4b-q4",
        "name": "Qwen3.5 4B",
        "description": "Mais qualidade · maior uso de memória e processamento",
        "filename": "Qwen3.5-4B-Q4_K_M.gguf",
        "url": (
            "https://huggingface.co/lmstudio-community/Qwen3.5-4B-GGUF/resolve/"
            "f9f88ac3e234be915e23811a6d28ea287bdb927e/Qwen3.5-4B-Q4_K_M.gguf"
        ),
        "sha256": "25082a7dd3776cc3c741c6347d3bd04523f05796607b3fbc32fa3a25dfa1418c",
        "size_bytes": 2_707_513_696,
        "parameters": "4B",
        "context_length": 4096,
        "license_id": "Apache-2.0",
        "license_url": "https://huggingface.co/Qwen/Qwen3.5-4B/blob/main/LICENSE",
        "upstream_model": "Qwen/Qwen3.5-4B",
        "quant_source": "lmstudio-community/Qwen3.5-4B-GGUF",
        "requires_acceptance": False,
        "disable_thinking": False,
    },
    {
        "id": "granite-4.2-3b-q4",
        "profile": "granite-4.2-3b-q4",
        "name": "IBM Granite 4.2 3B",
        "description": "Alternativa IBM · modelo oficial com português testado",
        "filename": "granite-4.2-3b-Q4_K_M.gguf",
        "url": (
            "https://huggingface.co/ibm-granite/granite-4.2-3b-GGUF/resolve/"
            "c40945d71cd90f249a56985e8155551a9188dc30/granite-4.2-3b-Q4_K_M.gguf"
        ),
        "sha256": "e0406663965846ae22a403456eb826ccce5f450840491f71952f18a7cb78e7d5",
        "size_bytes": 2_244_011_552,
        "parameters": "3B",
        "context_length": 4096,
        "license_id": "Apache-2.0",
        "license_url": "https://huggingface.co/ibm-granite/granite-4.2-3b/blob/main/LICENSE",
        "upstream_model": "ibm-granite/granite-4.2-3b",
        "quant_source": "ibm-granite/granite-4.2-3b-GGUF",
        "requires_acceptance": False,
        "disable_thinking": False,
    },
    {
        "id": "gemma-4-e2b-q4",
        "profile": "gemma-4-e2b-q4",
        "name": "Gemma 4 E2B",
        "description": "2B efetivos / 5B totais · modelo oficial do Google, download maior",
        "filename": "gemma-4-E2B_q4_0-it.gguf",
        "url": (
            "https://huggingface.co/google/gemma-4-E2B-it-qat-q4_0-gguf/resolve/"
            "675cff42a74c774d6cb76f76d8eacb49b48c9b93/gemma-4-E2B_q4_0-it.gguf"
        ),
        "sha256": "fa401b55b07ee70a54c6dae3903c783a6e65064312529ea57175cb5f8dec6634",
        "size_bytes": 3_349_516_256,
        "parameters": "E2B / 5B",
        "context_length": 4096,
        "license_id": "Apache-2.0",
        "license_url": (
            "https://huggingface.co/google/gemma-4-E2B-it-qat-q4_0-gguf/blob/main/LICENSE"
        ),
        "upstream_model": "google/gemma-4-E2B-it",
        "quant_source": "google/gemma-4-E2B-it-qat-q4_0-gguf",
        "requires_acceptance": False,
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
