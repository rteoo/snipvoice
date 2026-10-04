# Offline meeting implementation and validation

This document records the current local meeting implementation, source-level
checks, and the explicitly dated Windows host checks below. It does not certify
packaged startup, full tray integration, signing, or macOS physical capture.

## Implemented local behavior

The meeting workspace preserves Snipvoice's capture and recovery boundary:

- microphone and selected speaker-output sources remain independent native PCM
  tracks, with append-only segments, CRC journaling, atomic metadata, explicit
  gaps, pause/resume, and interrupted-session recovery;
- transcription and summaries run automatically with installed models,
  sequentially and with preserved revisions. Transcription is bounded by
  audio chunks; unavailable models are skipped and acquisition remains an
  explicit settings action;
- final audio is a derived atomic MP3 mixdown; WAV remains an export option and
  existing WAV recordings remain playable. Original raw tracks remain the
  model input and recovery source. Optional microphone volume adjustment
  measures the raw track in bounded memory and applies capped gain and a limiter
  only to the final playback file. It does not hard-gate quiet speech or perform spectral
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
| 3. Transcript interaction | Full text with paragraphs by default, switchable timestamps, complete text export, and bounded previews; advanced labels, highlights, navigation and source-track clips remain available. Timings remain chunk-level. |
| 4. Single-meeting Q&A | Local `Ask this meeting` with bounded answers, validated transcript citations, uncertainty, and explicit save-to-QA-report behavior. |
| 5. Organization | Existing canonical collections, tags, people, and series metadata is preserved; manual categorization and filter controls are removed from the library interface. |
| 6. Search and cross-meeting Q&A | SQLite/FTS5 projection, provenance snippets, safe query handling, rebuild/repair, and cited local answers across recordings. |
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

### Windows recording transport

Windows WASAPI commonly delivers one packet every 10 ms per source. The previous
transport limited its queue to 128 events, so two sources could exhaust that
queue after approximately 640 ms of consumer delay, even below the byte/time
limits. Each packet also required a raw-track fsync and a journal fsync, roughly
400 flushes per second for two sources. The Python reader separately failed
after 250 ms with a full four-block queue.

The Windows helper now groups clock-contiguous packets into at most 100 ms of
PCM, preserving every sample. Only packet-clock jitter within 2 ms is normalized
inside a batch; real clock gaps/overlaps and native discontinuities flush it.
Pause, source changes and stop flush short tails. The first packet after start
or an explicit reset establishes the source clock and does not flag an earlier
audio loss. Subsequent discontinuity flags remain visible journal events.

The helper queue is bounded by 10 seconds per source, 16 MiB of PCM, and 1,024
events. The Python queue holds at most eight blocks (32 MiB at the protocol's
maximum block size) and applies backpressure for up to 10 seconds. These are
independent limits, not a guarantee for a particular stall duration or format.
Raw-track and journal fsync ordering, append-only recovery, and atomic metadata
remain unchanged. Exhaustion stops capture visibly rather than dropping audio
silently; its terminal error now identifies disk/consumer backpressure.

The recording overlay shares the dictation indicator and the sole Tk root. Its
200 ms poll observes the controller independently of the manager window, so it
continues while minimized or closed to the tray. Starting/saving, paused,
partial, and interrupted states are distinct from active recording.

Regression coverage exercises temporary and persistent consumer stalls, forced
teardown with a full queue, actionable native overflow errors, packet batching
and tail preservation in the native non-recording self-test, and overlay
minimize/restore/dictation ownership. Desktop smoke checks include preserving
the foreground window when showing the overlay. These checks do not certify a
packaged release or macOS capture.

### Windows host checks on 2026-10-03

- The MSVC/Windows SDK helper build and its non-recording self-test passed.
- The full desktop suite ran 1,187 tests successfully with 10 skips, including
  unavailable PyAV/MP3 integration and platform-specific cases. Ruff 0.16.3 and
  the diff formatting check passed. The final focused transport, overlay,
  entrypoint, catalog, and native suites ran 40 tests successfully; a subsequent
  real Tk minimize/restore smoke also passed.
- A 12-minute microphone/system-output soak applied a five-second consumer
  stall and a one-second pause/resume. Incoming device audio was discarded and
  only synthetic silence was persisted in a temporary store. Capture stopped on
  request with no transport overflow or source error. All 14,369 emitted audio
  blocks survived durable-store readback with identical per-source frame totals
  (approximately 718.16 seconds of system audio and 717.65 seconds of microphone
  audio, after source startup and the explicit pause). The fixture was removed.
- Windows reported 15 native discontinuity events during this load test, in
  addition to four explicit pause/resume events. These remain visible source
  warnings; continued recording is not proof of loss-free endpoint audio.
  Installed-package operation, MP3 encoding, and macOS remain unverified.

Focused unit coverage exists for canonical sidecars, report/profile validation,
citation handling, Q&A save semantics, annotations, clips, organization,
SQLite/FTS5 search and rebuild, GUI/controller routing, hotkey/tray dispatch,
privacy/consent, and retention failure/recovery paths. On 2026-09-17,
`python -m unittest discover -s tests -q` ran 1,195 tests successfully with 53
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
