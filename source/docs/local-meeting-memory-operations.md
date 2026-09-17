# Local meeting memory: operations and limits

This note describes the implemented local meeting-memory slice and its
operational limits. It does not certify physical Tk/tray interaction, native
capture, packaged startup, or signing on this host. Never use a user's live
meeting directory as a test fixture; copy it to an isolated workspace first.

## Where data lives

`SNIPVOICE_HOME` is the canonical root. It defaults to `~/.snipvoice` and is
separate from the non-roaming model caches selected by
`SNIPVOICE_VOICE_CACHE` and `SNIPVOICE_SUMMARY_CACHE`. Snipvoice does not
implicitly migrate `~/.sniptype` data.

The meeting root contains one app-owned bundle per session:

```text
SNIPVOICE_HOME/
├── workspace.json
├── library.sqlite (+ -wal/-shm while SQLite is active)
├── retention-ops/
├── trash/
└── meetings/<session-id>/
    ├── metadata.json
    ├── annotations.json        # created on first human-owned mutation
    ├── events.journal
    ├── microphone/*.pcm
    ├── system/*.pcm
    ├── transcripts/*.jsonl
    └── reports/<report-id>.json
```

Canonical files are authoritative. `metadata.json` and `events.journal` retain
capture/recovery state; native PCM segments retain source audio; transcript
JSONL files are immutable revisions; `annotations.json` owns notes, labels,
highlights, organization, review, and retention overrides; report files retain
generated report envelopes. `workspace.json` owns global profiles, collections,
series, and policy defaults.

`library.sqlite` is a disposable SQLite/FTS5 projection for listing, filters,
snippets, and retrieval. It intentionally contains sensitive plaintext copied
from transcripts, notes, and reports. It is not a safe or sufficient deletion
tool. Deleting the database and its WAL/SHM sidecars must lose only the index;
the canonical bundles remain rebuildable through the library reconciliation
path. Capture and recovery do not open SQLite from their real-time path.

## Local model boundary

Model acquisition is an explicit settings action. Meeting processing accepts an
installed, catalog-validated model and does not download a model implicitly.
After the model is installed, report generation and single-meeting Q&A run
through the local llama.cpp seam; the inference path has no cloud fallback.
Transcript, profile instructions, and questions are untrusted data. Generated
reports and answers must remain bounded and cite resolvable transcript segment
IDs. Unknown owners and deadlines remain unknown. Review generated decisions,
actions, follow-up drafts, and answers before relying on them.

Report profiles are versioned local recipes. Built-ins cover general,
one-on-one, interview, sales, customer feedback, project update, and
retrospective use cases. Custom profiles are stored in `workspace.json`; a
new profile version does not rewrite an existing report. Report generation
creates a new immutable report envelope, while human review is stored
separately and never overwrites generated evidence.

## Transcript, organization, and exports

Speaker labels and highlights are human annotations tied to a transcript
revision and stable segment IDs. A new ASR revision does not silently inherit
labels from an older revision. Highlight clips use the source track and
timestamp provenance, preserve explicit gaps, and do not rewrite the original
segments. Chunk timestamps are not word-level timing, and source labels do not
identify individual people.

Collections/folders, tags, people, and manually assigned recurring series are
organization metadata, not a second copy of meeting content. The library UI
supports these filters and the cross-meeting Q&A panel. Search and listing use
the disposable projection; when it is unavailable, direct canonical access
remains the source of truth and the UI can rebuild the index.

Existing meeting exports include Markdown, plain text, JSON, final audio, and
per-track WAV where the source format permits it. Reports export Markdown, text,
or JSON with report/profile/model/transcript provenance and without absolute
local paths. Export destinations are external to the library and are not
removed by meeting retention operations. There is no implicit cloud export or
import/merge workflow; restoring a workspace means restoring a complete private
backup to an isolated `SNIPVOICE_HOME` and allowing the index to reconcile.

## Consent, retention, trash, and raw audio

The capture UI makes source selection and recording state visible; Snipvoice
does not add hidden recording or automatic meeting participation. The privacy
settings expose a configurable pre-recording notice, notice language, saved-Q&A
mode, raw-audio policy, whole-meeting policy, and trash deadline. The notice is
an operational reminder, not legal consent: operators remain responsible for
obtaining and recording any consent required for the meeting.

The retention core resolves exact app-owned targets and produces an immutable,
read-only preview before mutation. It supports:

- whole-meeting moves to `trash/` with a tombstone and operation journal;
- restore from trash without overwriting a newly created source directory;
- separately planned raw-track staging under `retention-ops/`; and
- permanent purge only after explicit confirmation.

Plans include byte estimates, missing targets, excluded external exports,
capability loss, and an inventory fingerprint. Active capture, processing, or
playback leases block destructive work. Link/junction, traversal, ownership,
interruption, and index-projection checks are fail-closed. If index projection
fails after a canonical retention change, the canonical state remains primary
and the index is marked for reconciliation.

Permanent purge removes the app-owned target and its recovery material; it is
not recoverable through Snipvoice. File deletion on an SSD is not forensic
secure erasure. Use full-disk encryption and account/device controls when the
threat model requires protection after deletion.

## Backup, restore, migration, and downgrade

For a consistent backup, stop Snipvoice and copy the complete `SNIPVOICE_HOME`
tree, including `meetings/`, `workspace.json`, `library.sqlite*`, `trash/`, and
`retention-ops/`. Treat the copy as private. The index is disposable, but
keeping it can reduce rebuild time; it is not a substitute for the canonical
bundles. Model caches are separate and must be backed up independently if
desired.

Restore to an isolated `SNIPVOICE_HOME`, verify ownership and available disk,
then start Snipvoice. If the catalog is missing or corrupt, rebuild it from the
canonical bundles. Do not merge two live homes by hand, and do not restore over
an active workspace.

Schema-1 bundles open without eager rewriting. Sidecars are additive and are
created on the first relevant mutation. Unknown future schemas remain
read-only; they are never silently downgraded or rewritten. An older build may
ignore new sidecars and display stale legacy title/notes/review fields, but it
must leave unknown files untouched. Reinstalling the newer build restores the
sidecar view. Copy a fixture and test this behavior before an upgrade or
downgrade; there is no implicit Sniptype migration.

## Repair and rebuild

The SQLite catalog can be deleted or replaced without deleting canonical
meetings. Reconciliation reads canonical metadata, transcript revisions,
annotations, and report envelopes and publishes a disposable replacement. The
rebuild path supports cancellation and bounded iteration; an incomplete
replacement must not be presented as a ready index. A stale or unavailable
index should be reported as a search limitation, not as proof that meetings or
transcripts are gone.

The user-facing repair/rebuild action has bounded progress and cancellation;
the controller and GUI keep it off Tk and capture threads. The
`--sqlite-runtime-probe` entry point checks SQLite/FTS5 before desktop imports.
Run repair in an isolated copied workspace and keep the original backup
untouched. A successful rebuild proves only that the disposable projection was
recreated; it does not erase canonical meeting data.

## Validation boundary

Unit tests cover canonical sidecars, reports, annotations, index behavior,
profiles/intelligence seams, clips, GUI/controller routing, privacy/consent,
hotkey/tray dispatch, and retention failure paths. On 2026-09-17,
`python -m unittest discover -s tests -q` ran 1,195 tests successfully with 53
environment/platform skips. Ruff is unavailable in this validation context. No
unit test proves microphone capture,
Core Audio/WASAPI behavior, physical playback, two-hour drift, permissions,
packaged startup, signing, or forensic deletion. See
[`local-meeting-memory-implementation-plan.md`](local-meeting-memory-implementation-plan.md)
for the checkpoint matrix and open definition-of-done gates.
