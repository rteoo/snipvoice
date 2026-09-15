# Offline meeting implementation and validation

Date: 2026-09-14. Branch: `codex/offline-meetings`.

Source implementation is integrated and published for review in
[PR #1](https://github.com/rteoo/snipvoice/pull/1). Native physical platform
acceptance remains open; this is not a certified desktop release.

## Delivered checkpoints

| Plan checkpoints | Implementation |
| --- | --- |
| 1, 3 | Validated meeting settings, stable endpoint selections, bounded versioned helper transport, process deadlines and generation guards. |
| 2 | Separate native PCM tracks, 30-second segments, CRC journal, atomic metadata, bounded event projections and streaming recovery/readers. |
| 4, 5 | Owned WASAPI helper: input/output enumeration, microphone and selected-endpoint loopback, simultaneous capture, shared native clocks, PCM conversion, default roles, explicit gaps. |
| 6 | Owned macOS 14.4+ CoreAudio input and device-specific tap helper, stable UIDs, microphone permissions, bounded callback queues and cleanup. |
| 7, 8 | Worker-owned lifecycle, temporary dictation reservation, pause/resume, pinned manual endpoints, default changes, partial/failure preservation and safe shutdown. |
| 9, 10, 13 | PT-BR tray workspace, source/device controls, meters, paginated/status-filtered library, title/notes/bookmarks, local text search, timestamp/track playback, optional recording shortcut. |
| 11, 12 | Installed-only model preparation, verified catalog-constrained disk import, durable pending jobs, bounded per-track ASR, chunk timestamps, preserved raw outputs/revisions and resumable valid prefixes. |
| 14 | Standard PCM WAV import; explicit Markdown/plain/JSON and per-track PCM16 WAV export; cancellation and atomic destination preservation. |
| 15 | Opt-in fixed-loopback Ollama adapter; cloud/remote model rejection, structured cited summaries/actions, bounded hierarchical reduction, preserved original and manually reviewed summaries. |
| 16 | Helper compiler scripts, CI build gates, bundled helper resolution, macOS usage descriptions and non-recording staged package probes. |

Recording works without an installed model. Completed recordings queue a durable
pending transcription revision; the user starts/resumes processing from the
workspace. Meeting processing and eligible dictation restoration never download
models. Model downloads remain explicit actions in the existing voice settings.

The recording shortcut reuses an independent instance of the existing bounded
hotkey observer. This keeps it available while dictation is disabled or temporarily
reserved, without changing existing observer constructor callers. Chords with the
same final key and overlapping modifier subsets are rejected in both settings flows.

## Validation on this Windows host

- `python -m unittest discover -s tests -v`, from `source`: **924 tests run;
  878 passed; 46 reported skips; no failures**.
- Cached Ruff **0.16.4**: `ruff check source` passed. The configured development
  pin remains **0.16.3**; that exact executable is unavailable on this host.
- `python -m compileall -q source`: passed.
- `python -m pip check`: passed.
- `git diff --check`: passed.
- GitHub CI: **13 jobs passed** across Windows, macOS and Linux on Python 3.12
  and 3.14. Hosted Windows and macOS jobs compiled both capture helpers and ran
  their non-recording probes successfully.
- Independent integration and native protocol reviews completed. Corrections
  include device-change timestamps, matching sample-rate ceilings, required integer
  generations, error exit handling, partial loss statuses, and reservation retention
  after unproven native teardown.
- Tests exercise fragmented framing, subprocess stderr flooding/reaping, stale
  generations, durable journal prefixes, disk/write failures, import/export
  cancellation, seek/gap playback, failed saves, resumed transcript tails,
  installed-only preparation, structured citations and remote model rejection.

Skips include unavailable Tcl initialization, the missing optional `soxr` runtime,
OS-specific cases, and capture helper binary/platform tests. Behavioral GUI tests
passed; the actual Tk window smoke did not run successfully.

No project dependencies or host tools were installed or changed. No private live
recordings, models, settings or command libraries were copied into the repository.

## Remaining acceptance gates

1. Use isolated synthetic signals on physical Windows and macOS hardware to verify
   microphone/output selection, simultaneous tracks, formats, pause, permission
   denial, endpoint changes, and cleanup. A tap-only aggregate on macOS needs actual
   signal verification; static API review does not establish successful capture.
2. Record two hours on each platform and measure start/end synchronization, drift,
   bounded memory, disk latency and recoverability. Unit queue/storage limits do
   not certify physical long-session behavior.
3. Exercise the real Tk workspace, cold offline startup, actual local ASR weights,
   physical playback, existing Ollama weights with outbound networking blocked,
   and staged packages/signatures. No packaged release was built or promoted here.
4. Review and merge PR #1 only after the physical acceptance gates required for the
   intended release have been assigned or completed.

## Deliberate limits

- Windows/macOS only for native capture; macOS system capture requires 14.4+.
- Manual endpoints never silently fall back. Output selection does not reroute
  another application's playback.
- Source labels identify tracks, not people. No diarization or acoustic echo
  cancellation is promised. Transcript timings describe audio chunks, not words.
- WAV import accepts standard integer PCM. Format-changing recordings stay in
  original segments; WAV export refuses a mixed-format track rather than resampling
  silently. RIFF WAV export has the 4 GiB format ceiling.
- GUI pages show 50 sessions and at most 500 transcript rows; export retains all
  revisions. Deep library pagination has a documented 10,000-row offset ceiling;
  a rebuildable SQLite index is the upgrade path.
- Playback is blocked during recording. Inference/file jobs serialize meeting
  admission; unproven teardown keeps the runtime unavailable.
- Ollama is provisioned manually. Disable its cloud features with
  `OLLAMA_NO_CLOUD=1`; loopback calls and model metadata alone cannot certify the
  external process is offline. Summaries require review before use.
