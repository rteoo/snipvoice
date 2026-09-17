# Offline meeting implementation and validation

This document records the current local meeting implementation. It describes
source-level behavior and focused checks; it does not certify physical Tk/tray
interaction, native capture, packaged startup, or signing on this host.

## Implemented local behavior

The meeting workspace preserves Snipvoice's capture and recovery boundary:

- microphone and selected speaker-output sources remain independent native PCM
  tracks, with append-only segments, CRC journaling, atomic metadata, explicit
  gaps, pause/resume, and interrupted-session recovery;
- transcription is installed-model-only, opt-in, sequential, revision-preserving,
  and bounded by transcript chunks; model acquisition remains an explicit
  settings action;
- final audio is a derived atomic mixdown. The optional microphone treatment is
  a deterministic low-level gate, bounded gain, and limiter, not spectral
  denoising or acoustic echo cancellation;
- the shared Tk root, controller workers, meeting hotkey, and tray actions keep
  capture, inference, disk work, and playback off keyboard/Tk callbacks;
- startup loads privacy defaults and reconciles interrupted retention journals
  before admitting the meeting hotkey or destructive actions.

The first seven local meeting-memory features are implemented through the
canonical-bundle/disposable-index architecture:

| Feature | Implemented behavior |
| --- | --- |
| 1. Report profiles | Built-in and custom versioned profiles; bounded local reports with model and transcript provenance; immutable report history and separate review. |
| 2. Post-meeting artifacts | Decisions, actions, questions, risks, objections, feedback, and follow-up drafts are reviewable, cited, and exportable without sending or mutating external systems. |
| 3. Transcript interaction | Revision-scoped manual speaker labels, highlights, navigation, source-track clips, and bounded transcript pages; timings remain chunk-level. |
| 4. Single-meeting Q&A | Local `Ask this meeting` with bounded answers, validated transcript citations, uncertainty, and explicit save-to-QA-report behavior. |
| 5. Organization | Canonical collections, tags, people, and manually assigned series with filtered library views and bounded batch assignment. |
| 6. Search and cross-meeting Q&A | SQLite/FTS5 projection, provenance snippets, safe query handling, filters, rebuild/repair, and cited local answers across selected meetings. |
| 7. Privacy and retention | Visible consent reminder/settings, memory-only or explicit-save Q&A policy, retention previews, same-root trash/restore, journaled raw-track staging, and separately confirmed permanent purge. |

## Offline and privacy boundary

`SNIPVOICE_HOME` contains the authoritative meeting bundles and a disposable
`library.sqlite`/FTS5 projection. The catalog may contain sensitive plaintext
copied from transcripts, notes, and reports. Deleting or rebuilding the catalog
must never be described as deleting meeting content. The canonical bundle is
the recovery source when the index is stale, missing, corrupt, or incomplete.

There is no telemetry, implicit upload, or cloud fallback by default. Reports
and answers run through the local llama.cpp seam after an installed model has
been selected. Transcript, profile, and question text are untrusted data;
generated claims require resolvable transcript citations and human review.
Source labels are not speaker identity, and chunk timestamps are not word
timing. External exports are outside the library and are not removed by
retention operations.

Consent reminders are configurable but are not legal consent. Operators remain
responsible for notifying attendees and meeting applicable law or policy.
Permanent purge removes app-owned recovery material, but filesystem deletion on
an SSD is not forensic erasure; use disk encryption and account/device controls
for that threat model.

## Source-level validation boundary

Focused unit coverage exists for canonical sidecars, report/profile validation,
citation handling, Q&A save semantics, annotations, clips, organization,
SQLite/FTS5 search and rebuild, GUI/controller routing, hotkey/tray dispatch,
privacy/consent, and retention failure/recovery paths. On 2026-09-17,
`python -m unittest discover -s tests -q` ran 1,191 tests successfully with 53
environment/platform skips and the SQLite runtime probe passed. Ruff is
unavailable in this validation context and must not be called
passed without running the pinned development tool.

The SQLite runtime probe is available as `--sqlite-runtime-probe`; it checks
the interpreter before desktop imports. Rebuild and repair use bounded progress
and cancellation. Tests must use copied fixtures under `source/tests/tmp`, not
a live `SNIPVOICE_HOME`.

## Unverified release and physical gates

The following remain environment checks rather than missing local-memory
features:

1. Real Tk window and tray interaction on supported Windows and macOS hosts.
2. Physical microphone/WASAPI loopback/Core Audio capture, endpoint changes,
   permission recovery, playback, and long-session drift/memory behavior.
3. Packaged cold startup, bundled native helpers, model runtimes, migrations,
   offline operation, and the synthetic 10,000-meeting/250,000-segment scale
   measurement.
4. Windows publisher signing and Apple Developer ID signing/notarization.

See [local meeting-memory operations](local-meeting-memory-operations.md) for
backup, restore, downgrade, retention, and repair procedures, and the
[implementation record](local-meeting-memory-implementation-plan.md) for the
checkpoint disposition.
