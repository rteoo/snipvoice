# LiquidAI LFM2.5-2.6B summary-model research

Research date: 2026-09-15. This note records the first-party artifact and
licensing facts needed before adding the model to Snipvoice's local
llama.cpp-compatible summary catalog.

## Candidate artifact

- **Upstream model:** [LiquidAI/LFM2.5-2.6B](https://huggingface.co/LiquidAI/LFM2.5-2.6B)
- **First-party GGUF repository:** [LiquidAI/LFM2.5-2.6B-GGUF](https://huggingface.co/LiquidAI/LFM2.5-2.6B-GGUF)
- **Recommended quantization:** `LFM2.5-2.6B-Q4_K_M.gguf` (Q4_K_M). LiquidAI's
  model card explicitly documents the `llama.cpp` invocation
  `LiquidAI/LFM2.5-2.6B-GGUF:Q4_K_M`.
- **Observed file size:** 1,674,455,040 bytes (1.56 GiB; Hub displays 1.67 GB).
- **Observed SHA-256:**
  `02a8b7e17487d326e46d68ce0ba24211e1b80a14c4cd0597fa73c1cd697f52ed`.
  This is the SHA-256 in the first-party Git LFS pointer, not a locally
  downloaded verification.
- **Pinned repository revision:**
  `84022ce711b28455e8c4fc364ce68c00cf995875`. The official Hub API returned
  this full revision, and its immutable resolve URL reports the expected
  1,674,455,040-byte artifact.

## Model and runtime facts

LiquidAI reports 2.6B parameters (2.69B total), a 131,072-token context
length, 30 layers, and Portuguese among its 16 supported languages. The model
card identifies the GGUF repository as the quantized format for `llama.cpp`,
and the official llama.cpp source recognizes the `lfm2` architecture and
constructs an LFM2 model for it. Snipvoice should still keep its existing
bounded summary context rather than adopting the model's maximum context.

Snipvoice pins `llama-cpp-python==0.3.35`; that release vendors llama.cpp commit
`4df29be4f4c3673f428170fda944a5b19f743bb8`, whose architecture registry
contains `LLM_ARCH_LFM2`.

## License and first-download notice

The Hub metadata identifies the license as `lfm1.0`; the bundled license names
it **LFM Open License v1.0**. It is neither MIT nor Apache-2.0. The license
grants reproduction, derivative-work, and distribution rights subject to its
terms, requires a copy of the license on redistribution, and limits
commercial use by a legal entity whose annual revenue is US$10 million or more.
This is a material redistribution/commercial-use condition, so the
catalog must expose a first-download notice and require acknowledgement before
the first download.

Suggested notice:

> This model is licensed under Liquid AI's **LFM Open License v1.0**, not MIT
> or Apache-2.0. Review the license before downloading or redistributing it.
> Commercial use by a legal entity with annual revenue at or above US$10 million is
> not licensed under the LFM Open License. The license must accompany copies
> that you redistribute.

The notice is a product safeguard, not a legal determination that Snipvoice's
particular use is permitted. Keep the complete license available with the
model metadata/package notices.

## Primary sources

- [LiquidAI LFM2.5-2.6B model card](https://huggingface.co/LiquidAI/LFM2.5-2.6B): parameters, context length, languages, and GGUF role.
- [LiquidAI first-party GGUF model card](https://huggingface.co/LiquidAI/LFM2.5-2.6B-GGUF): exact filename, Q4_K_M llama.cpp usage, and license metadata.
- [Q4_K_M Hub file page](https://huggingface.co/LiquidAI/LFM2.5-2.6B-GGUF/blob/main/LFM2.5-2.6B-Q4_K_M.gguf): file revision, size, and SHA-256.
- [Q4_K_M Git LFS pointer](https://huggingface.co/LiquidAI/LFM2.5-2.6B-GGUF/raw/main/LFM2.5-2.6B-Q4_K_M.gguf): authoritative pointer size and SHA-256.
- [LFM Open License v1.0](https://huggingface.co/LiquidAI/LFM2.5-2.6B-GGUF/blob/main/LICENSE): redistribution and commercial-use terms.
- [llama.cpp architecture registry](https://github.com/ggml-org/llama.cpp/blob/master/src/llama-arch.cpp) and [model construction](https://github.com/ggml-org/llama.cpp/blob/master/src/llama-model.cpp): official `lfm2` support.

## Evidence limits

- The artifact was not downloaded or executed locally during this research.
- No Snipvoice transcript-quality or latency benchmark exists for this model.
