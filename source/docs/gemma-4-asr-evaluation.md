> Historical Sniptype record, retained unchanged from the predecessor. Release, path, and issue references describe Sniptype. See [extraction.md](extraction.md) for the current Snipvoice boundary.

# Gemma 4 ASR evaluation

Status: primary-source research note, 2026-08-27. Scope: evaluate Google
Gemma 4 E2B, E4B, and 12B Unified as local alternatives to Sniptype's current
Parakeet and Qwen profiles. No weights were downloaded and no target-machine
benchmark was run.

## Decision

Do not replace Parakeet or Qwen with Gemma 4 yet. Gemma 4 is a verified local
audio/ASR-capable family, but the available evidence describes a multimodal
text-generating model rather than a low-latency dictation engine. Google does
not publish CPU real-time factors, peak RSS, or isolated Brazilian-Portuguese
WER for these variants. The model-card accuracy table is not enough to clear
Sniptype's adoption gates.

The sensible follow-up, if we want to test it, is an experimental E2B or E4B
adapter behind the existing provider boundary. Keep Parakeet as the balanced
default and Qwen as the accuracy control until the same-corpus test proves a
material gain. Do not add Gemma weights or dependencies as part of this note.

## What is verified

Google's official audio guide names the exact instruction-tuned IDs
`google/gemma-4-E2B-it`, `google/gemma-4-E4B-it`, and
`google/gemma-4-12B-it`, and states that all three support multilingual ASR.
The official model cards also list audio as a native modality for E2B, E4B,
and 12B Unified. Audio input is mono, 16 kHz, float32, and limited to 30
seconds; the guide says audio costs 25 tokens per second. The documented path
uses Transformers `pipeline(task="any-to-any")` and autoregressive
`generate()`, with a prompt instructing the model to output only the
transcription.

The 12B announcement says that 12B projects raw audio directly into the LLM
embedding space and has no audio encoder. That may reduce encoder overhead,
but it does not make it a streaming recognizer: the official example still
decodes a bounded clip through a multimodal language model.

## Parameters, memory, and CPU

The `E` in E2B/E4B means effective parameters, not total resident weights.
Google's model card reports:

| Model | Effective / total-weight figure | Audio path | Official deployment target |
|---|---:|---|---|
| E2B | 2.3B effective; 5.1B with embeddings | ~300M audio encoder | Mobile devices |
| E4B | 4.5B effective; 8B with embeddings | ~300M audio encoder | Mobile devices and laptops |
| 12B Unified | 11.95B total | No separate encoder | Laptops, desktops, small servers |

The effective count is useful for compute intuition, but RAM is driven mainly
by the total stored weights plus runtime buffers, embeddings, tokenizer,
activations, and KV cache. Google's official load estimates are 2.9 GB / 5.7
GB / 11.4 GB for E2B at Q4 / SFP8 / BF16, 4.5 GB / 8.9 GB / 17.9 GB for E4B,
and 6.7 GB / 13.4 GB / 26.7 GB for 12B. Google says these estimates include
20% load overhead but exclude supporting software and context-window memory;
they are GPU/TPU estimates, not measured Windows process RSS. Google separately
describes 12B as fitting in 16 GB of VRAM/unified memory, which is a deployment
claim rather than a CPU-RSS result.

No authoritative Gemma 4 source found in this review reports CPU throughput,
real-time factor, cold-load time, peak RSS, average CPU, or power on the
Sniptype target hardware. Google's audio notebook explicitly runs on a T4 GPU.
Therefore any Gemma-vs-Parakeet/Qwen CPU or memory ranking beyond the weight
arithmetic above is an inference. A decoder-only multimodal model also has to
generate text tokens after ingesting audio, so it is structurally more exposed
to decode latency than the current purpose-built ASR paths; this is a design
inference, not a measured result.

## Accuracy comparison

Gemma's official model-card table reports these audio scores:

| Model | CoVoST | FLEURS (lower is better) |
|---|---:|---:|
| E2B | 33.47 | 0.09 |
| E4B | 35.54 | 0.08 |
| 12B Unified | 38.5* | 0.069* |

`*` excludes Chinese. The table does not provide an isolated `pt-BR` row, a
named-entity score, punctuation score, or a clean transcription WER protocol
that can be aligned directly with Sniptype's existing results. CoVoST is a
speech-translation benchmark, not a dictation WER comparison. The FLEURS rows
are useful evidence that Gemma can perform multilingual audio tasks, but they
do not establish superiority over Parakeet or Qwen on our corpus.

The current local comparison records Parakeet TDT 0.6B v3 at 4.76% Portuguese
and 4.85% English on NVIDIA's reference FLEURS evaluation, and Qwen3-ASR-1.7B
at 3.35% English FLEURS plus a 4.90% multilingual FLEURS aggregate that
includes Portuguese. Those numbers are from different model cards and
evaluation protocols; they are directional only. Neither the existing
Parakeet nor Qwen figures prove quantized pt-BR performance. Gemma has no
published isolated pt-BR result in the sources reviewed here.

## Side-by-side product evidence

Artifact and memory figures are not interchangeable: the current models list
download size plus Sniptype's conservative RAM gate, while Gemma lists Google's
Q4 load estimate. CPU throughput is comparable only for Parakeet and Qwen,
because those results came from the same transcribe.cpp benchmark harness.

| Candidate | Published accuracy signal | Local weight/load signal | Published CPU signal | Sniptype fit |
|---|---|---:|---|---|
| Parakeet TDT 0.6B v3 Q8 | FLEURS pt 4.76%, en 4.85% on the reference checkpoint; quantized pt-BR remains unmeasured | 740 MB GGUF; 1.5 GB minimum / 2 GB recommended in the catalog | 7-8x realtime on Ryzen 4750U; 27-29x on M4 Max | Keep as Balanced default |
| Qwen3-ASR-1.7B Q8 | FLEURS-en 3.35%; multilingual FLEURS 4.90%, with no isolated pt-BR row | 2.08 GB GGUF; 6 GB minimum / 8 GB recommended in the catalog | 1.9-2.1x realtime on Ryzen 4750U; 8x on M4 Max | Keep as Accuracy control |
| Gemma 4 E2B Q4 | FLEURS 0.09 aggregate; no isolated pt-BR row | 2.9 GB official load estimate | No official audio RTF, latency, or CPU-use measurement | Only plausible first experiment |
| Gemma 4 E4B Q4 | FLEURS 0.08 aggregate; no isolated pt-BR row | 4.5 GB official load estimate | No official audio RTF, latency, or CPU-use measurement | Experimental, likely high CPU cost |
| Gemma 4 12B Q4 | FLEURS 0.069 aggregate, excluding Chinese; no isolated pt-BR row | 6.7 GB official load estimate | No official audio RTF, latency, or CPU-use measurement | Too heavy for a default dictation profile |

## Streaming and integration fit

Sniptype's `LocalVoiceProvider` currently wraps transcribe.cpp and its pinned
Q8 GGUF catalog. The current profiles have a native provider boundary,
cancellation, completed-utterance decoding, and a separate stateful streaming
candidate. Gemma's official local path currently requires the Transformers /
PyTorch stack, `torchvision`, `librosa`, and `accelerate`; the audio guide
installs Transformers 5.10.1 or newer. That is a materially different runtime
and would add a large dependency and packaging surface. Adding it requires
explicit dependency approval.

The official 12B announcement mentions ecosystem integrations including
Transformers, llama.cpp, MLX, SGLang, and vLLM, but the official ASR example is
Transformers-based. Current llama.cpp documentation lists E2B and E4B as mixed
audio/image models, while warning that audio support is highly experimental
and may reduce quality. It does not make Gemma a transcribe.cpp drop-in: the
runtime, multimodal projector, prompt/generation path, packaging, and provider
adapter would all be new integration surfaces. The same documentation does not
yet list 12B in its mixed-modality quick-start set, so 12B needs a separate
runtime proof rather than an inferred compatibility claim.

Google documents a maximum 30-second audio clip and a prompt-plus-`generate`
workflow, but no partial-transcript or chunk-state API in the reviewed sources.
Consequently Gemma should be treated as batch-only until a runtime demonstrates
bounded chunk latency and cancellation. E2B/E4B are the only plausible first
experiments; 12B is a heavyweight accuracy/understanding experiment, not a
default dictation engine.

## License

Google publishes Gemma 4 under Apache 2.0. That is operationally simpler than
Parakeet's CC-BY-4.0 and avoids the derivative-license review required by the
current Nemotron profile. The model and any runtime still need their own
notices and exact redistribution review before shipping. License permissiveness
does not answer the performance or packaging questions.

## Required proof before adoption

Run E2B and E4B first, with 12B only if they fail the accuracy target but fit
the machine. Use the same recorded pt-BR, en-US, and code-switched corpus as
Parakeet/Qwen and record model revision, quantization, cold/warm load, p50/p95
release-to-text latency, peak RSS, average/peak CPU, real-time factor,
punctuation, named entities, and cancellation behavior. Reject a cross-model
claim unless audio segmentation, normalization, and scoring are identical.

The current gate remains: balanced profile p95 <= 1.5 s and <= 1.5 GB RSS;
accuracy profile p95 <= 3.0 s and <= 6 GB RSS, with a material same-corpus
accuracy improvement. Gemma cannot currently be marked compliant because its
CPU/RSS and pt-BR evidence are missing.

## Sources

- Google, [Gemma 4 audio understanding guide](https://ai.google.dev/gemma/docs/capabilities/audio) — exact IDs, ASR prompt, input format, 30-second limit, token cost, and T4 notebook.
- Google DeepMind, [Gemma 4 model card: E2B](https://huggingface.co/google/gemma-4-E2B) — architecture, parameter table, audio capability, benchmark table, and Apache 2.0 link.
- Google DeepMind, [Gemma 4 model card: E4B](https://huggingface.co/google/gemma-4-E4B) — official E4B checkpoint and model-card metadata.
- Google DeepMind, [Gemma 4 model card: 12B Unified](https://huggingface.co/google/gemma-4-12B) — total parameters, encoder-free audio path, benchmark table, and local usage.
- Google, [Gemma 4 announcement](https://blog.google/innovation-and-ai/technology/developers-tools/gemma-4/) — effective-parameter explanation, edge targets, and Apache 2.0 release.
- Google, [Introducing Gemma 4 12B](https://blog.google/innovation-and-ai/technology/developers-tools/introducing-gemma-4-12B/) — encoder-free audio design, 16 GB deployment claim, and ecosystem references.
- llama.cpp, [multimodal support](https://github.com/ggml-org/llama.cpp/blob/master/docs/multimodal.md) — current E2B/E4B audio support and experimental-quality warning.
- NVIDIA, [Parakeet TDT 0.6B v3 model card](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3).
- Qwen, [Qwen3-ASR-1.7B model card](https://huggingface.co/Qwen/Qwen3-ASR-1.7B).
- handy-computer, [transcribe.cpp Parakeet model card](https://github.com/handy-computer/transcribe.cpp/blob/main/docs/models/parakeet-tdt-0.6b-v3.md) and [Qwen model card](https://github.com/handy-computer/transcribe.cpp/blob/main/docs/models/qwen3-asr-1.7b.md) — current portable-runtime size and CPU evidence recorded in Sniptype's [existing comparison](voice-model-value-research.md).
