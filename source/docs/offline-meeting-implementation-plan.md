# Offline recording and meeting implementation plan

Status: planning only, 2026-09-14. Inspected source baseline: `98c998f`.

Ship offline recording first, then the transcript workspace and local summaries. Each numbered checkpoint should be one implementation commit with its own validation. This document does not authorize implementation, dependency changes, host configuration changes, pushes, PRs, or releases.

Background: [Offline meeting and recording scout](offline-meeting-scout.md).

## Scope and implementation choices

The first release covers Windows and macOS:

- Microphone only, speaker output only, or simultaneous capture.
- Independent OS-default/manual device selection for each source.
- Start/stop, pause/resume, source meters, and a recording hotkey.
- Separate synchronized audio tracks, crash recovery, local transcription, playback, notes, and export.
- Local summaries as a separately gated extension.

Defer individual-speaker diarization, voice profiles, calendar integration, automatic meeting detection, cloud connectors, and cross-meeting Q&A.

### Confirmed code constraints

Paths in this document are relative to the repository root.

| Existing implementation | Consequence |
| --- | --- |
| `source/voice_audio.py` uses `sounddevice.InputStream`, a 30-second limit, whole-take samples, and one normalizer owner | Implement meeting capture separately; preserve dictation behavior |
| `source/voice_settings.py` has no device settings; `source/voice_support.py` constructs capture without arguments | Device selection needs persistence and runtime resolution |
| `source/voice_history.py` assumes one mono audio file and reads it entirely | Use separate segmented meeting storage |
| `source/voice_provider.py` can download during `prepare()` | Add an installed-only preparation path |
| `source/voice_runtime.py` returns text without segment timing | Meeting timestamps must initially represent audio chunks |
| `source/snipvoice.pyw` owns application construction, settings, tray, and history GUI | Integrate there while retaining the single Tk root |

The codebase-design skill informed small, testable interfaces for native capture, persistence, and session lifecycle. The actual microphone implementation uses `InputStream`; the earlier scout's `RawInputStream` description should not guide implementation.

## Shared contracts

New Python modules below belong in `source`; their tests belong in `source/tests`.

Use owned native helpers: Windows C++ with WASAPI, macOS Swift with Core Audio taps. This avoids introducing an unapproved Python audio dependency. Helpers must use an explicitly resolved executable path, run without a visible console, and never receive shell-built commands.

Define these contracts before platform implementation:

- Device selection: `{mode: default|manual, endpoint_id, default_role}`. Persist stable native endpoint IDs, not enumeration indices.
- Audio block: session generation, track, sequence, native rate/channels, capture timestamp, frame count, PCM payload, discontinuity flags.
- Capture interface: enumerate devices, start selected sources, read events, pause/resume, stop.
- Meeting controller: start, pause, resume, stop, snapshot, shutdown.
- Transcript segment: stable ID, track, start/end, text, timing precision, model identity, processing revision.

Store meetings beneath `SNIPVOICE_HOME/meetings`, independently of `voice-history`. Preserve native-format audio in short append-only segments; derive mono 16 kHz audio for ASR separately. No automatic retention or deletion.

## Ordered checkpoints

### 1. Define settings and capture contracts

Create `source/meeting_settings.py` and `source/meeting_audio.py` with validated selections, events, and an injectable capture adapter. Default to both sources using OS defaults; recording starts only through an explicit user action.

Use existing atomic settings persistence, preserving unknown keys. Settings edits apply to the next session.

Validation: malformed settings, absent devices, unsupported source combinations, round trips, and unchanged existing voice settings. Add `source/tests/test_meeting_settings.py`.

### 2. Implement durable meeting storage

Create `source/meeting_store.py`:

- Versioned session metadata and separate microphone/system tracks.
- Fixed-duration segment rotation, format metadata, sequence numbers, timestamps, and gap events.
- Atomic metadata replacement using the existing JSON pattern.
- Bounded audio readers and paginated meeting listing.
- Recovery of unfinished recordings and valid prefixes of incomplete segments.

Preserve incomplete original bytes; recovery must not require destructive truncation or a whole-session merge.

Validation: short writes, disk-full failures, crashes before/after metadata updates, incomplete final frames, malformed paths, and bounded reads. Add `source/tests/test_meeting_store.py`.

### 3. Implement native-helper transport

Add the Python helper adapter in `source/meeting_audio.py`. Use versioned, length-prefixed event headers and PCM payloads on stdout; bounded control messages on stdin.

Drain diagnostics independently, validate frame lengths and formats, reject stale session generations, and enforce startup/stop deadlines. Pipe writing and blocking operations stay off native audio callbacks.

Validation: fragmented frames, invalid lengths, unexpected EOF, stderr flooding, helper crashes, queue overflow, and termination/reaping. Add `source/tests/test_meeting_audio.py` with a fake helper.

### 4. Implement Windows device enumeration and single-source capture

Add `source/native/windows_capture.cpp`:

- Enumerate native capture/render endpoints and stable IDs.
- Resolve multimedia and communications defaults separately.
- Record microphone input or selected-render-endpoint WASAPI shared-mode loopback.
- Emit native timestamps, format changes, silence/discontinuities, and actionable errors.

Do not depend on Stereo Mix or change another application's audio routing.

Validation: helper compilation, default/manual microphone, default/manual output, silence, invalid endpoint, and clean stop. Record a known test signal on physical Windows hardware before proceeding.

### 5. Implement simultaneous Windows capture

Extend the helper to run both sources independently. Align their timelines using native capture clocks; preserve source formats, gaps, and drift information.

Use bounded queues measured in bytes/audio duration. Overflow must produce a visible partial-recording condition, never silent data loss.

Validation: distinct signals in each track, different sample rates, headphones/speakers, synchronization at start/end, and a two-hour recording with bounded memory.

### 6. Implement the macOS adapter

Add `source/native/macos_capture.swift` using Core Audio microphone capture and device-specific output taps. Implement the same transport and event contracts.

Proposed initial target: macOS 14.4+, subject to SDK compilation and packaged permission testing. Earlier systems must report unsupported system capture clearly.

Validation: default/manual devices, both sources, permission denial/recovery, output changes, native cleanup, and long-session synchronization on a physical Mac. Keep Tk/Cocoa lifecycle unchanged.

### 7. Implement meeting lifecycle and resource reservation

Create `source/meeting_support.py`. States should distinguish starting, recording, paused, stopping, interrupted, and failed; transcription has its own status.

Add a temporary reservation/suspension mechanism to `VoiceController` in `source/voice_support.py`:

- Close dictation/retry admission before granting meeting ownership.
- Grant only after current work safely finishes and inference resources are available.
- Preserve `voice_enabled` and other user preferences.
- Reject conflicting model switches/deletion while reserved.
- Release ownership only after capture/inference workers terminate.
- Restore eligible dictation readiness without triggering downloads.

Do not call ordinary `disable()` to reserve resources; it changes persisted settings.

Validation: simultaneous starts, stop during startup, repeated stop, shutdown, late callbacks, reservation failures, and restored dictation. Extend controller tests and add `source/tests/test_meeting_support.py`.

### 8. Handle device changes, pause, and partial capture

Extend native adapters, `source/meeting_audio.py`, and `source/meeting_support.py`. Default mode follows relevant OS default changes by closing/reopening the affected track segment. Manual mode stays pinned to its selected endpoint.

Pause stops both sources and preserves elapsed-time gaps. On source loss, continue the remaining track with an explicit partial status; if all sources disappear, stop and preserve recoverable audio.

Validation: USB/Bluetooth disconnect/reconnect, endpoint replacement, default changes, format changes, pause races, and no silent fallback.

### 9. Add recording controls and device selection

Create `source/meeting_gui.py`; integrate it into application construction, tray, and shutdown in `source/snipvoice.pyw`.

Provide PT-BR controls for sources, selectors, refresh, meters, start/stop/pause, elapsed time, and errors. Enumerate devices and load meeting data off Tk; marshal snapshots through `GuiThread`.

Selecting an output endpoint captures its existing playback; it must not reroute meeting audio.

Validation: fake-controller widget tests, failed persistence, stale refresh results, close/reopen behavior, and actual desktop interaction. Add `source/tests/test_meeting_gui.py` using the existing shared-root patterns.

### 10. Add the recording hotkey

Extend `VoiceHotkeyMonitor` in `source/voice_hotkey.py` with an optional recording chord and press-edge toggle. Keep existing constructor callers compatible.

The recording shortcut must work while dictation is disabled. Ignore auto-repeat and reject conflicting chords. Initially leave the shortcut unassigned until configured.

Validation: chord specificity, repeat handling, listener lifecycle, dictation/command compatibility, and bounded keyboard callbacks. Extend `source/tests/test_voice_hotkey.py` and meeting settings tests.

### 11. Add installed-only preparation and local model import

Extend `LocalVoiceProvider.prepare()` in `source/voice_provider.py` with an explicit download policy, preserving existing callers. Meeting jobs and dictation restoration use installed-only preparation.

Add catalog-constrained local import in `source/voice_models.py`: stream-copy, verify size/SHA-256, then atomically install. Preserve an existing valid installation on failure.

Validation: missing/corrupt models, cancelled imports, insufficient disk space, incompatible files, and assertions that no network opener is called. Extend `source/tests/test_voice_provider.py` and `source/tests/test_voice_models.py`.

### 12. Implement resumable local transcription

Create `source/meeting_transcription.py`. Start with bounded chunked inference using existing Parakeet/Qwen profiles.

- Recording works without a loaded model.
- Capture continues when ASR is slow or unavailable.
- Persist pending/completed jobs and model/settings snapshots.
- Serialize native inference; do not share a session across concurrent jobs.
- Preserve raw chunk outputs and separate processing revisions.
- Cancellation stops processing without deleting recordings.
- Label timestamps as chunk-level; do not claim word alignment.

Validation: slower-than-realtime inference, restart/resume, cancellation, duplicate job admission, stale revisions, silent chunks, and Portuguese/English boundary behavior. Add `source/tests/test_meeting_transcription.py`.

### 13. Add the meeting workspace

Extend `source/meeting_gui.py` and `source/meeting_store.py` with titles, manual notes/bookmarks, transcript display, status filters, and ordinary local text search.

Add bounded playback reading with `sounddevice.OutputStream`, track selection, seeking, and gap handling. Disable playback during system capture initially to prevent recording the app's own playback.

Validation: long-library pagination, seeks across segment boundaries, mixed sample rates, note preservation, playback cancellation, and timestamp navigation. Add a focused playback test module if playback becomes its own module.

### 14. Add import, reprocessing, and export

Extend the meeting workspace, store, and transcription modules. Support WAV import first using existing/standard-library capabilities. Broader codecs require an approved decoder decision.

Add model/language reprocessing with preserved prior revisions, plus explicit Markdown/plain-text export containing source labels, timing precision, and metadata.

Validation: malformed/oversized WAV files, bounded import, failed reprocessing, export escaping, destination errors, and unchanged original recordings/notes.

### 15. Add local summaries as an extension

Create `source/meeting_summary.py` with an injected local text-model adapter. Initially support an existing, manually provisioned Ollama runtime; do not install or configure it automatically.

Generate editable summaries, decisions, and action items referencing transcript segment IDs. Validate references and structured fields; absent owners/deadlines remain unknown.

Loopback URLs alone do not establish offline processing: verify the runtime uses local weights with outbound networking blocked.

Validation: unavailable runtime/model, timeouts, cancellation, oversized transcripts, invalid references, cloud-backed routes, and preservation of previous reports/manual notes. Add `source/tests/test_meeting_summary.py`.

### 16. Integrate packaging and release checks

Update `build_release.bat`, `build_release_macos.sh`, runtime probes, packaging tests, and `.github/workflows/ci.yml` to compile/bundle helpers. Add macOS system-audio usage descriptions before signing.

Helpers need a non-recording self-test; package probes must not activate microphones. Retain staged promotion and existing rollback behavior.

Validation: clean builds, native library/helper resolution, signature verification, packaged GUI startup, source/device capture, and cold offline startup. Extend `source/tests/test_voice_packaging.py` and runtime-probe coverage.

## Dependencies, compatibility, and risks

- Native compiler/SDK availability is unresolved; `cl` and `swift` were not found in the inspected Windows shell. Do not infer that they are absent from the host.
- No project dependency change is assumed. Any alternative capture library, codec, model pin, or dependency installation needs explicit approval.
- Existing dictation settings/history remain readable and unchanged. Meetings use a new versioned directory; unknown future schemas must open read-only or report incompatibility.
- The operator contract references `instructions/git-workflow.md`, but that file was missing during planning. Before implementation commits, check whether it has moved; otherwise follow the explicit task-branch rules in `AGENTS.md`.
- Preserve the existing untracked scouting note. Create a task branch; stage only owned paths.
- System capture records the selected endpoint's mix, potentially including notifications/music. Per-application capture is deferred.
- Physical speakers can leak call audio into the microphone and duplicate speech. Two tracks do not establish acoustic echo cancellation; validate headphones and speakers separately and do not promise echo removal.
- Source labels describe microphone/system origin, not individual people. A room microphone can contain multiple speakers.
- Playback clocks and microphone/output capture clocks can differ. Preserve native timestamps and discontinuities; report measured drift and synchronization limits.
- Capture, inference, and metadata reads must remain off keyboard/Tk callbacks. Never retain audio-device-owned buffers beyond callbacks without copying them.
- Large recordings and libraries must not grow sample lists, queues, or GUI loading work with session/library duration. Record explicit greppable `ceiling:` comments for deliberate limits.
- Saved audio, transcripts, settings, and model files remain private and outside the repository. Hardware test recordings must use an explicitly isolated test destination.

## Safe implementation delegation

After checkpoint 1 locks contracts, storage, Windows native capture, and macOS native capture can be delegated to separate implementation agents with non-overlapping owned files. Root owns controller integration, shared files, packaging, and final verification.

GUI and transcription work can run independently after their contracts stabilize. Helpers must share the same protocol and adapter contract; root validates both against the common conformance tests.

Suggested dependencies:

- Checkpoints 2 and 3 depend on checkpoint 1 and can run independently.
- Checkpoints 4 and 6 depend on checkpoints 1 and 3 and can run independently.
- Checkpoint 5 depends on checkpoint 4.
- Checkpoint 7 depends on checkpoints 1-3 and can be tested using fake capture while native work continues.
- Checkpoint 8 integrates checkpoints 4-7.
- Checkpoint 9 depends on checkpoints 1, 2, 7, and 8.
- Checkpoint 10 depends on checkpoint 9's application lifecycle integration.
- Checkpoint 11 can run independently once the reservation/download policy is agreed.
- Checkpoint 12 depends on checkpoints 2, 7, and 11.
- Checkpoint 13 depends on checkpoints 9 and 12.
- Checkpoint 14 depends on checkpoints 11-13.
- Checkpoint 15 depends on checkpoints 12-14 and is separately accepted.
- Checkpoint 16 packages the completed core; it need not wait for optional summaries.

Planning itself does not authorize agent spawning or implementation.

## Final validation

Run meaningful targeted tests for each checkpoint, then the full gates:

From `source`:

```powershell
python -m unittest discover -s tests -v
```

Temporary test artifacts must stay in `source/tests/tmp`. On Windows, set the test process's `TEMP`/`TMP` to that directory when required by the environment; do not change global host settings.

From the repository root, with the pinned development tool:

```powershell
python -m ruff check source
git diff --check
```

Also run the strict native-runtime tests without optional skips, common helper protocol/conformance tests, native helper builds, and platform package builds. Use the existing `voice-native` CI lane's no-skips runtime/resampler command. Retain the Windows/macOS/Linux Python 3.12/3.14 regression matrix; Linux meeting capture remains explicitly unsupported.

On physical Windows and macOS, prove:

- OS-default/manual mic-only, output-only, and dual-source recording.
- Two-hour bounded-memory capture, measured synchronization, and final audio-tail preservation.
- Device loss/change, differing sample rates, pause, permission denial, and restart recovery.
- Overflow/disk-full handling, slow ASR, cancellation, and no stale callbacks or blind paste.
- Packaged operation and cold startup with provisioned models and networking disabled.
- Local playback, note preservation, reprocessing, and export of recovered recordings.

The earlier local run had 793 passes and 40 skips. Missing `soxr` and unusable Tcl initialization remain validation blockers, not passed gates. These results are a prior local baseline, not final validation of this feature.

Before completion, inspect the full task-owned diff and final status, perform a distinct review of capture/lifecycle/storage changes, and account explicitly for unavailable, failed, skipped, or unverified checks. Do not promote a release based only on unit tests, helper self-tests, or CI.

## Definition of done

Checkpoints 1-14 and 16 pass on both supported platforms. Old dictation still works, default/manual device selections behave as specified, recordings survive interruption, session memory is bounded, transcription can resume locally, and offline processing makes no external requests.

Local summaries are accepted separately when checkpoint 15 passes, using a locally provisioned text model and verified offline runtime. Deferred features are not implied by acceptance of the core release.
