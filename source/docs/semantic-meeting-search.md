# Local semantic meeting retrieval

The implementation is opt-in through **Library → Local semantic search**.
The boolean `semantic_search` is stored in `workspace.json`; absent means
disabled. Keyword and metadata search remain available. Disabling semantic
search releases its worker and retains the disposable index. Deleting an index
or model is a separate action.

## Implemented behavior

- Explicit **Install model…** action, download confirmation, cancellation,
  resumable transfer, size check, SHA-256 verification, and atomic installation.
  Automatic indexing never downloads a model.
- Text-only BF16 EmbeddingGemma 2 GGUF, 768-dimensional vectors, search/document
  prefixes, finite/nonzero validation, and L2 normalization. Native inference
  lives in a subprocess with bounded requests, a 120-second batch deadline,
  cancellation, and process cleanup. Input overflow fails instead of truncating.
  The adapter reads the native output projection (768 dimensions), rather than
  the encoder's 512 hidden channels. Windows venv workers bypass the redirector
  using CPython's multiprocessing launch pattern, so termination and memory
  measurements target the inference process itself.
- `embedding-models` follows the existing chosen model root and model relocation
  workflow. The packaged entrypoint dispatches `--embedding-worker` before
  desktop, mutex, capture, and inference imports.
- `semantic.sqlite` sits beside `library.sqlite` under the chosen data root.
  It contains derived float32 vectors and source identities/hashes, not copied
  recordings or transcript text. Whole generations publish atomically. Compatible
  unchanged vectors are reused; changed or corrupt rows are regenerated.
- Only readable completed transcript revisions with a valid segment count are
  eligible. Links/junctions, partial reads, invalid source references, and changed
  snapshots fail closed. Canonical filters and live source hashes apply before
  semantic ranking/fusion. Source records preserve session/revision/segment IDs
  and original start/end times; existing library navigation opens the cited
  revision and seeks its recording.
- Background indexing coalesces completion requests. Shutdown and the library
  cancel action stop inference. Successful workers remain warm for subsequent
  queries. Runtime/index failures visibly return keyword results.
- Reciprocal rank fusion uses the conventional `k=60` default, without tuning on
  holdout data. Transcript evidence remains distinct from reports. Identical
  overlapping transcript snippets are deduplicated within a revision.
- Initial ceilings: 20,000 transcript segments, 500 candidates per ranking,
  16 documents per native request, 8,000 text characters, 8,192 native tokens.
  Deeper pages retain keyword behavior. Increase ceilings only after measuring
  a representative library. No semantic similarity cutoff is calibrated yet;
  related results are retrieval suggestions, not proof that a question is answered.

## Synthetic benchmark

From `source`, run `python semantic_benchmark.py`. The public fixture contains
20 synthetic passages and 40 held-out PT-BR queries. It has no private meeting
material. The existing literal AND/BM25 retrieval is the baseline.

One local Windows CPU run on 2026-10-07 with the actual pinned model and the
project-built custom wheel described below:

| Query group | Count | BM25 Recall@5 / MRR@10 | Hybrid Recall@5 / MRR@10 |
| --- | ---: | ---: | ---: |
| Paraphrase | 20 | 0% / 0 | 100% / 1 |
| Exact term/name | 8 | 100% / 1 | 100% / 1 |
| Date | 4 | 100% / 1 | 100% / 1 |
| Negation | 4 | 0% / 0 | 100% / 1 |
| Unrelated | 4 | Undefined | Undefined |

The proposed synthetic gate passed: paraphrase recall improved by 100 percentage
points; exact recall did not decrease. Wilson 95% recall intervals are
approximately 0–16.1% for BM25 paraphrases, 83.9–100% for hybrid paraphrases,
and 67.6–100% for exact queries. Eight exact queries cannot establish a two-point
noninferiority margin. The unrelated group returned no keyword hits but **all
four queries returned hybrid suggestions**. No rejection threshold was fitted
to this holdout; unrelated-query calibration remains an acceptance gap.

| Measurement | BM25 | Hybrid |
| --- | ---: | ---: |
| First query | About 2 ms | 1,163 ms, includes model reload |
| Warm p50 / p95 | About 1 / 1 ms | 83.14 / 94.54 ms |
| Index build including model load | Not measured | 3,445 ms |
| SQLite bytes, keyword plus semantic | 65,536 | 167,936 |
| Parent peak working set | About 27 MB | 33,267,712 bytes |
| Native worker peak working set | None | 968,368,128 bytes (about 924 MiB) |

These numbers describe twenty short synthetic passages on one Windows host.
OS file caches were not flushed. Per-process peaks are not a simultaneous
process-tree peak. No settings were tuned on the holdout.

After an approved compatible runtime and model installation, run
`python semantic_benchmark.py --semantic`. This compares BM25 and hybrid recall
and MRR on the same holdout, reports the proposed quality gate, cold model-load
query latency and warm p50/p95, index-build wall time/size, and per-process peak
working sets. OS file caches are not flushed. Per-process peaks are not a
simultaneous process-tree peak. No settings are tuned on the holdout.
For an explicitly approved isolated cache, add
`--embedding-cache tests/tmp/embedding-validation/embedding-models`. This does
not update the application model-location pointer or download weights.

## Runtime integration and remaining acceptance

The former `llama-cpp-python==0.3.35` pin referenced llama.cpp commit
`4df29be4f4c3673f428170fda944a5b19f743bb8`. Its
[architecture registry](https://github.com/ggml-org/llama.cpp/blob/4df29be4f4c3673f428170fda944a5b19f743bb8/src/llama-arch.cpp)
does not recognize `gemma-embedding2`. EmbeddingGemma 2 support was merged in
[upstream PR 30054](https://github.com/ggml-org/llama.cpp/pull/30054) on 2026-10-06.
A compatible native build is required; version-number assumptions are insufficient.
The project now uses the [custom wheel recipe](../../packaging/README.md#local-summary-and-embedding-runtime)
in CI and both desktop bundle jobs. `requirements-voice.txt` pins the wheel's
Python dependencies; upstream llama.cpp wheels are deliberately excluded from
the source and generated release manifests. The recipe assembles SHA-256-pinned
source archives and installs the generated wheel through its own hash lock.
Local validation used Python 3.14.6, the Python wrapper at
[`1652066e0af45f2313b339670ef9555e8a54e545`](https://github.com/abetlen/llama-cpp-python/commit/1652066e0af45f2313b339670ef9555e8a54e545)
(0.3.36), and its llama.cpp submodule checked out at
[`4fbc76dec51d0add466f0210855c0596589b60d4`](https://github.com/ggml-org/llama.cpp/commit/4fbc76dec51d0add466f0210855c0596589b60d4).
The Windows CPU build used existing Visual Studio 2022/CMake tools with
`GGML_CUDA=OFF`, `LLAMA_CURL=OFF`, `GGML_NATIVE=OFF`, and `GGML_OPENMP=OFF`.
OpenMP is disabled to avoid an additional Windows runtime DLL. macOS retains
upstream's default Metal configuration; macOS execution was not tested here.
Merely installing 0.3.36 does not supply that native revision.
The first trial pairing 0.3.35 with the new native library failed because its
model-parameter struct lacks the native `lazy_mode` field; do not substitute
a new DLL into that older wrapper.

The generated wheel carries native MIT license text, source/build identities,
and native-library hashes. Source preflights verify those hashes before
packaging. Frozen probes check identity, inventory and ABI while allowing
signing to change native bytes; the existing final bundle-signature gate remains
required. Both packagers run `--embedding-runtime-probe` before promotion.

The model catalog pins Unsloth revision
`ba3888272494be64ed88c9eb536ddc61a1be73d5`, BF16 text GGUF size 557,950,240 bytes,
SHA-256 `f315cbbb30dd487e44d501c8902abe88808755e43753a96beed1964f0a48aa4f`.
The approved isolated download passed the catalog's size and SHA-256 checks.
Weights and build artifacts remain under ignored `source/tests/tmp`; they are
not application model-cache contents or release assets.
Configuration follows the [Google model card](https://ai.google.dev/gemma/docs/embeddinggemma/model_card_2)
and [Unsloth text runtime guide](https://unsloth.ai/docs/models/embeddinggemma-2).

The project-wheel real-model synthetic library smoke resolved the original `revision-1`
and 0–3-second offset, respected an excluding metadata filter, and terminated
and reaped a long native query about 288 ms after invocation (cancellation was
requested at 200 ms). Temporary canonical bundles were cleaned up. This proves
source resolution and cancellation; the fixture has no playable recording.

A temporary Windows PyInstaller 6.22.3 onedir executable built from the real
`snipvoice.pyw` entrypoint passed both summary and embedding probes. Its windowed
embedding worker loaded the actual pinned model, returned a normalized
768-dimensional vector over inherited pipes, and cancelled/reaped a long query
in about 310 ms. This is frozen worker/model-path proof, not a release installer
or desktop/capture test. The native transcription/resampler lane passed 16 tests
without skips, and the summary worker loaded after the transcription GGML
runtime. A 140,768-byte untrained synthetic GGUF exercised the actual summary
model constructor, native generation, and constrained JSON grammar; it establishes
API compatibility rather than summary quality and was cleaned up after the smoke.

Pending: an approved private sample outside Git/logs, calibrated unrelated-query
behavior, representative library/hardware RSS and latency, concurrent capture
impact, interactive Windows/macOS navigation and physical playback, macOS
worker/model discovery, complete platform bundles/installers and live CI.
No latency budget or release-readiness
claim is made. Synthetic-vector regression tests establish indexing/navigation
contracts separately from the real-model quality measurements above.
