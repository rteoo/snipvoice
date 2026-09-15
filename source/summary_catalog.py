"""Pinned llama.cpp models for local meeting summaries."""


DEFAULT_SUMMARY_MODEL = "qwen3.5-2b-q4"

SUMMARY_CATALOG = (
    {
        "id": "qwen3.5-0.8b-q4",
        "profile": "qwen3.5-0.8b-q4",
        "name": "Qwen3.5 0.8B",
        "description": "Economia máxima · para hardware limitado, com menor qualidade",
        "filename": "Qwen3.5-0.8B-Q4_K_M.gguf",
        "url": (
            "https://huggingface.co/lmstudio-community/Qwen3.5-0.8B-GGUF/resolve/"
            "7925ccdc665d4efdb1034791e6b553e11128e6f8/Qwen3.5-0.8B-Q4_K_M.gguf"
        ),
        "sha256": "f5b14da98939b60bbe1019a964eba656407e1e0b64f1fe3003ff6d650e93bfec",
        "size_bytes": 527_502_816,
        "parameters": "0.8B",
        "context_length": 4096,
        "license_id": "Apache-2.0",
        "license_url": "https://huggingface.co/Qwen/Qwen3.5-0.8B/blob/main/LICENSE",
        "upstream_model": "Qwen/Qwen3.5-0.8B",
        "quant_source": "lmstudio-community/Qwen3.5-0.8B-GGUF",
        "requires_acceptance": False,
        "disable_thinking": False,
    },
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
        "id": "lfm2.5-2.6b-q4",
        "profile": "lfm2.5-2.6b-q4",
        "name": "LiquidAI LFM2.5-2.6B",
        "description": "Alternativa eficiente · modelo oficial para português e uso local",
        "filename": "LFM2.5-2.6B-Q4_K_M.gguf",
        "url": (
            "https://huggingface.co/LiquidAI/LFM2.5-2.6B-GGUF/resolve/"
            "84022ce711b28455e8c4fc364ce68c00cf995875/LFM2.5-2.6B-Q4_K_M.gguf"
        ),
        "sha256": "02a8b7e17487d326e46d68ce0ba24211e1b80a14c4cd0597fa73c1cd697f52ed",
        "size_bytes": 1_674_455_040,
        "parameters": "2.6B",
        "context_length": 4096,
        "license_id": "LFM Open License v1.0",
        "license_url": (
            "https://huggingface.co/LiquidAI/LFM2.5-2.6B-GGUF/blob/"
            "84022ce711b28455e8c4fc364ce68c00cf995875/LICENSE"
        ),
        "license_notice": (
            "Esta licença não é MIT nem Apache-2.0. O uso comercial por pessoa jurídica "
            "com receita anual de US$ 10 milhões ou mais não é licenciado. Cópias "
            "redistribuídas devem incluir a licença."
        ),
        "upstream_model": "LiquidAI/LFM2.5-2.6B",
        "quant_source": "LiquidAI/LFM2.5-2.6B-GGUF",
        "requires_acceptance": True,
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
    {
        "id": "gemma-4-e4b-q4",
        "profile": "gemma-4-e4b-q4",
        "name": "Gemma 4 E4B",
        "description": "4,5B efetivos / 8B totais · mais qualidade e maior uso de memória",
        "filename": "gemma-4-E4B_q4_0-it.gguf",
        "url": (
            "https://huggingface.co/google/gemma-4-E4B-it-qat-q4_0-gguf/resolve/"
            "4b4a2c1d584be7264f87aac328a1bc739ce81b6c/gemma-4-E4B_q4_0-it.gguf"
        ),
        "sha256": "676c35070db6dbe52f93e9c864ee0fba4eddea94b9c875d9cb10daff453fbaee",
        "size_bytes": 5_154_941_280,
        "parameters": "E4B / 8B",
        "context_length": 4096,
        "license_id": "Apache-2.0",
        "license_url": (
            "https://huggingface.co/google/gemma-4-E4B-it-qat-q4_0-gguf/blob/main/LICENSE"
        ),
        "upstream_model": "google/gemma-4-E4B-it",
        "quant_source": "google/gemma-4-E4B-it-qat-q4_0-gguf",
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
