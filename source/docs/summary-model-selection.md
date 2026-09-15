# Offline summary model selection

Research updated: 2026-09-15. Snipvoice uses a small, fixed catalog for local
meeting summaries through its packaged llama.cpp runtime. The catalog favors
Portuguese and English, laptop-class hardware, public downloads, and licenses
that permit redistribution and local use.

## Recommendation

Use **Qwen3.5 2B Q4_K_M** as the default. Qwen publishes it as a 2B model with
a native 262K context, support for 201 languages and dialects, and non-thinking
behavior by default. Qwen's own language table reports material gains over
Qwen3 1.7B, including MMLU-Pro 55.3 versus 40.2 and multilingual MMMLU 56.9
versus 46.7 in non-thinking mode. These are vendor benchmarks, not a Snipvoice
meeting-summary evaluation. Snipvoice keeps the runtime context at 4K to bound
memory use and already reduces long transcripts before inference.

- Upstream: [Qwen/Qwen3.5-2B](https://huggingface.co/Qwen/Qwen3.5-2B)
- Quantization: [LM Studio Community Qwen3.5 2B GGUF](https://huggingface.co/lmstudio-community/Qwen3.5-2B-GGUF)
- Pinned Q4_K_M: 1,270,808,032 bytes; SHA-256
  `0bfe35afc9f05b7fac3fa04925e051ac7939a42a8a17ea11afc99701bea826cc`

The Qwen GGUF files are community conversions of the official
Apache-2.0 weights. Snipvoice pins the exact Hub revision, byte count, and hash;
it does not follow a mutable `main` download URL.

## Shipped catalog

| Model | Role | Parameters shown to users | GGUF size | Artifact source | License |
| --- | --- | --- | ---: | --- | --- |
| Qwen3.5 2B Q4_K_M | Default balance | 2B | 1.18 GiB | LM Studio Community conversion of Qwen | Apache-2.0 |
| Qwen3.5 4B Q4_K_M | Higher quality | 4B | 2.52 GiB | LM Studio Community conversion of Qwen | Apache-2.0 |
| LiquidAI LFM2.5-2.6B Q4_K_M | Efficient alternative | 2.6B | 1.56 GiB | First-party LiquidAI GGUF | LFM Open License v1.0 |
| Gemma 4 E2B QAT Q4_0 | Current Google alternative | E2B / 5B total | 3.12 GiB | First-party Google GGUF | Apache-2.0 |
| Gemma 4 E4B QAT Q4_0 | Higher-capacity Google option | E4B / 8B total | 4.80 GiB | First-party Google GGUF | Apache-2.0 |

[Qwen3.5 4B](https://huggingface.co/Qwen/Qwen3.5-4B) stays within the requested
4B ceiling and offers a quality-oriented option for machines with more memory.
Its pinned Q4_K_M file is 2,707,513,696 bytes with SHA-256
`25082a7dd3776cc3c741c6347d3bd04523f05796607b3fbc32fa3a25dfa1418c`.

[LiquidAI LFM2.5-2.6B](https://huggingface.co/LiquidAI/LFM2.5-2.6B) replaces
Granite 4.2 3B as the smaller efficient alternative. LiquidAI lists Portuguese
among 16 supported languages, reports 2.69B total parameters and a native 128K
context, and publishes the GGUF directly. The pinned Q4_K_M file is
1,674,455,040 bytes with SHA-256
`02a8b7e17487d326e46d68ce0ba24211e1b80a14c4cd0597fa73c1cd697f52ed`.

LFM2.5 is governed by the LFM Open License v1.0, not MIT or Apache-2.0.
The license excludes commercial use by a legal entity with annual revenue of
US$10 million or more and requires the license to accompany redistributed
copies. Snipvoice shows these conditions and requires explicit acceptance
before the model's first download.

[Gemma 4 E2B](https://huggingface.co/google/gemma-4-E2B-it-qat-q4_0-gguf) is the
current Google option. Google labels the variant E2B, while the Hub metadata
reports 5B total parameters. The UI states both figures. Its first-party QAT
Q4_0 GGUF is 3,349,516,256 bytes with SHA-256
`fa401b55b07ee70a54c6dae3903c783a6e65064312529ea57175cb5f8dec6634`.
The separate multimodal projector is intentionally not downloaded because
Snipvoice sends transcript text only.

[Gemma 4 E4B](https://huggingface.co/google/gemma-4-E4B-it) is an advanced
opt-in. Google reports 4.5B effective and 8B total parameters. Its published
benchmarks are materially above E2B, including MMLU-Pro 69.4% versus 60.0% and
MMMLU 76.6% versus 67.4%; these vendor results do not measure Snipvoice summary
quality. The pinned first-party QAT Q4_0 GGUF is 5,154,941,280 bytes with
SHA-256 `676c35070db6dbe52f93e9c864ee0fba4eddea94b9c875d9cb10daff453fbaee`.

## Downloader and compatibility rules

Every catalog URL names an immutable Hub commit. A model becomes usable only
after its exact size and SHA-256 match. Downloads stream to a resumable partial
file and are atomically promoted after verification. Users may download and
remove each model from the **Resumo local** tab; inference makes no network
request after installation.

The packaged `llama-cpp-python` version recognizes Qwen3.5, LFM2, and Gemma 4
GGUF architectures. Snipvoice relies on each GGUF's embedded chat
template, requests deterministic JSON, and caps output. Previous catalog IDs
migrate to the corresponding current family without deleting old cached files.

## Evidence limits

- No same-transcript PT-BR/en-US quality and latency comparison has been run in
  Snipvoice, so the default is based on current upstream evidence and practical
  size rather than an app-specific benchmark.
- The exact model downloads and live llama.cpp inference were not exercised as
  part of this catalog update; the downloader metadata came from the current
  Hugging Face model API and is pinned against immutable revisions.
- LiquidAI's model license has use and redistribution conditions beyond the
  application license. The in-app notice summarizes them but does not replace
  the complete LFM Open License v1.0.
- Gemma 4 E2B exceeds four billion total parameters even though its name and
  effective-compute class are E2B. Its download size and both parameter figures
  remain visible before download.
- Gemma 4 E4B is outside the original 1–4B total-parameter target. It remains an
  explicit advanced option because its 4.5B effective class can improve quality
  on hardware that can accommodate the 4.80 GiB weights plus runtime overhead.
