> Historical Sniptype record, retained unchanged from the predecessor. Release, path, and issue references describe Sniptype. See [extraction.md](extraction.md) for the current Snipvoice boundary.

# Local voice model comparison

Status: research note, 2026-08-13. Scope: Sniptype push-to-talk and optional
streaming on Windows x64 (Core i7-13620H, 32 GB RAM) and macOS ARM64. No model
was installed or benchmarked on the target machines.

## Decision

**Maximum likely accuracy: Qwen3-ASR-1.7B Q8_0.** It has the strongest published
English result in this set and wins Qwen's multilingual aggregates that include
Portuguese. The trade is a 2.08 GB Q8 model and roughly one-quarter the CPU
throughput of Parakeet/Nemotron in the same portable C++ runtime.

**Best balance: Parakeet TDT 0.6B v3 Q8_0/INT8.** Its Q8 English WER is effectively
unchanged from F32 in transcribe.cpp, while the model shrinks from 2.51 GB to
740 MB. It also has the strongest directly published Portuguese checkpoint
result among the portable candidates.

**Best true streaming choice: Nemotron 3.5 0.6B Q8_0/INT8.** CPU throughput is
similar to Parakeet, but its stateful cache-aware design produces partials and
offers explicit latency/accuracy tiers. Its pt-BR and en-US accuracy is weaker.

Do not ship Parakeet F32 by default: no evidence here justifies its roughly
3.4x larger artifact over Q8_0.

## Comparison

WER is lower-is-better. Cross-row accuracy values are not automatically
apples-to-apples; the dataset/runtime is named in each cell.

| Candidate | PT-BR accuracy | EN-US accuracy | Model size | Same-runtime CPU performance | Memory evidence | Judgment |
|---|---|---|---:|---|---|---|
| **Parakeet TDT 0.6B v3 F32** | NVIDIA FLEURS Portuguese: **4.76%**. NVIDIA notes training Portuguese was European while the benchmark is mostly Brazilian. | NVIDIA FLEURS English: **4.85%**; transcribe.cpp LibriSpeech test-clean: **1.95%**. | **2.51 GB** GGUF/checkpoint | No F32 result in the comparable portable benchmark. It should be heavier than Q8, but the size alone does not quantify speed. | NVIDIA says at least 2 GB RAM; exact process RSS is unpublished. | Accuracy/control baseline only. |
| **Parakeet TDT 0.6B v3 Q8_0 / INT8** | No published quantized pt-BR WER. Do not silently inherit 4.76%. | transcribe.cpp LibriSpeech test-clean: **1.94%**, versus F32 1.95% in the same harness. | **740 MB Q8 GGUF**; sherpa INT8 components total about **640 MB**. | M4 Max CPU: **27–29x realtime**. Ryzen 7 4750U CPU: **7–8x**. A separate ONNX runtime reports ~20x on Ryzen 5700X and ~5x on old i5-6500. | Exact RSS unpublished; artifact size is only a lower-bound/relative footprint signal. | **Best overall balance and safest default.** |
| **Nemotron 3.5 ASR Streaming 0.6B Q8_0 / INT8** | NVIDIA at 560 ms, language supplied: **5.65%** FLEURS; auto: 5.57%. A separate Core ML conversion at 2.24 s reports **6.14%** pt-BR. | NVIDIA at 560 ms, supplied: **7.99%**; auto: 8.80%. At the 1.12 s offline tier, transcribe.cpp Q8 scores **7.88%** versus reference 7.99% on FLEURS English. | **716 MB Q8 GGUF**; sherpa INT8 components total about **650 MB**. | M4 Max CPU: **28–30x realtime**. Ryzen 7 4750U CPU: **7–8x**. | Exact RSS unpublished. Stateful streaming uses bounded caches rather than a growing LLM KV cache. | **Best for live partials; weaker dictation accuracy.** |
| **Qwen3-ASR-1.7B Q8_0** | Qwen publishes no per-language Portuguese result. It scores **4.90% FLEURS aggregate**, **8.55% MLS**, and **9.18% CommonVoice** across multilingual sets that include Portuguese. | Qwen FLEURS English: **3.35%**. transcribe.cpp LibriSpeech test-clean Q8: **1.61%**, versus BF16 1.62%. | **2.08 GB Q8 GGUF**; **3.89 GB BF16 GGUF**; official repository about 4.7 GB. | M4 Max CPU: **8x realtime**. Ryzen 7 4750U CPU: **1.9–2.1x**. This is about 3.5–4x slower than Parakeet/Nemotron in the same runtime. | Q8 transcribe.cpp RSS is unpublished. A different BF16 C runtime measured **6.57 GiB peak RSS for 10 s** and about 6.7 GiB in segmented long-audio mode on M3 Max. | **Highest likely accuracy, with the largest CPU/RAM cost.** |

## Interpretation for Sniptype

- On the i7-13620H/32 GB Windows machine, all four should fit. Qwen's latency,
  thermals, and sustained CPU use are the practical risks, not capacity.
- The same-runtime Ryzen figures suggest Parakeet/Nemotron will finish a short
  utterance several times faster than Qwen. The i7-13620H should outperform
  that older 4750U, but an exact multiplier would be speculation.
- Parakeet Q8 is supported by the cleanest quantization evidence: English WER
  changed from 1.95% to 1.94% in one full test-clean run. This does **not** prove
  pt-BR quantization neutrality, so pt-BR corpus testing remains mandatory.
- Qwen is clearly stronger on published English accuracy. For Portuguese, its
  lead is plausible but unproven because Qwen does not publish a pt-BR row.
- Nemotron's `560 ms` is model lookahead/chunk configuration, not total
  push-to-talk release-to-text latency.

## Sources

- NVIDIA, [Parakeet TDT 0.6B v3 model card](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3).
- handy-computer, [Parakeet v3 transcribe.cpp model card](https://github.com/handy-computer/transcribe.cpp/blob/main/docs/models/parakeet-tdt-0.6b-v3.md) (quantized WER, sizes, same-runtime CPU benchmarks).
- k2-fsa, [sherpa-onnx Parakeet export](https://k2-fsa.github.io/sherpa/onnx/pretrained_models/offline-transducer/nemo-transducer-models.html).
- NVIDIA, [Nemotron 3.5 model card](https://huggingface.co/nvidia/nemotron-3.5-asr-streaming-0.6b).
- handy-computer, [Nemotron 3.5 transcribe.cpp model card](https://github.com/handy-computer/transcribe.cpp/blob/main/docs/models/nemotron-3.5-asr-streaming-0.6b.md) (quantized WER, sizes, same-runtime CPU benchmarks).
- FluidInference, [FluidAudio benchmarks](https://github.com/FluidInference/FluidAudio/blob/main/Documentation/Benchmarks.md) (Core ML pt-BR/en-US and throughput results).
- Qwen, [Qwen3-ASR-1.7B model card](https://huggingface.co/Qwen/Qwen3-ASR-1.7B) and [official repository](https://github.com/QwenLM/Qwen3-ASR).
- handy-computer, [Qwen3-ASR-1.7B transcribe.cpp model card](https://github.com/handy-computer/transcribe.cpp/blob/main/docs/models/qwen3-asr-1.7b.md) (quantized WER, sizes, same-runtime CPU benchmarks).
- antirez, [qwen-asr CPU runtime](https://github.com/antirez/qwen-asr) (BF16 CPU throughput and peak RSS).

## Required local proof

Benchmark Parakeet Q8, Nemotron Q8 at 560 ms, and Qwen 1.7B Q8 on the same real
pt-BR, en-US, and code-switched short-utterance corpus. Measure WER, named
entities, punctuation, cold load, peak RSS, average/peak CPU, and p50/p95
release-to-result latency. That run decides whether Qwen's accuracy gain is
worth its roughly fourfold CPU cost.
