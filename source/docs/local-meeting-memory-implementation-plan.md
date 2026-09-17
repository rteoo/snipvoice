# Local meeting memory implementation plan

Status: implementation record, 2026-09-17. This document began as a plan
against `1e875d2` (`v3.1.0`). The checkpoints below now describe the
implemented local meeting-memory slice; they do not certify physical or
packaged acceptance.

This plan implements the first seven recommendations from the
[notetaker product benchmark](notetaker-product-benchmark.md): report profiles,
post-meeting artifacts, transcript interaction, single-meeting Q&A, local
organization, cross-meeting search/Q&A, and privacy/retention controls.

No dependency or host configuration change is part of this implementation
record. Never run its storage, rebuild, or retention checks against live
Snipvoice data; use copied fixtures and an isolated `SNIPVOICE_HOME`.

## Implementation snapshot

The following status separates implemented behavior from validation that still
requires a physical host or release environment:

| Area | Status | Evidence boundary |
| --- | --- | --- |
| Canonical bundles, versioned workspace/annotations, disposable SQLite/FTS catalog, and library routing | Implemented | Canonical files remain authoritative; SQLite/FTS5 is rebuildable derived state. |
| Profiles, bounded local reports, immutable report history, reviewed artifacts, and single-meeting Q&A | Implemented | Reports and answers carry validated transcript citations and model provenance. |
| Transcript revisions, manual speaker labels, revision-scoped highlights, navigation, and bounded source clips | Implemented | Labels and clips retain revision/segment/track provenance; timings are chunk-level. |
| Collections, tags, people, and manual series | Implemented | GUI filters and bounded organization assignment use canonical annotations. |
| Search projection, provenance snippets, cross-meeting Q&A, and rebuild/repair UI | Implemented | Search and Q&A remain bounded and local; rebuild never deletes canonical bundles. |
| Consent/privacy controls, retention previews, trash, restore, raw-track staging, and permanent purge | Implemented | Destructive actions are explicit, journaled, fail-closed, and subject to SSD limits. |
| Hotkey/tray/controller integration and startup recovery | Implemented | Callback/thread routing is covered by tests; physical Tk/tray behavior is unverified. |
| Package probes, native capture, packaged startup, signing, and physical acceptance | Open validation | These are release/environment checks, not missing local-memory features. |

The implementation is therefore usable as a local development surface. On
2026-09-17, `python -m unittest discover -s tests -q` ran 1,191 tests
successfully with 53 environment/platform skips. Ruff is unavailable in this
validation context. Physical Tk/tray,
native capture, packaged startup, signing, migration/downgrade, and long-run
scale results must be reported separately with their actual skips and limits.

## Checkpoint disposition

Checkpoints 1–22 are implemented in the current source and have focused unit
coverage across the canonical bundle, index, intelligence, GUI/controller,
organization, export, consent, and retention seams. Checkpoint 23's repair,
export, and documentation integration is also present, including the
`--sqlite-runtime-probe` entry point. The remaining validation boundary is
physical Tk/tray/native capture, packaged startup, trusted signing, and the
synthetic scale/migration checks; these are not described as passed here.

## Product outcome

Ship a local meeting-memory workspace that keeps capture, transcription,
storage, retrieval, reporting, and question answering usable with networking
disabled after models are installed.

The work preserves Snipvoice's existing capture and recovery guarantees. It
adds structure above the recording layer without making a database, a model, or
the GUI part of the capture-critical path.

## Scope

1. Built-in and custom report profiles, used as reusable local recipes.
2. Reviewable follow-up email drafts, decisions, actions, questions, risks,
   objections, and feedback artifacts.
3. Transcript navigation, manual speaker labels, highlights, and bounded WAV
   clips.
4. Local `Ask this meeting` with transcript citations.
5. Local folders, tags, projects, people labels, and manually assigned recurring
   meeting groups.
6. Cross-meeting full-text search and cited local Q&A.
7. Visible consent support, retention previews, recoverable deletion, and
   explicit raw-audio removal.

The first implementation does not include automatic diarization, embeddings,
calendar auto-join, meeting bots, cloud sharing, CRM/email mutation, automatic
task execution, sentiment scoring, MCP, or a local network server.

## Confirmed baseline

- `MeetingStore` owns an append-only CRC-checked journal, separate native PCM
  tracks, atomic metadata, startup recovery, and append-only transcript JSONL.
- `MeetingController` keeps capture, processing, file work, and playback off Tk
  and keyboard callback threads.
- `meeting_summary.py` performs bounded hierarchical reduction through the
  packaged llama.cpp runtime, validates citations, and preserves the previous
  summary on failure.
- `meeting_gui.py` already pages the library, selects transcript chunks, seeks
  playback to a chunk start, edits notes/bookmarks, and exposes summary review.
- `MeetingLibrary` routes user-facing meeting reads and writes through canonical
  bundles and the disposable catalog. `MeetingIndex` provides SQLite/FTS5
  search, bounded snippets, filters, cursors, and rebuild/reconciliation; a
  canonical scan remains the safe fallback when the index is unavailable.
- `MeetingIntelligence` provides bounded local reports and single- and
  cross-meeting answers with validated transcript citations. The GUI exposes
  profiles, report history, Q&A, organization, search, repair, consent, and
  retention controls through worker-backed controller seams.
- Focused tests cover these seams. The 2026-09-17 full run completed 1,191
  tests successfully with 53 environment/platform skips; this record is not a
  physical-capture or packaged-acceptance certification.

## Architecture decision

Use canonical meeting bundles plus a rebuildable SQLite/FTS catalog.

```text
SNIPVOICE_HOME/
├── settings.json
├── workspace.json
├── library.sqlite
├── library.sqlite-wal          # present only while SQLite needs it
├── library.sqlite-shm
├── retention-ops/
├── trash/
└── meetings/
    └── <session-id>/
        ├── metadata.json
        ├── annotations.json
        ├── events.journal
        ├── microphone/*.pcm
        ├── system/*.pcm
        ├── transcripts/*.jsonl
        └── reports/*.json
```

### Sources of truth

| Data | Canonical location | Notes |
| --- | --- | --- |
| Capture state, track projection, processing revisions | `metadata.json` | Existing atomic file and recovery behavior remain authoritative |
| Audio event provenance | `events.journal` | Existing valid-prefix recovery remains authoritative |
| Native audio | Track PCM segments | Never store audio blobs in SQLite |
| ASR output | Transcript revision JSONL | Append-only; generated revisions are never edited in place |
| Human-owned meeting state | `annotations.json` | Title, notes, bookmarks, highlights, speaker labels, organization, reviewed artifacts, retention override |
| Generated reports | `reports/<report-id>.json` | Generated payload remains immutable; review is stored separately in the report envelope or annotations |
| Folder/profile/policy definitions | `workspace.json` | Versioned and atomically replaced |
| Listing/search projection | `library.sqlite` | Contains sensitive plaintext but no irreplaceable state; deleting it and rebuilding must be safe |
| Unsaved Q&A | Memory only | Persist only through an explicit `Save answer` action |

### Deep modules and seams

`MeetingStore` remains the capture/recovery module. Do not route real-time audio
through SQLite.

Add a `MeetingLibrary` module as the sole interface for user-facing meeting
data. It composes `MeetingStore`, canonical sidecar files, workspace state, and
the index. Callers may request sessions, search, annotations, reports, folders,
or retention plans without knowing which file or SQL table supplies them.

`MeetingIndex` is an internal module with a SQLite implementation and temporary
SQLite test databases. Its interface is not exposed to GUI code. Index failure
must not roll back a successful canonical file write.

`MeetingIntelligence` owns evidence packing, local-model execution, schema
validation, citation validation, hierarchical reduction, reports, and answers.
Keep `summarize_meeting()` as a compatibility wrapper until all callers move to
the deeper interface.

`MeetingRetention` owns dry-run planning, exact target resolution, operation
journals, trash moves, restore, and permanent purge. It never receives a broad
filesystem path from the GUI.

### Canonical terms

- **Meeting bundle:** one app-owned directory containing a recording and all
  durable material derived from it.
- **Transcript revision:** immutable ASR output for one profile/language run.
- **Annotation:** human-owned information layered over a meeting or transcript
  revision without changing generated source data.
- **Report profile:** a reusable selection of supported report sections plus
  bounded instructions. A custom recipe is represented as a report profile;
  do not add a second recipe model in this release.
- **Report revision:** one generated structured artifact bound to a transcript
  revision, profile version, and exact model artifact.
- **Reviewed artifact:** user-edited output derived from a report revision. It
  never overwrites the generated payload.
- **Collection:** a user-created folder or project grouping. Tags remain
  lightweight labels; a recurring-meeting group is a manually assigned series.
- **Catalog index:** the disposable SQLite projection used for listing,
  filtering, snippets, and retrieval.
- **Retention plan:** an immutable preview of exact app-owned targets and
  capability loss, produced before any destructive operation.

## Data formats and compatibility

### `annotations.json`

Use a separately versioned atomic object with an integer generation for
compare-and-swap updates:

```json
{
  "schema_version": 1,
  "generation": 4,
  "title": "Weekly product review",
  "notes": "Human-authored notes",
  "bookmarks": [],
  "highlights": [],
  "speaker_labels": {},
  "collection_ids": [],
  "tags": [],
  "people": [],
  "series_id": null,
  "reviewed_artifacts": {},
  "retention_override": null,
  "updated_at": "2026-09-16T12:00:00Z"
}
```

Speaker and highlight references include both transcript revision ID and stable
segment ID. A new transcript revision does not silently inherit labels from an
older revision.

When `annotations.json` is absent, project the existing `metadata.json` title,
notes, bookmarks, and `reviewed_summary` as legacy annotations. Create the
sidecar only on the first user-owned mutation. Once present, the sidecar wins.
Do not rewrite all existing meetings at startup.

Downgrading to an older Snipvoice build may show stale legacy annotations, but
the older build must leave unknown sidecars untouched. Reinstalling the newer
build restores the canonical sidecar view. Document this limitation before
release.

### `reports/*.json`

Each report envelope includes:

- schema version and report ID;
- report kind and profile ID/version;
- source session and transcript revision IDs;
- exact model ID, hash, runtime, and context limit;
- generated structured payload;
- transcript citations for every factual section;
- creation status, timestamps, and cancellation/failure state when retained;
- optional reviewed artifact with its own edit generation; and
- no local absolute paths.

Report IDs and filenames use the existing safe identifier rules. Save through
atomic replacement. Regeneration creates a new report rather than modifying a
prior generated payload.

### `workspace.json`

Persist only global meeting-workspace definitions: custom report profiles,
collections, series definitions, privacy defaults, and retention defaults.
Preserve unknown keys when writing. Unknown future schema versions open
read-only with an actionable error; they are never downgraded or rewritten.

### `library.sqlite`

Use Python's standard-library `sqlite3`; adding a new dependency is not part of
this plan. Keep the database under `SNIPVOICE_HOME` so its plaintext index is
covered by the same privacy and deletion expectations as transcripts.

Minimum projection:

- sessions: identity, status, dates, duration, active transcript/report IDs,
  annotation generation, and content fingerprint;
- transcript segments: session, revision, segment, timing, track, manual
  speaker projection, and text;
- report sections: report/profile identity, kind, reviewed/generated state,
  and text;
- collection/tag/people/series membership;
- FTS5 rows with source kind and stable canonical identifiers; and
- index metadata using `PRAGMA user_version` plus build/reconciliation state.

Use one SQLite connection per worker operation or another explicitly
thread-confined connection policy. Configure a bounded busy timeout. SQLite
WAL/checkpoint work stays off Tk and capture threads.

Official packaged Python runtimes must prove FTS5 availability. If FTS5 is
absent or the database is corrupt, core capture and direct meeting access remain
available through canonical files, and the UI reports that search is rebuilding
or unavailable. Release acceptance for feature 6 requires working FTS5.

## Ordered implementation checkpoints

Each checkpoint is one implementation commit unless a failing checkpoint needs
its own corrective commit. Never merge a checkpoint whose stated validation is
failing or unresolved.

### 1. Lock data contracts and legacy fixtures — implemented

**Changes**

- Add an ADR recording the approved hybrid decision and a short domain glossary
  using the canonical terms above.
- Add immutable test fixtures representing a current schema-1 meeting with raw
  tracks, transcript revision, summary, reviewed summary, notes, and bookmarks.
- Add tests that copy fixtures into `source/tests/tmp`; tests never open the
  user's live `SNIPVOICE_HOME`.
- Add a packaged/runtime capability probe for `sqlite3` and FTS5 without opening
  or creating the production database.

**Files**

- `source/docs/adr/0001-hybrid-meeting-library.md`
- `source/CONTEXT.md`
- `source/tests/fixtures/meeting-v1/**`
- `source/tests/test_meeting_library.py`
- packaging/runtime-probe tests only if the existing probe seam can host the
  check without activating recording devices

**Validation**

- Fixture bytes are unchanged after read-only inspection.
- Current `MeetingStore` opens the fixture.
- The active and staged packaged interpreters report SQLite and FTS5 capability.
- Existing meeting tests remain green.

**Completion criterion:** the old format, new terms, FTS requirement, and
rollback expectation are explicit and executable as tests before storage code
changes.

### 2. Add versioned workspace and annotation storage — implemented

**Changes**

- Add `MeetingLibrary` with atomic, versioned readers/writers for
  `workspace.json` and `annotations.json`.
- Merge absent sidecars with legacy metadata fields at read time.
- Use annotation generations for conditional writes so a stale GUI save cannot
  overwrite a background title/refinement or another completed edit.
- Validate sizes, IDs, timestamps, collection references, speaker maps,
  highlights, and retention overrides before writing.
- Preserve unknown fields within a known schema.

**Files**

- new `source/meeting_library.py`
- `source/meeting_store.py` only for narrow helpers that belong to bundle path
  validation
- new `source/tests/test_meeting_library.py`

**Validation**

- Legacy sessions read identically without creating files.
- First mutation creates one valid sidecar atomically.
- Disk-full/replace failure preserves the prior canonical state.
- Stale generation writes fail with a conflict result rather than overwriting.
- Unknown schema versions remain byte-identical and read-only.
- Symlink/junction and traversal attempts cannot redirect writes.

**Completion criterion:** human-owned state has one safe canonical write path,
and existing meetings still open without eager migration.

### 3. Add the rebuildable SQLite catalog — implemented

**Changes**

- Add an internal `MeetingIndex` module with schema creation, transactionally
  indexed session projections, FTS5 rows, removal, and full rebuild.
- Fingerprint canonical source generations/revision IDs so startup
  reconciliation indexes only changed meetings.
- Build and reconcile on a background worker with cancellation and bounded
  batches.
- Treat the database and its WAL/SHM files as disposable derived state.

**Files**

- new `source/meeting_index.py`
- `source/meeting_library.py`
- new `source/tests/test_meeting_index.py`

**Validation**

- Deleting `library.sqlite*` and rebuilding yields identical query projections.
- Corrupt, old, partially created, and read-only databases never damage meeting
  bundles.
- An index transaction failure after an annotation commit leaves the canonical
  edit visible and marks/reconciles the index stale.
- Rebuild cancellation leaves either the prior usable index or an explicitly
  incomplete disposable replacement.
- Synthetic 10,000-meeting/250,000-segment indexing stays bounded in memory;
  record build time and first-page latency on the Windows reference host.

**Completion criterion:** the index can be destroyed at any point without
losing user data, and rebuilding converges to canonical files.

### 4. Route user-facing library operations through `MeetingLibrary` — implemented

**Changes**

- Construct one `MeetingLibrary` beside the existing `MeetingStore` in
  `snipvoice.pyw`.
- Route library list/get/update/delete, transcription completion, report
  completion, and session-finalization projections through its interface.
- Retain `MeetingStore` directly inside the capture path.
- Use the existing directory reader as a compatibility fallback while a missing
  index rebuilds; do not present stale indexed content as current.
- Keep GUI and controller code unaware of SQL or sidecar paths.

**Files**

- `source/snipvoice.pyw`
- `source/meeting_support.py`
- `source/meeting_library.py`
- `source/meeting_gui.py`
- controller, entrypoint, and standalone tests

**Validation**

- Capture does not open SQLite from its audio event loop.
- Existing listing, notes, bookmarks, deletion, import, transcription,
  summary, export, and recovery tests pass through the new interface.
- Missing/rebuilding index still lists canonical meetings.
- Index failure cannot fail recording finalization.
- Tk and keyboard callbacks remain bounded and free of disk/model work.

**Completion criterion:** every non-capture caller uses the new deep module, and
the legacy user-visible behavior is unchanged.

### 5. Define report profiles and supported sections — implemented

**Changes**

- Define built-in General, One-on-one, Interview, Sales, Customer Feedback,
  Project Update, and Retrospective profiles.
- Represent custom recipes as custom report profiles stored in
  `workspace.json`.
- Limit profiles to supported section types: summary, decisions, action items,
  open questions, risks, objections, feedback, and follow-up email.
- Bound profile name, instructions, selected sections, output counts, and
  lengths. Profiles may guide analysis but cannot replace the system evidence
  and citation rules.
- Version profiles so report history remains explainable after edits.

**Files**

- new `source/meeting_intelligence.py`
- `source/meeting_library.py`
- new `source/tests/test_meeting_intelligence.py`
- `source/tests/test_meeting_library.py`

**Validation**

- Built-ins validate deterministically in Portuguese and English configurations.
- Malformed, oversized, duplicate, unknown-section, and future-version profiles
  fail without modifying `workspace.json`.
- Custom profile edits create a new profile version/hash; existing reports keep
  their original identity.
- Profile text is never executed as code or passed to a shell.

**Completion criterion:** every requested artifact can be expressed through one
bounded profile model without arbitrary output schemas.

### 6. Deepen local structured generation — implemented

**Changes**

- Move transcript validation, evidence packing, hierarchical reduction, model
  lifecycle, cancellation, and citation validation behind
  `MeetingIntelligence`.
- Generate only sections requested by the selected profile.
- Require citations for factual sections and the factual basis of follow-up
  drafts. Unknown owners and deadlines remain null.
- Retain `meeting_summary.summarize_meeting()` as a compatibility wrapper that
  requests the General profile and returns the legacy shape expected by current
  callers/tests.
- Keep installed-only model preparation and outbound-network-free inference.

**Files**

- `source/meeting_intelligence.py`
- `source/meeting_summary.py`
- `source/meeting_support.py`
- summary/intelligence tests

**Validation**

- Existing summary tests continue to pass unchanged or through a narrow
  compatibility assertion.
- Invalid JSON, invented segment IDs, invented owners/deadlines, oversized
  output, cancellation, runtime-close failure, and prompt injection in
  transcript/profile text preserve the prior report.
- Large transcripts remain bounded through hierarchical reduction.
- No model is downloaded or network opener invoked.

**Completion criterion:** reports and later Q&A share one tested inference
module without weakening existing summary guarantees.

### 7. Persist report revisions and reviewed artifacts — implemented

**Changes**

- Save each successful generation as a new atomic report envelope under
  `reports/`.
- Preserve generated payloads; review edits create/update the separate reviewed
  artifact with generation checks.
- Expose report history and active-report selection through `MeetingLibrary`.
- Project existing `summary`/`reviewed_summary` metadata as a legacy virtual
  report until explicitly regenerated.
- Index successful report sections only after the canonical file commits.

**Files**

- `source/meeting_library.py`
- `source/meeting_intelligence.py`
- `source/meeting_files.py`
- library/intelligence/export tests

**Validation**

- Regeneration preserves prior reports and human review.
- Failed generation or save leaves the prior active report unchanged.
- Stale review generations report a conflict.
- Export distinguishes generated and reviewed text and contains profile/model/
  transcript provenance without absolute paths.
- Deleting/rebuilding SQLite preserves all report history.

**Completion criterion:** multiple report profiles and revisions coexist without
overwriting source or human work.

### 8. Add report profile and history UI — implemented

**Changes**

- Replace the single `Gerar resumo local` action with a profile selector and
  `Generate report` action while retaining the General default.
- Add profile management for create, duplicate, edit, disable, and delete.
- Show active report provenance, prior revisions, generated sections, review
  state, and actionable validation errors.
- Run all storage/model work through existing background lanes; marshal only
  bounded projections to Tk.

**Files**

- `source/meeting_gui.py`
- `source/meeting_support.py`
- `source/tests/test_meeting_gui.py`
- `source/tests/test_meeting_support.py`

**Validation**

- GUI logic covers selection, stale edits, cancellation, failed persistence,
  switching meetings with unsaved review, and history navigation.
- Window smoke verifies the real shared Tk root when Tcl is available.
- Long reports are paged/truncated for display without truncating canonical
  files.

**Completion criterion:** a user can create and review reports from built-in or
custom profiles without blocking Tk or losing earlier output.

### 9. Add reviewable post-meeting outputs — implemented

**Changes**

- Render decisions, actions, questions, risks, objections, feedback, and
  follow-up email as distinct reviewed sections.
- Add copy-to-clipboard and atomic Markdown/plain/JSON export for selected
  reviewed sections.
- Keep external effects manual: no sending, scheduling, CRM writes, or task
  creation.
- Preserve citations in exports; optionally render human-readable timestamp
  references beside stable IDs.

**Files**

- `source/meeting_gui.py`
- `source/meeting_files.py`
- `source/clipboard_support.py` only through its existing safe interface
- GUI/file/clipboard tests

**Validation**

- Clipboard restoration behavior remains unchanged.
- Export failure preserves an existing destination.
- Drafts never become external messages automatically.
- Unknown owner/deadline fields remain visibly unknown.
- Every factual exported item resolves to the source transcript revision and
  segment.

**Completion criterion:** feature 2 produces useful artifacts without granting
Snipvoice external mutation authority.

### 10. Add transcript annotation storage — implemented

**Changes**

- Store manual speaker labels and highlights in `annotations.json`, scoped to a
  transcript revision and stable segment IDs.
- A highlight includes an ID, transcript revision, start/end, source track,
  label, optional note, and cited segment IDs.
- Keep original transcript JSONL immutable.
- Define explicit behavior for superseded transcript revisions: retain old
  annotations, display them with the old revision, and never remap silently.

**Files**

- `source/meeting_library.py`
- `source/tests/test_meeting_library.py`

**Validation**

- Invalid time ranges, tracks, revision/segment IDs, duplicate IDs, oversized
  labels, and stale generations fail atomically.
- Reprocessing preserves old annotations without applying them to new segments.
- Index rebuild projects the active revision's labels/highlights only while
  retaining older canonical annotations.

**Completion criterion:** human transcript corrections exist as reversible
overlays with stable provenance.

### 11. Add synchronized transcript navigation and highlight UI — implemented

**Changes**

- Page transcript segments instead of retaining only the current 500-segment
  hard limit.
- Preserve click-to-seek and add bounded playback progress events from the audio
  worker through controller state to Tk.
- Select the active transcript row as playback crosses chunk boundaries. Label
  timing honestly as chunk-level.
- Add manual speaker-label and highlight create/edit/delete controls.
- Search-result activation opens the meeting, transcript revision, segment, and
  timestamp.

**Files**

- `source/meeting_gui.py`
- `source/meeting_support.py`
- `source/meeting_files.py`
- GUI/controller/file tests

**Validation**

- Fake-clock playback tests cover seeks, gaps, pause/stop, track changes, stale
  callbacks, and window close.
- Tk receives no calls from playback workers.
- Long transcripts remain bounded and navigable.
- Manual labels never imply biometric identity or modify ASR source text.

**Completion criterion:** transcript, playback, annotations, and search results
share stable revision/segment/timestamp navigation.

### 12. Add bounded highlight clip export — implemented

**Changes**

- Extend bounded audio readers with an exclusive end time.
- Export a selected highlight from one source track as atomic PCM16 WAV with
  session, track, start/end, gap, and annotation provenance.
- Do not overwrite existing files or alter the raw recording.
- Defer mixed and compressed highlight export until source-track clips pass
  physical playback validation.

**Files**

- `source/meeting_files.py`
- `source/meeting_gui.py`
- `source/tests/test_meeting_files.py`
- GUI tests

**Validation**

- Exact-start/end, partial-block trim, silence gaps, format changes, 4 GiB WAV
  limit, cancellation, and destination failure tests.
- Exported sample count matches the requested bounded interval.
- Provenance omits private absolute paths.

**Completion criterion:** feature 3 can export a traceable source clip without
changing or loading the full recording.

### 13. Implement local `Ask this meeting` — implemented

**Changes**

- Add a bounded question request to `MeetingIntelligence` using only the
  selected transcript revision.
- Return `{answer, citations, uncertainty}` with citations validated against
  supplied segment IDs.
- Use hierarchical evidence processing when the transcript exceeds context;
  preserve evidence gaps and refuse unsupported certainty.
- Treat transcript and question text as data, never executable instructions.
- Keep answers in memory by default.

**Files**

- `source/meeting_intelligence.py`
- `source/meeting_support.py`
- intelligence/controller tests

**Validation**

- Empty, oversized, cancelled, multilingual, unanswerable, prompt-injected,
  and citation-inventing cases.
- A model/runtime/save failure does not modify the meeting.
- Network openers are not called and the model remains installed-only.
- Answers cite the selected transcript revision, not a stale summary.

**Completion criterion:** a question can be answered locally with resolvable
evidence and no durable write.

### 14. Add Q&A UI and explicit answer saving — implemented

**Changes**

- Add an `Ask this meeting` panel with question, cancel, answer, citations, and
  source-jump controls.
- Keep answer history session-memory-only.
- `Save answer` creates a report revision of kind `qa`; closing without saving
  leaves no transcript/question/answer artifact on disk or in logs.

**Files**

- `source/meeting_gui.py`
- `source/meeting_support.py`
- GUI/controller tests

**Validation**

- Meeting switching, close, cancellation, stale completion, explicit save,
  failed save, and citation jumps.
- Logs and error messages contain no question, answer, or transcript content.
- GUI remains responsive during long inference.

**Completion criterion:** feature 4 is useful without silently accumulating a
new private chat history.

### 15. Add collections, tags, people, and manual series — implemented

**Changes**

- Add versioned workspace definitions for collections and series.
- Store meeting memberships, tags, and people labels in annotations.
- A collection may be a folder or project; avoid separate overlapping storage
  models. A meeting may belong to multiple collections and at most one manual
  recurring series in the first release.
- Add create/rename/archive/delete collection behavior. Deleting a definition
  removes membership references only after preview and confirmation; it never
  deletes meetings.

**Files**

- `source/meeting_library.py`
- library tests

**Validation**

- Duplicate names/IDs, Unicode normalization, archived definitions, missing
  references, stale generations, and definition deletion.
- Rebuild indexes memberships from canonical workspace/annotation files.
- A workspace-write failure leaves every meeting annotation unchanged.

**Completion criterion:** feature 5 has one coherent organization model without
calendar or contact ingestion.

### 16. Add organization and filtered-library UI — implemented

**Changes**

- Add collection, tag, people, and series filters to the library.
- Add per-meeting assignment and bounded batch assignment for selected meetings.
- Replace deep offset paging internally with an index cursor while retaining a
  compatibility wrapper for existing controller callers.
- Show indexing/reconciliation state without blocking ordinary meeting access.

**Files**

- `source/meeting_gui.py`
- `source/meeting_support.py`
- `source/meeting_library.py`
- GUI/controller/library tests

**Validation**

- Combined filters, empty results, archived collections, cursor invalidation
  after edits, cancelled batch changes, and partial index availability.
- No Tk callback performs SQL or file enumeration.
- Current status filtering remains compatible.

**Completion criterion:** users can organize and retrieve meetings without
learning storage or index details.

### 17. Complete full-text search with provenance snippets — implemented

**Changes**

- Search titles, notes, tags, people labels, transcript segments, generated
  reports, and reviewed artifacts through FTS5.
- Return bounded snippets plus stable source kind/session/revision/segment/report
  identifiers.
- Weight transcript evidence separately from generated/reviewed artifacts.
- Keep the existing case-insensitive canonical scan only as a rebuild fallback,
  not as the normal search implementation.

**Files**

- `source/meeting_index.py`
- `source/meeting_library.py`
- `source/meeting_gui.py`
- index/library/GUI tests

**Validation**

- Portuguese accents, English, punctuation, quoted phrases, empty terms,
  malformed FTS syntax, tags, and combined filters.
- Search snippets never break Unicode or disclose absolute paths.
- Deleted/retained data disappears from results after the canonical operation;
  rebuilding produces the same result set.
- On the 10,000-meeting/250,000-segment fixture, first-page searches complete
  within one second on the Windows reference host after indexing; record cold
  and warm measurements rather than hiding regressions.

**Completion criterion:** feature 6 search is fast, source-aware, rebuildable,
and independent of generated summaries.

### 18. Add cited cross-meeting Q&A — implemented

**Changes**

- Retrieve bounded transcript candidates from the index, filtered by selected
  collections/tags/people/series/date ranges.
- Generate answers from transcript segments, with citations containing session,
  transcript revision, segment, and timestamp.
- Use report/notes hits only to improve candidate discovery; when transcript
  evidence exists, do not present generated reports as primary evidence.
- Cap meetings, segments, bytes, model context, output, and concurrent jobs.
- Defer embeddings. Add them only after a separate measured retrieval decision.

**Files**

- `source/meeting_intelligence.py`
- `source/meeting_library.py`
- `source/meeting_support.py`
- `source/meeting_gui.py`
- intelligence/library/GUI tests

**Validation**

- Multi-meeting conflicts, duplicate claims, stale revisions, deleted meetings,
  sparse evidence, prompt injection, cancellation, and unanswerable questions.
- Every citation opens the exact canonical meeting/revision/segment.
- PT-BR and English fixture questions measure retrieval recall separately from
  generation quality.
- Inference works with outbound networking blocked.

**Completion criterion:** cross-meeting answers remain bounded, local, and
auditable back to transcripts.

### 19. Add explicit consent and privacy settings — implemented

**Changes**

- Add an optional pre-recording consent reminder with copyable PT-BR/English
  notice text.
- Preserve a continuously visible local recording state in the app/tray; do not
  add silent or always-on recording.
- Add workspace privacy defaults for raw-audio retention, recoverable trash,
  saved Q&A behavior, and recording notice.
- Explain that local storage and deletion do not imply forensic SSD erasure.

**Files**

- `source/meeting_gui.py`
- `source/meeting_support.py`
- `source/meeting_library.py`
- settings/UI tests

**Validation**

- Reminder enabled/disabled, cancellation before capture, language selection,
  copy failure, hotkey start, and tray start behavior.
- A reminder never records attendee data or blocks capture after the user's
  explicit confirmation.
- Settings preserve unknown future keys.

**Completion criterion:** feature 7 begins with transparent, configurable local
recording behavior rather than hidden automation.

### 20. Implement retention planning without deletion — implemented

**Changes**

- Add `MeetingRetention.plan()` using an injected clock and exact canonical
  inventory.
- Support policies for keep indefinitely, whole-meeting age, raw-track age after
  a reviewed transcript/report exists, and per-meeting override.
- A plan lists exact app-owned paths, byte estimates, canonical/index changes,
  lost capabilities, excluded external exports, and recovery mode.
- Reject raw-audio removal if transcription is missing/incomplete, citations
  cannot remain resolvable, or a processing/playback/capture lease is active.

**Files**

- new `source/meeting_retention.py`
- `source/meeting_library.py`
- new `source/tests/test_meeting_retention.py`

**Validation**

- Time zones, clock rollback, interrupted sessions, pending revisions, failed
  reports, externally exported final audio, symlinks/junctions, missing files,
  active leases, and policy overrides.
- Dry runs are byte-for-byte non-mutating.
- Plans cannot contain paths outside the meetings/trash roots.

**Completion criterion:** every destructive feature begins from a deterministic,
reviewable, exact-target plan.

### 21. Add recoverable whole-meeting trash — implemented

**Changes**

- Replace immediate session deletion with an atomic same-root move to app trash
  by default, retaining explicit permanent deletion behind a second confirmation.
- Persist a small tombstone with original session ID, deletion time, size, and
  purge deadline but no transcript content.
- Add restore and empty-trash actions.
- Update the index after the canonical move; index failure cannot undo or hide
  the trash state.

**Files**

- `source/meeting_retention.py`
- `source/meeting_library.py`
- `source/meeting_support.py`
- `source/meeting_gui.py`
- retention/library/controller/GUI tests

**Validation**

- Crash before/after rename, restore conflicts, active recordings, missing
  tombstones, disk errors, expired trash, repeated deletion, and startup
  reconciliation.
- Exported files outside the library remain untouched.
- Permanent purge resolves the exact trash target again and never follows
  links.

**Completion criterion:** whole-meeting deletion is recoverable by default and
converges after interruption.

### 22. Add staged raw-audio removal — implemented

**Changes**

- Execute approved raw-track retention plans through an operation journal under
  `retention-ops/`.
- Stage track directories into operation-owned trash, atomically update the
  meeting projection to mark tracks unavailable, then finalize or roll back on
  restart according to recorded state.
- Preserve transcript revisions, annotations, reports, citations, and event
  provenance while making playback/retranscription/export loss explicit.
- Keep externally exported final audio untouched.

**Files**

- `source/meeting_retention.py`
- `source/meeting_store.py` for explicit purged-track read behavior
- `source/meeting_library.py`
- `source/meeting_files.py`
- `source/meeting_gui.py`
- retention/store/file/GUI tests

**Validation**

- Failure injection at every operation state, restart recovery, partial track
  selection, both tracks, missing bytes, existing trash, read-only files,
  playback/retranscription after purge, and index rebuild.
- No operation truncates journals or rewrites transcript JSONL.
- The UI previews irreversible capability loss and cannot silently retry a
  destructive action.

**Completion criterion:** raw audio can be removed without creating an ambiguous
or unrecoverable meeting state.

### 23. Integrate repair, export, documentation, and release gates — implemented; acceptance open

**Changes**

- Add an explicit `Rebuild search index` action and bounded progress/cancel UI.
- Extend Markdown/plain/JSON exports with selected annotations, report history,
  source citations, and purged-track state while redacting local paths.
- Update README, development, privacy/data-location, migration, downgrade,
  backup, repair, and release-validation documentation.
- Add package probes for SQLite/FTS and new canonical file access without
  activating recording hardware.

**Files**

- `source/meeting_files.py`
- `source/meeting_gui.py`
- runtime/package probes and tests
- `README.md`
- relevant `source/docs/*.md`
- `CHANGELOG.md` only when preparing an authorized release

**Validation**

- Rebuild from deleted/corrupt/stale index.
- Export/import round trip for legacy and new meetings without absolute paths.
- Windows and macOS packages open legacy/new libraries with networking blocked.
- Documentation matches observed behavior and states remaining physical limits.

**Completion criterion:** users and future agents can inspect, migrate, repair,
export, and validate the completed feature set without private-data surprises.

## Dependencies and ordering

```text
1 -> 2 -> 3 -> 4
               ├─> 5 -> 6 -> 7 -> 8 -> 9
               ├─> 10 -> 11 -> 12
               ├─> 15 -> 16 -> 17 -> 18
               └─> 19 -> 20 -> 21 -> 22

6 + 7 -> 13 -> 14
17 + 13 -> 18
9 + 12 + 14 + 18 + 22 -> 23
```

- Checkpoints 1–4 lock the data seam; feature implementation should not start
  before they pass.
- Report persistence must precede Q&A answer saving.
- Transcript annotations must precede highlight UI and clip export.
- The catalog must precede organization filters and cross-meeting retrieval.
- Retention planning must precede every destructive operation.
- Final UI integration remains serial because `meeting_gui.py` is shared.

## Safe implementation delegation

Delegation is useful only after checkpoint 4 locks the interfaces. Give each
agent exact owned files and prohibit shared-file edits unless root assigns an
integration window.

### Independent lanes after checkpoint 4

- **Report/intelligence lane:** checkpoints 5–7 and 13; own
  `meeting_intelligence.py`, `meeting_summary.py`, and their tests. Root handles
  controller/GUI integration.
- **Transcript/file lane:** checkpoints 10 and 12; own annotation validation
  helpers agreed in `MeetingLibrary`, `meeting_files.py`, and focused tests.
- **Index/search lane:** checkpoints 3 and 17 can be delegated together if root
  retains `meeting_library.py`; own `meeting_index.py` and index tests.
- **Retention lane:** checkpoints 20–22; own `meeting_retention.py` and focused
  tests. Root reviews every destructive path and integrates GUI/controller.
- **Independent review lane:** after each phase, inspect migrations, citation
  integrity, thread ownership, path safety, and failure injection without
  editing implementation files.

### Root-owned integration

Root owns `meeting_library.py`, `meeting_support.py`, `meeting_gui.py`,
`snipvoice.pyw`, schema/version decisions, full-suite validation, packaging,
physical-host proof, and final task-owned diff review.

## Compatibility and migration concerns

- Existing schema-1 bundles must open without eager rewriting.
- Sidecars are additive. Older builds ignore them and may show stale legacy
  fields; they must not delete unknown files. Downgrade behavior must be
  documented and exercised against a copied fixture.
- Unknown future bundle/workspace/report schemas open read-only.
- Current JSON/Markdown/plain exports remain readable; new fields are additive.
- Stable session, revision, and segment IDs remain unchanged.
- SQLite is standard-library functionality, but FTS5 must be verified in every
  packaged interpreter and supported OS.
- A search index contains the same sensitive plaintext as transcripts even
  though it is derived; backup, deletion, diagnostics, and support instructions
  must treat it as private.
- Windows antivirus/file locking and SQLite WAL behavior require packaged-host
  testing. Connections cannot leak across cleanup or app shutdown.
- The one-process mutex reduces but does not replace transaction and
  compare-and-swap correctness across app worker threads.
- Raw-audio removal permanently disables playback, retranscription, new clips,
  and audio re-export for the removed tracks. The transcript and reports are not
  substitutes for the original evidence.
- Chunk-level timestamps do not justify word-level highlighting or exact quote
  timing claims.
- Manual speaker labels are user annotations, not biometric identification.
- Follow-up drafts and answers remain untrusted generated text requiring review.

## Risks and edge cases requiring explicit coverage

- Power loss between canonical file commit and index update.
- Power loss during trash move or raw-track staging.
- Disk full during JSON replacement, report save, WAL growth, index rebuild,
  clip export, and retention operations.
- Corrupt/truncated JSON, JSONL, journal, report, annotations, workspace, or
  SQLite files.
- Removed, renamed, disconnected, or externally modified meeting directories.
- Symlink/junction/path traversal attacks at every destructive or export seam.
- Search/index races with transcription, report generation, annotation edits,
  retention, and deletion.
- Stale GUI generations and callbacks after meeting/window changes.
- Reprocessing that changes segment IDs while annotations/reports cite an older
  revision.
- Very long notes, transcripts, report histories, tags, folders, people labels,
  and libraries.
- Portuguese accents, code-switching, names, dates, currency, and conflicting
  statements across meetings.
- LLM prompt injection inside transcripts, notes, questions, and custom profile
  instructions.
- Model cancellation/unload failures retaining the existing voice-resource
  reservation.
- Index or Q&A accidentally preferring generated text over transcript evidence.
- Trash retention conflicting with the user's privacy expectation.
- Permanent deletion on SSDs being mistaken for forensic secure erasure.

## Verification strategy

### Per-checkpoint gates

- Run the focused test modules named by the checkpoint.
- Run `python -m compileall -q source` for new modules and edited imports.
- Run `python -m ruff check source` from the repository root when the pinned
  development tool is available. Ruff is unavailable in this validation
  context, so no lint pass is claimed here.
- Run `git diff --check` and inspect the full task-owned diff.
- Add failure injection for every new canonical write, transaction, migration,
  destructive state, and model lifecycle.

### Phase gates

After checkpoints 4, 9, 14, 18, 22, and 23, run from `source`:

```powershell
python -m unittest discover -s tests -v
```

Retain the Windows/macOS/Linux Python 3.12/3.14 CI matrix, strict native runtime
lane, packaging lanes, and existing capture probes. New SQLite/FTS package probes
must not activate microphone or system-audio capture.

### Data and privacy validation

- Run every migration, rebuild, retention, trash, restore, and purge test on a
  copied fixture under `source/tests/tmp`.
- Prove deleting `library.sqlite*` loses no canonical information.
- Scan logs/errors to confirm they do not contain transcript, question, answer,
  profile instruction, note, or local path content.
- Run capture, transcription, report generation, single-meeting Q&A,
  cross-meeting Q&A, search, rebuild, and export with outbound networking
  blocked after models are installed.
- Verify raw-audio removal deletes only resolved app-owned targets and leaves
  external exports untouched.

### Physical product validation

On supported physical Windows x64 and Apple Silicon macOS hosts:

- Re-run microphone-only, system-only, and simultaneous capture with default
  and manually selected devices.
- Run a two-hour session and measure synchronization, drift, memory, disk use,
  index update behavior, and transcript/report responsiveness.
- Exercise USB/Bluetooth changes, permissions, suspend/resume, app restart,
  disk pressure, cancellation, and recovery.
- Verify transcript/playback synchronization, highlights, clips, search result
  jumps, and stale-window protection.
- Verify package startup, migration, FTS, Q&A, trash/restore, raw purge, repair,
  and cold offline behavior.

## Release definition of done

The source implementation covers the feature set above. Release completion
still requires all of the following evidence:

- Features 1–7 are implemented through the approved hybrid architecture.
- Capture and recovery remain independent of SQLite, GUI, and model latency.
- Existing meetings open without eager migration or data loss.
- Canonical annotations, transcript revisions, reports, and workspace state
  survive index deletion and rebuild.
- Built-in/custom profiles produce bounded cited reports, and human review never
  overwrites generated evidence.
- Transcript navigation, manual labels, highlights, and source clips preserve
  revision/time/track provenance.
- Single- and cross-meeting answers are local, bounded, and cite resolvable
  transcript segments.
- Organization and search work at the synthetic 10,000-meeting/250,000-segment
  gate without unbounded memory or UI blocking.
- Retention always previews exact targets and capability loss; trash/restore and
  raw-audio operations recover after injected interruption.
- No telemetry, implicit upload, cloud fallback, silent model download,
  automatic external mutation, or hidden recording is introduced.
- Focused tests, full unit suite, Ruff, compile, diff, CI, package probes, offline
  tests, migration tests, and physical Windows/macOS acceptance all pass, with
  every skip or platform limit stated rather than called passed.
- README, architecture/data, privacy, migration, downgrade, repair, backup, and
  release-validation documentation match observed behavior.
- The final task-owned diff receives a distinct correctness, privacy, migration,
  destructive-path, and thread-ownership review before any release decision.
