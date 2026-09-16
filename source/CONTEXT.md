# Local meeting-memory storage context

The storage seam uses canonical meeting bundles plus a rebuildable SQLite/FTS
catalog. `MeetingStore` owns capture/recovery and remains on the capture path;
`MeetingLibrary` is the only user-facing library interface; `MeetingIndex` is an
internal disposable projection.

Canonical data remains in `meetings/<session-id>/`: `metadata.json`, the valid
prefix of `events.journal`, native PCM track segments, append-only transcript
revision JSONL, additive `annotations.json`, and generated report envelopes.
`workspace.json` holds global workspace definitions. `library.sqlite` may be
deleted at any point without data loss and is rebuilt from those canonical
files. No audio or transcript BLOB is stored in SQLite.

Schema-1 meetings are opened without eager migration. Missing annotations are
projected from legacy title, notes, bookmarks, summary, and reviewed-summary
fields at read time; the sidecar is written only after an explicit mutation.
Known sidecars are versioned, size-limited, validated, atomically replaced,
and compare-and-swap protected by their integer generation. Malformed or newer
sidecars remain untouched and produce an actionable read-only error instead of
silently falling back to legacy data.

SQLite/FTS is optional for direct access. The catalog has explicit
`ready`, `stale`, `rebuilding`, `unavailable`, and `incomplete` states. A
canonical write wins if indexing fails; the next reconciliation repairs the
projection. Capture, hotkey, and Tk callbacks never open SQLite or perform
unbounded disk/model work.

The active packaged interpreter must report Python `sqlite3` and FTS5 support
through the in-memory runtime probe before search is considered releasable.
