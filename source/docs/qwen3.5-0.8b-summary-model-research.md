# Qwen3.5-0.8B summary-model research

Research date: 2026-09-15. This note records the best Q4_K_M artifact and the
runtime evidence for a small local summary option.

## Candidate artifact

- **Upstream model:** [Qwen/Qwen3.5-0.8B](https://huggingface.co/Qwen/Qwen3.5-0.8B)
- **Q4_K_M source:** [LM Studio Community Qwen3.5-0.8B-GGUF](https://huggingface.co/lmstudio-community/Qwen3.5-0.8B-GGUF).
  Qwen's own repository links users to community quantizations for llama.cpp;
  the official [ggml-org repository](https://huggingface.co/ggml-org/Qwen3.5-0.8B-GGUF)
  currently publishes BF16, Q4_0, and Q8_0, but no Q4_K_M. Snipvoice uses the
  LM Studio conversion to match its existing Qwen3.5 2B and 4B sources.
- **Filename:** `Qwen3.5-0.8B-Q4_K_M.gguf`
- **Immutable revision:** `7925ccdc665d4efdb1034791e6b553e11128e6f8`
- **Size:** `527,502,816` bytes (about 503 MiB; Hub displays 528 MB)
- **LFS SHA-256:**
  `f5b14da98939b60bbe1019a964eba656407e1e0b64f1fe3003ff6d650e93bfec`
- **Quant source:** LM Studio conversion of the Qwen checkpoint. The upstream
  model remains `Qwen/Qwen3.5-0.8B`.

## Model and language facts

Qwen reports 0.8B parameters, 24 layers, and a native 262,144-token context.
The official Qwen3.5 announcement lists Portuguese among the supported
languages and dialects within its 201-language coverage. The model is
multimodal in its native repository, but summary input is text-only; omit the
projector and use the language-model GGUF.

## llama.cpp compatibility

The official llama.cpp source has a `qwen35` architecture implementation and
conversion support. `llama-cpp-python==0.3.35` updates its vendored llama.cpp
to `ggml-org/llama.cpp@4df29be4f` (the binding changelog also records an
`adb55e514` update in that release); the pinned release's architecture registry
contains Qwen3.5 support. This is source-level compatibility evidence, not a
Snipvoice runtime load test. The Q4_K_M file's model page also documents
llama.cpp usage.

## License

Qwen and the GGUF conversion identify the model license as **Apache-2.0**.
Use the upstream [Qwen LICENSE](https://huggingface.co/Qwen/Qwen3.5-0.8B/blob/main/LICENSE)
as the license authority; retain the conversion attribution and license link in
catalog metadata.

## Primary sources

- [Qwen/Qwen3.5-0.8B model card](https://huggingface.co/Qwen/Qwen3.5-0.8B): parameters, context, model shape, and official license.
- [Qwen3.5 official announcement](https://qwen.ai/blog?email_hash=23463b99b62a72f26ed677cc556c44e8&id=qwen3.5): Portuguese and the broader supported-language list.
- [ggml-org official GGUF repository](https://huggingface.co/ggml-org/Qwen3.5-0.8B-GGUF): first-party llama.cpp organization and available files.
- [LM Studio Q4_K_M file page](https://huggingface.co/lmstudio-community/Qwen3.5-0.8B-GGUF/blob/7925ccdc665d4efdb1034791e6b553e11128e6f8/Qwen3.5-0.8B-Q4_K_M.gguf): immutable revision, size, and LFS SHA-256.
- [LM Studio Q4_K_M LFS pointer](https://huggingface.co/lmstudio-community/Qwen3.5-0.8B-GGUF/raw/7925ccdc665d4efdb1034791e6b553e11128e6f8/Qwen3.5-0.8B-Q4_K_M.gguf): authoritative byte size and SHA-256.
- [Official llama.cpp Qwen3.5 implementation](https://github.com/ggml-org/llama.cpp/blob/master/src/models/qwen35.cpp) and [conversion](https://github.com/ggml-org/llama.cpp/blob/master/conversion/qwen.py): architecture/runtime support.
- [llama-cpp-python 0.3.35 changelog](https://llama-cpp-python.readthedocs.io/en/latest/changelog/): vendored llama.cpp revision(s).

## Evidence limits

- Qwen does not currently publish a Q4_K_M GGUF in its own namespace; this
  catalog candidate depends on the LM Studio conversion.
- The artifact was not downloaded or loaded with Snipvoice during research.
- No Snipvoice summary-quality or latency benchmark exists for this model.
