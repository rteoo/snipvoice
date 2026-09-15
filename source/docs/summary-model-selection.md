# Offline summary model selection

Research date: 2026-09-14. This note selects a small, downloadable catalog for
Snipvoice meeting summaries. The target is local inference through llama.cpp,
with Portuguese (Brazil) and English support, modest RAM use, and no account or
cloud runtime requirement after the model is downloaded.

## Recommendation

Use **Qwen3-1.7B** as the default summary model, in a Q4_K_M GGUF, with
thinking disabled for summaries. Its official card lists 1.7B parameters, a
32,768-token context, support for 100+ languages and dialects, and a hard
non-thinking mode intended to improve efficiency. It is Apache 2.0 and its
official repository is public. The ggml-org GGUF is already published with a
Q4_K_M artifact of about 1.28 GB. [Qwen3 card](https://huggingface.co/Qwen/Qwen3-1.7B/blob/main/README.md),
[Qwen3 GGUF](https://huggingface.co/ggml-org/Qwen3-1.7B-GGUF)

The application should explicitly use Qwen3's non-thinking chat template mode
(`enable_thinking=False`) and impose its own short output limit. This is an
implementation inference from the model's documented modes, not a claim that
Qwen3 has been benchmarked inside Snipvoice. Thinking would add latency and
intermediate output without helping a constrained meeting-summary format.

## Candidate catalog

| Candidate and exact source | Parameters / context | Language fit | License and access | llama.cpp / download class | Decision |
| --- | --- | --- | --- | --- | --- |
| [Qwen/Qwen3-1.7B](https://huggingface.co/Qwen/Qwen3-1.7B) via [ggml-org/Qwen3-1.7B-GGUF](https://huggingface.co/ggml-org/Qwen3-1.7B-GGUF) | 1.7B; 32K context | 100+ languages and dialects; Portuguese is included in the published language metadata | Apache 2.0; public Hub repositories, no acceptance gate shown | Official ggml-org GGUF; Q4_K_M ~1.28 GB, Q8_0 ~2.17 GB, F16 ~4.07 GB | **Default**. Best balance of current multilingual instruction following, size, and direct GGUF availability. |
| [Qwen/Qwen2.5-1.5B-Instruct](https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct) via [Qwen/Qwen2.5-1.5B-Instruct-GGUF](https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF) | 1.54B; 32K context, 8K generation | 29+ languages, explicitly including Portuguese and English | Apache 2.0; public Hub repositories, no acceptance gate shown | Official Qwen GGUF; Q4_K_M ~1.12 GB | **Fallback/stability option**. Slightly smaller and has a simpler non-reasoning behavior, useful on weaker machines. |
| [ibm-granite/granite-3.3-2b-instruct](https://huggingface.co/ibm-granite/granite-3.3-2b-instruct) via [official GGUF](https://huggingface.co/ibm-granite/granite-3.3-2b-instruct-GGUF) | 2B; 128K published context, capped to 4K by Snipvoice | English and Portuguese are explicitly listed | Apache 2.0; public Hub repositories | First-party IBM Q4_K_M GGUF ~1.55 GB | **Meeting-focused alternative**. The official card explicitly lists long-document and meeting summarization use cases. |
| [google/gemma-3-1b-it](https://huggingface.co/google/gemma-3-1b-it) via [ggml-org/gemma-3-1b-it-GGUF](https://huggingface.co/ggml-org/gemma-3-1b-it-GGUF) | 1B; 32K context for the 1B variant | The 1B card is described as English-focused; the 4B+ variants carry the broad 140+ language claim. Do not promise strong PT-BR quality from 1B without local evaluation. | Gemma terms, not Apache/MIT. The Hub repository requires the user to log in and accept Google's usage license before files can be downloaded; Google's terms include use restrictions and downstream notice obligations. | Official ggml-org GGUF; Q4_K_M ~806 MB, Q8_0 ~1.07 GB, F16 ~2.01 GB | **Optional lightweight model**. Smallest download, but licensing friction and weaker documented PT-BR fit make it a secondary choice. |
| [HuggingFaceTB/SmolLM2-1.7B-Instruct](https://huggingface.co/HuggingFaceTB/SmolLM2-1.7B-Instruct) | 1.7B; the card does not state a long context window in the cited model summary | The official card says it primarily understands and generates English | Apache 2.0; public repository | Community GGUF repositories exist; one published Q4_K_M is ~1.06 GB. This is not an official Hugging Face or ggml-org quantization. | **Do not ship in the initial catalog**. Useful English-only fallback, but inferior language fit and weaker provenance for the GGUF artifact. |
| [microsoft/Phi-4-mini-instruct](https://huggingface.co/microsoft/Phi-4-mini-instruct) | 3.8B; 128K context | 24 languages including Portuguese and English | MIT; public repository, no Hub acceptance gate shown | No official Microsoft or ggml-org GGUF was found in this review; community GGUFs exist. Q4 size should be treated as roughly 2–3 GB until the exact artifact is pinned and hashed. | **Later/advanced option**. It may produce better summaries with more memory, but it is above the preferred 1–2B class and lacks a first-party GGUF path. |

### Why Qwen3 over Gemma and Qwen2.5

Gemma 3 is attractive on size and its 1B Q4_K_M artifact is only about 806 MB,
but Google's official model information gives the 1B variant 32K context and
the broad 140+ language description to the 4B and larger variants. The Hub
also gates the files behind acceptance of Gemma's terms. That makes Gemma a
good opt-in experiment, not the universal default for PT-BR users. [Gemma 3
card](https://huggingface.co/google/gemma-3-1b-it), [Gemma terms](https://ai.google.dev/gemma/terms)

Qwen2.5-1.5B remains a sound fallback: its official card explicitly names
Portuguese, gives a 32K context, and uses Apache 2.0. Qwen3 is preferable for
the default because its own card documents stronger instruction-following
improvements, 100+ language support, and a strict non-thinking switch suited to
fast summaries. Those are vendor-reported capabilities; Snipvoice should run a
small PT-BR/English meeting-summary fixture before declaring a quality winner.
[Qwen2.5 card](https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct/blob/main/README.md)

Phi-4-mini-instruct is the strongest candidate above 2B: Microsoft documents
3.8B parameters, 128K context, Portuguese support, and an MIT license. It is
worth adding only after the app can pin a specific GGUF repository, file, SHA256
and prompt template. A generic community conversion would weaken the
reproducibility and supply-chain guarantees required for an in-app downloader.
[Phi-4-mini card](https://huggingface.co/microsoft/Phi-4-mini-instruct/blob/main/README.md)

## llama.cpp and downloader implications

llama.cpp requires GGUF for local model loading. Its model documentation supports
the `-hf <owner>/<repo>[:quant]` convention and also documents running a local
GGUF file. The project publishes pre-built binaries and supports CPU, Apple
Silicon, CUDA, HIP, Vulkan and other backends. Snipvoice should use the local
llama.cpp runtime and download only from a pinned, reviewed catalog; it should
not accept arbitrary repository names from the settings UI. [llama.cpp model
documentation](https://github.com/ggml-org/llama.cpp/blob/master/docs/models.md),
[llama.cpp README](https://github.com/ggml-org/llama.cpp/blob/master/README.md)

For the first implementation, pin these entries and files:

```text
Qwen3 default:
  repo: ggml-org/Qwen3-1.7B-GGUF
  file/quant: Q4_K_M
  license: Apache-2.0

Granite meeting-focused alternative:
  repo: ibm-granite/granite-3.3-2b-instruct-GGUF
  file: granite-3.3-2b-instruct-Q4_K_M.gguf
  license: Apache-2.0

Gemma opt-in:
  repo: ggml-org/gemma-3-1b-it-GGUF
  file: gemma-3-1b-it-Q4_K_M.gguf
  license: Gemma terms; require acceptance before download
```

The catalog stores the exact URL, byte size, SHA256, license URL, and
prompt-template/runtime compatibility. Downloads belong in a separate
non-roaming Snipvoice summary cache, use a temporary file plus atomic rename, and
must be resumable or safely restartable. The UI should show the license and
approximate disk size before download, report progress, and leave a partial file
unusable after cancellation or failure.

## Open questions and limits

- No apples-to-apples Snipvoice fixture evaluation was run here; “best” is an
  evidence-backed catalog recommendation, not a measured quality ranking.
- Exact Qwen3 tokenizer/template behavior must be validated against the version
  of llama.cpp bundled with Snipvoice, especially the non-thinking switch.
- Gemma uses separate terms. Snipvoice requires explicit acceptance before it
  downloads the public ggml-org GGUF and keeps the license URL visible.
- Phi-4-mini's 128K context is useful for long transcripts, but the larger
  quantized footprint and lack of a first-party GGUF artifact make it a poor v1
  default. Transcript chunking remains necessary for all models when the input
  exceeds the selected context budget.
- Model weights are not included in the repository and were not downloaded for
  this research.
