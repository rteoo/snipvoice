# EmbeddingGemma 2: semantic meeting retrieval

Date: 2026-10-06. Status: proposed implementation; documentation only.

## Outcome and existing architecture

Find a relevant meeting passage from a paraphrased question and open its recording at the correct timestamp. Keep exact keyword and metadata searches available. `source/meeting_index.py` already owns SQLite FTS5/BM25 retrieval with session, revision, and segment references. Integrate with that contract rather than replacing the meeting library.

## Implementation milestones

1. **Benchmark first.** Create synthetic transcript fixtures and 40 held-out PT-BR queries covering paraphrases, exact names, dates, negation, and unrelated questions. Record BM25 Recall@5, MRR@10, and query latency. Separately obtain an approved private sample; keep it outside Git and logs.
2. **Runtime adapter.** Add a bounded, cancellable local embedding adapter. Start with the text-only encoder, 768 dimensions, correct query/document prefixes, and L2 normalization. Reject malformed or non-finite vectors. Model installation is explicit and hash verified; automatic processing uses installed models only. Resolve the cache through existing model-location mechanics rather than inventing an untracked location.
3. **Derived index.** Store vectors separately from authoritative meeting data. Key entries by session, transcript revision, segment, source hash, model revision, dimension, and preprocessing version. Index completed transcript segments in background jobs; changed revisions become stale and are rebuilt atomically. Respect existing path, lock, relocation, and cancellation safeguards.
4. **Hybrid retrieval.** Apply existing metadata filters and eligibility rules before ranking candidates. Combine BM25 and semantic rankings with reciprocal rank fusion; benchmark fusion settings on development queries only. Return original source references and snippets, deduplicate overlapping hits, and distinguish transcript evidence from summaries.
5. **Library integration.** Add an opt-in semantic mode and index status using existing GUI conventions. Search, indexing, inference, downloads, and disk work stay off GUI/hotkey threads. Results open the exact transcript revision and audio offset. Runtime failures visibly leave keyword search usable.

## Acceptance and checks

- Proposed quality gate: improve Recall@5 by at least 10 percentage points on paraphrase queries, without lowering exact-query Recall@5 by more than 2 points. Report small-sample uncertainty and failures; these targets are not measured results.
- Verify removed/inaccessible meetings, revision changes, relocation, cancellation, malformed vectors, process interruption, and offline/runtime failure.
- Measure cold/warm p50/p95 latency, peak RSS, index size, and background indexing impact on representative hardware before setting a latency budget.
- Run focused `test_meeting_index` coverage and the required suite from `source`: `python -m unittest discover -s tests -v`; run `python -m ruff check source` with the existing approved tool.
- Verify the real library flow on Windows and macOS, including packaged model discovery. Tests clean up temporary indexes and processes on failure.

## Rollout and boundaries

Ship behind an opt-in setting; keep the source recordings and transcripts authoritative. Disable semantic retrieval to roll back; index cleanup is a separate explicit action. Audio embeddings and fine-tuning are later experiments, not prerequisites. Dependency changes, model acquisition, publication, and release require their own authorization. No benchmark or runtime check has run as part of this plan.

## References

- [Google model card](https://ai.google.dev/gemma/docs/embeddinggemma/model_card_2)
- [Unsloth runtime guide](https://unsloth.ai/docs/models/embeddinggemma-2)
