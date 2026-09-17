# ADR 0001: Canonical meeting bundles with a rebuildable catalog

- Status: accepted
- Date: 2026-09-16

## Decision

Snipvoice keeps each meeting bundle as the canonical source of truth and adds a
disposable SQLite/FTS catalog for listing, snippets, and retrieval. The catalog
contains projections only: it never stores audio or another irreplaceable copy
of a transcript, annotation, or report.

`MeetingStore` remains responsible for capture, journal recovery, native PCM
tracks, metadata, and transcript JSONL. `MeetingLibrary` is the user-facing
storage seam for canonical sidecars and projections. `MeetingIndex` owns the
thread-confined SQLite implementation and may be deleted and rebuilt at any
time.

## Consequences

- Existing schema-1 bundles open without eager migration. `annotations.json` is
  additive and is created only by an explicit human-owned mutation.
- A successful canonical write remains successful when SQLite is unavailable,
  corrupt, read-only, or missing FTS5. The library marks the projection stale
  and falls back to bounded canonical directory reads.
- SQLite WAL/SHM files are private derived data and are safe to discard. A
  rebuild is transactional: cancellation or failure leaves a previously ready
  projection usable, or marks a new disposable database incomplete.
- FTS5 is a release capability requirement for search, but it is probed against
  an in-memory database and never by opening the user's production library.
- Downgraded Snipvoice builds may show stale legacy title/notes fields while
  leaving unknown sidecars untouched; reinstalling this build restores the
  sidecar view.

## Domain glossary

- **Meeting bundle**: one app-owned directory containing a recording and all
  durable material derived from it.
- **Transcript revision**: immutable ASR output for one profile/language run.
- **Annotation**: human-owned state layered over a meeting or revision.
- **Report revision**: an immutable generated artifact bound to source and model
  provenance; a reviewed artifact never overwrites it.
- **Collection**: a user-created folder/project grouping. Tags are lightweight
  labels and a series is a manually assigned recurring-meeting group.
- **Catalog index**: the disposable SQLite projection used for listing/search.
- **Retention plan**: an immutable preview of exact app-owned targets and lost
  capabilities before a destructive operation.

## Rollback

Removing `library.sqlite`, its `-wal`/`-shm` companions, and any unfinished
disposable rebuild loses only the projection. Reopening the canonical bundles
and running reconciliation recreates it. Canonical JSON, JSONL, journal, and
PCM files are never rolled back through SQLite.
