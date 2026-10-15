# Offline meeting implementation and validation

This document records the current local meeting implementation, source-level
checks, and the explicitly dated Windows host checks below. It does not certify
packaged startup, full tray integration, signing, or macOS physical capture.

## Implemented local behavior

The meeting workspace preserves Snipvoice's capture and recovery boundary:

- microphone and selected speaker-output sources use temporary independent PCM
  capture segments, CRC journaling, atomic metadata, explicit gaps, pause/resume,
  and interrupted-session recovery until a final MP3 has been verified;
- transcription and summaries run automatically with installed models,
  sequentially and with preserved revisions. Transcription is bounded by
  audio chunks; unavailable models are skipped and acquisition remains an
  explicit settings action;
- final audio is an atomic MP3 mixdown, decoded to EOF and checked against the
  rendered duration before publication. Playback, transcription, retries, and
  clips use that MP3. A journaled compaction removes temporary capture segments
  only after verified publication and the canonical metadata commit. Existing
  WAV recordings remain readable for compatibility. Optional capture-time
  microphone gain affects the final mix and does not perform acoustic echo
  cancellation;
- the shared Tk root, controller workers, meeting hotkey, and tray actions keep
  capture, inference, disk work, and playback off keyboard/Tk callbacks;
- startup loads privacy defaults and reconciles interrupted retention/compaction journals
  before admitting the meeting hotkey or destructive actions.

The first seven local meeting-memory features are implemented through the
canonical-bundle/disposable-index architecture:

| Feature | Implemented behavior |
| --- | --- |
| 1. Report profiles | Built-in and custom versioned profiles; bounded local reports with model and transcript provenance; immutable report history and separate review. |
| 2. Post-meeting artifacts | Decisions, actions, questions, risks, objections, feedback, and follow-up drafts are reviewable, cited, and exportable without sending or mutating external systems. |
| 3. Transcript interaction | Full text with paragraphs by default, switchable timestamps, complete text export, and bounded previews; advanced labels, highlights, navigation and final-MP3 clips remain available. Timings remain chunk-level. |
| 4. Single-meeting Q&A | Local `Ask this meeting` with bounded answers, validated transcript citations, uncertainty, and explicit save-to-QA-report behavior. |
| 5. Organization | Existing canonical collections, tags, people, and series metadata is preserved; manual categorization and filter controls are removed from the library interface. |
| 6. Search and cross-meeting Q&A | SQLite/FTS5 projection, provenance snippets, safe query handling, rebuild/repair, and cited local answers across recordings. |
| 7. Privacy and retention | Visible consent reminder/settings, memory-only or explicit-save Q&A policy, whole-meeting retention, same-root trash/restore, verified capture compaction, and separately confirmed permanent purge. |

## MP3 workflow validation, 2026-10-15

The library no longer displays a Completed/Partial status column or source-file
download/removal controls. Capture-health diagnostics remain in the private
journal; a device fault cannot be repaired by changing its label. Export progress
shows preparation, encoding percentage, finalization, and saved output. The
controller publishes 100% only after validation, atomic publication, and the
metadata commit. Processing failures/cancellation are persisted separately from
capture health. Imports now create the final MP3 before inference begins.

Native Windows capture copies and releases every acquired WASAPI packet before
gap diagnostics, batch flushing, conversion, or queue work. This reduces buffer
hold time; the historical device discontinuities alone do not establish their
cause. The helper's executable self-test checks copying, release ordering, and
exactly one release on the failure path.

Hierarchical summaries use a compact grammar for intermediate reports whose
budget is at most 2 KiB: one supported item per section, one citation per item,
and text bounded to 128 characters. This aligns the model contract with the
pairwise reduction budget before generation. The final synthesis retains the
requested profile and its larger report allowance. One bounded regeneration
attempt remains available for oversized or token-limited output. The worker
retains the model for that recoverable condition; failures preserve prior reports.

Verified local checks using the existing clean PyAV 18.1.0 codec:

- Three hours of synthetic 48 kHz stereo audio encoded and fully decoded:
  518,400,000 decoded frames, 10,800 seconds, 259,201,197 output bytes, about
  329 seconds of processing. The temporary output was cleaned after validation.
- MP3-only compaction, transcription input/retry, clip export, and import passed
  with stereo 48 kHz and mono 16/8 kHz fixtures.
- Native PCM blocks with one through eight channels are accepted at the
  mixdown boundary. Final MP3/WAV output stays mono or stereo; sources above
  two channels are averaged across all channels because no native channel mask
  is persisted. This includes center/surround samples but loses spatial
  placement and reduces their level. The six-channel codec fixture checks
  duration and stereo output.
- Windows synthesized an approximately eight-second test sentence. After MP3
  publication and capture compaction, the installed Whisper Small model
  transcribed the final MP3 and matched all four expected phrase checks.
- Failure injection covers pre-publication validation failure, destination
  preservation, cancellation, stage/commit crashes, restart recovery, modified
  final files, lock contention, and path traversal in recovery journals.

`tests/test_mp3_workflow.py` requires the provisioned codec and skips explicitly
when it is unavailable. The local codec smoke used an existing Python 3.12
runtime and NumPy 2.5.2; the regular development suite used Python 3.14. These
checks do not replace the release's pinned dependency/build gates. No dependency
was installed for these initial checks, which preceded packaging and installation.
A rebuilt Windows package and a live endpoint soak are separate gates before
claiming the installed recording workflow has been validated. Synthetic
long-file processing cannot certify uninterrupted Windows/device capture or
perfect recognition of arbitrary speech. Private incident evidence stays
outside the public repository.

The Windows/macOS packagers also run `--meeting-audio-probe` before promotion.
This isolated, model-free probe exercises actual MP3 export, pre-publication
validation, capture compaction, and final-file transcription input in the frozen
executable. It cleans its synthetic data and never opens the user's library.
Oversized generated reports receive one compact-output retry with unchanged
evidence; the repair adds no evidence-payload bytes. A second invalid response
still preserves prior results and surfaces a bounded failure.

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
and interrupted states are distinct from active recording; actionable errors
remain available without a library capture-health status column.

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
