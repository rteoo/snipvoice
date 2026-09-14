# Offline meeting and recording feature scout

Status: primary-source research snapshot, 2026-09-14. Scope: compare the supplied products for features that could extend Snipvoice while keeping local-model and offline operation as the baseline. Benchmark evidence comes from official product documentation and repository READMEs; the competitors were not installed, run, or comprehensively audited. Snipvoice architecture findings below were checked against the current local source at `98c998f`.

## Executive recommendation

The highest-value addition is a durable **meeting recording mode** alongside push-to-talk dictation:

1. Capture microphone and operating-system output independently, with `OS default` and an explicit device picker for each source.
2. Retain separate synchronized microphone and output tracks, with level meters and a clear unavailable-device error. A playback mix can be derived separately.
3. Keep the existing local ASR pipeline, but add long-session chunking and resumable processing.
4. Add local transcript timestamps and source labels first; summaries, search, and individual-speaker diarization can follow as local-model layers. Calendar integrations are a separate later decision.

The benchmark evidence supports this order. Capture and device routing are the shared foundation; diarization and meeting intelligence are downstream and platform/model dependent.

## Benchmark findings

### Granola

Granola's official site describes a bot-free notepad that uses computer audio and works with Zoom, Meet, Teams, and other meeting apps ([product page](https://www.granola.ai/)). Its security page describes manually started capture, external transcription/AI providers, and US-hosted AWS note storage ([security page](https://www.granola.ai/security)). Its desktop transcription documentation distinguishes microphone and system audio as `Me` and `Them`; these are source labels, not identification of every participant. It also documents no saved meeting audio and no pre-recorded file import ([transcription documentation](https://docs.granola.ai/help-center/taking-notes/transcription)).

Useful product ideas: manual start, editable notes, action items, follow-up generation, meeting history, and calendar preparation. The product is a UX benchmark, not an offline implementation benchmark: its own security material documents cloud providers and cloud-hosted notes. Granola's public pages do not document a user-selectable local model or local-only mode.

### Meetily

Meetily's public repository README describes local real-time transcription and summaries, with Whisper/Parakeet transcription and Ollama recommended for summaries ([repository](https://github.com/Zackriya-Solutions/meetily), README sections “Introduction”, “Features”, and “Key Features in Action”). It explicitly says recordings, transcripts, and models are stored locally, and lists an MIT license. The README's professional audio mixing feature captures microphone and system audio simultaneously with intelligent ducking and clipping prevention. It also supports importing existing audio and reprocessing it with another model or language. The repository advertises macOS and Windows support, with Linux build instructions.

Useful ideas: separate capture concerns from inference, import/reprocess, local Ollama summaries, and an explicit mixed-audio policy. Treat diarization as a moving target: the same README advertises it in the repository description while its PRO section says speaker diarization was planned for a later release. Verify the checked-out implementation before adopting a capability as a requirement.

### OpenWhispr comparison

The supplied comparison page claims local Whisper/Parakeet transcription, diarization, offline operation, system-audio capture, and push-to-talk dictation ([comparison](https://openwhispr.com/compare/granola)). The current [OpenWhispr repository README](https://github.com/OpenWhispr/openwhispr) documents local or cloud routes for transcription, AI reasoning, diarization, and semantic search, plus notes, file import, meeting capture, and an MIT license. It is the closest product-workflow comparison to a tray app combining dictation and meetings. The comparison page differs from current first-party material on some platform/calendar details; do not use its Granola claims or pricing as current authoritative evidence.

The comparison page is vendor-authored, so it is useful for feature discovery but weaker evidence for implementation details. The repository README is stronger for the project's declared scope, but neither source alone proves exact device-routing behavior or runtime quality. Verify local diarization and routing against checked-out code and target hardware before treating them as acceptance criteria.

### StenographAI (StenoAI)

StenoAI's repository README describes local recording, transcription, summarization, and querying, with external providers optional, and lists an MIT license ([repository](https://github.com/stenolabs/stenoai), README sections “StenographAI” and “Features”). Its capture design is directly relevant: selectable microphone input plus system-audio capture, using Windows loopback, macOS Core Audio Tap, or Linux PipeWire. It has a global record shortcut, a compact transcription pill, append/resume into an existing note, Markdown transcript export, and optional local speaker profiles.

The repository gives useful platform caveats. On Windows alpha, it reports a verified record → live Parakeet → batch transcript → Ollama summary pipeline, including loopback capture and `[You]`/`[Others]` channel labels; transcription is CPU-based and uses ONNX Runtime, with Whisper as an option. Acoustic multi-speaker diarization is documented as macOS-only; Windows/Linux use channel labels. The Windows installer downloads models on first run, so “local” does not mean “works without an initial model acquisition.”

Useful ideas: a persistent record pill, appendable sessions, explicit permission/device error states, and honest per-platform capability reporting. The platform-specific limitations argue for a channel-aware transcript model before promising universal diarization.

### Speakr

Speakr is a self-hosted web application with documented Docker deployment ([repository](https://github.com/murtaza-nasir/speakr)). Its capture documentation is the closest match to the requested routing UX: microphone, computer system audio, browser-tab audio, or both mixed; a per-OS setup guide and virtual-device picker for Pulse/PipeWire monitors, BlackHole, VB-Cable, Voicemeeter, and Stereo Mix. It supports long sessions streamed to the server, existing-file import, watched-folder intake, synced playback, timestamped transcript navigation, custom vocabulary/hotwords, summaries, action/event extraction, and transcript chat.

Its connector architecture supports self-hosted WhisperX, VibeVoice via a self-hosted vLLM server, OpenASR, and several cloud providers. WhisperX enables diarization and voice profiles, but GPU/container infrastructure is required in the documented setup. The README identifies the project as AGPL-3.0. Speakr is therefore a strong feature benchmark for routing, long sessions, and transcript UX, while its web/server architecture is heavier than Snipvoice's standalone tray model.

## Features worth adding to Snipvoice

### First slice: capture foundation

- Recording mode separate from push-to-talk dictation.
- Two independently selected sources: microphone and system output.
- For each source, `OS default` plus explicit enumerated device selection.
- Windows WASAPI loopback for render/output capture; report unavailable capture explicitly. Virtual audio inputs are supported only when explicitly selected, never as a silent substitute.
- Independent source meters and mute toggles, and a track-preserving recording format. Optional mix gain/ducking follows after reliable capture.
- Long-session segmented files with append-only recovery and a recoverable index; avoid requiring one final whole-session merge to access the recording.
- Permission, busy-device, format, and unavailable-loopback diagnostics surfaced in the UI.

### Second slice: local transcript workflow

- Incremental local transcription plus a final timestamped transcript; chunked updates are sufficient initially, with true streaming enabled only after model/runtime validation.
- Reprocess an existing recording with another installed local model or language.
- Per-session correction dictionaries first; ASR vocabulary/hotword biasing only when the backend supports it. The existing deterministic replacements do not establish recognition-time biasing.
- Searchable local history with playback seeking from transcript timestamps.
- Export to Markdown/plain text with recording metadata and source labels; SRT/VTT follows once timing accuracy is verified.

### Later local intelligence

- Local Ollama or a bundled local inference backend for summaries, action items, and Q&A, with transcript-segment references and editable outputs.
- Source attribution (`Microphone`/`System audio`) when tracks are separate. `You`/`Others` can be user-facing aliases, but a room microphone can contain several people.
- Optional acoustic diarization only when the selected local model/runtime and platform support it; do not present it as universal.
- Calendar integration and auto-start suggestions only as opt-in integrations, with offline capture remaining independent.

## Acceptance boundaries

"Offline" should mean that after models are installed, capture, transcription, storage, reprocessing, and local summaries work with network access disabled. Model acquisition is an explicit setup step; support verified local-model import for disconnected machines. Cloud connectors, calendar sync, and external sharing are outside this proposed implementation scope. A benchmark feature is not accepted until tested on the target OS with default devices, manually selected devices, mic-only, system-only, and simultaneous capture.

The proposed capture contract is:

| Control | Behavior |
| --- | --- |
| Sources | Microphone only, system output only, or both |
| Microphone | OS default input, or a manually chosen capture endpoint |
| Speaker output | OS default output, or a manually chosen render endpoint whose audio is captured |
| Transport | Start, stop, pause/resume, and a separate meeting recording hotkey |
| Feedback | Independent level meters, elapsed time, source names, disk/capture errors, and transcription backlog |
| Persistence | Remember default-following intent or a stable endpoint identity; never persist only a transient device-list index |

Selecting a speaker endpoint captures audio already routed there; it does not change the meeting application's playback device. If Teams or another app uses a different output, choose that endpoint. On Windows, offer the default communications role separately from the ordinary OS playback default. An output device picker for playing recordings is a separate control.

Resolve OS defaults when recording starts. During an active meeting, default-following mode should detect changes, close the affected segment, reopen on the new endpoint, and record any gap visibly. Manual mode must remain tied to its chosen endpoint; unplugging it must surface a lost-source state rather than quietly selecting another device. Reconnection creates a new segment and records the transition. Continuing with the remaining source must be visibly partial and governed by an explicit policy.

Pause stops capture from both sources and preserves a gap marker. Muting one source is independent. Audio device clocks can drift: align streams using capture timestamps, preserve discontinuities and silence, and measure synchronization over a long session. Acoustic echo from physical speakers reaching the microphone can duplicate speech; optional echo cancellation is a separate validated feature, not a promise from having two tracks.

## Current Snipvoice gaps and implementation fit

Verified against the local source:

- `source/voice_audio.py`: `AudioCapture(device=None)` already accepts a microphone device and negotiates native rate/channel count. It uses `sounddevice.RawInputStream`; no render-loopback backend is implemented.
- `source/voice_settings.py` and `source/voice_support.py`: there are no saved input/output selections, and the controller calls the capture factory with no arguments. Low-level device acceptance is not a finished device-selection feature.
- `source/voice_audio.py`: `MAX_SAMPLES` caps a take at 30 seconds. Capture keeps the whole take in a Python sample list, sizes queues with duration, and claims one class-wide normalizer slot. Two concurrent ordinary `AudioCapture` instances are not a supported dual-source design.
- `source/voice_history.py`: append-only float32 journaling and atomic metadata are valuable recovery building blocks. The schema assumes one mono 16 kHz audio file, and `load_samples()` reads the whole recording into memory. Meeting sessions need track/segment metadata and bounded readers.
- `source/voice_runtime.py`: the current provider-facing result is plain text. Add structured segments with source, start/end time, text, and model provenance. Capture/chunk times provide coarse timestamps; exact word timing requires backend support or a separately evaluated local aligner.
- `source/voice_catalog.py`: Parakeet and Qwen profiles are user selectable; the Nemotron streaming entry is not. Its presence is not proof that selectable live meeting transcription already works. Reuse the existing ASR boundary initially and benchmark chunked Portuguese/English transcription before changing model defaults.
- `source/snipvoice.pyw`: current history provides a table, retry, and copy. Meeting detail views, audio playback, transcript seeking, notes, tags/search, exports, and local summarization are additional features.

Use a meeting-session controller alongside the dictation controller. Preserve existing push-to-talk target/cancellation behavior. Meeting results belong to the recording library, never an automatic paste into whatever app has focus when a long recording ends. Until concurrent microphone ownership and inference scheduling are proved, show dictation as busy while a meeting owns those resources.

The meeting capture pipeline should be native sources -> timestamped bounded buffers -> per-track disk segments -> local ASR job queue -> structured transcript. Capture must keep saving when transcription falls behind or its model is unavailable; show pending jobs and resume them from disk. Do not solve meeting duration by merely raising the current memory/sample ceiling. Preserve original captured tracks and derive ASR-normalized mono streams separately; make audio format, disk use, and retention explicit. Native callbacks perform only bounded enqueue/copy work, and Tk continues using its single GUI root.

### Windows

[Microsoft WASAPI loopback](https://learn.microsoft.com/en-us/windows/win32/coreaudio/loopback-recording) captures a selected render endpoint in shared mode and does not require hardware Stereo Mix. It captures that endpoint's mix, which may include notifications/music; per-application capture is a later separate feature. Protected/exclusive playback has documented limitations.

The installed `sounddevice` 0.5.5 `WasapiSettings` signature has no loopback option, matching the [documented API](https://python-sounddevice.readthedocs.io/en/0.5.5/api/platform-specific-settings.html). Add a supported Windows capture backend or a small native helper rather than inventing `WasapiSettings(loopback=True)`. Backend selection and any new project dependency need a separate implementation decision. Use stable endpoint IDs and [device/default-change notifications](https://learn.microsoft.com/en-us/windows/win32/api/mmdeviceapi/nn-mmdeviceapi-immnotificationclient).

### macOS

A native Core Audio tap helper is a strong candidate for selected-output capture. Apple's [deviceUID property](https://developer.apple.com/documentation/coreaudio/catapdescription/deviceuid) and [device-stream tap initializer](https://developer.apple.com/documentation/coreaudio/catapdescription/initexcludingprocesses%3Aanddeviceuid%3Awithstream%3A?language=objc) explicitly support capturing audio routed to a particular output device. Include the [system-audio usage description](https://developer.apple.com/documentation/bundleresources/information-property-list/nsaudiocaptureusagedescription) and verify permission denial/recovery in the packaged app. Lock minimum supported OS/SDK versions during the native spike; StenoAI documents macOS 14.4+ for its implementation, not a certified minimum for Snipvoice.

ScreenCaptureKit is another native route to evaluate, but general system-audio capture does not by itself prove manually selected render-endpoint behavior. Existing virtual input devices can be offered explicitly; installing a virtual driver is not assumed. Preserve the current main-thread Tk/Cocoa lifecycle.

### Local summaries and search

ASR models transcribe speech; summarization/Q&A needs a separate local text model, and semantic search may need a local embedding model. Start with ordinary local text search. Reuse installed local ASR models before adding another inference stack; then select a text model using Portuguese/English quality, memory, latency, and long-transcript tests. Do not silently download or switch models when memory is constrained.

Ollama is a candidate optional local runtime, not an installed/verified Snipvoice capability. Its [official FAQ](https://docs.ollama.com/faq) documents default loopback binding and local-only cloud disable settings. Snipvoice must permit only local inference in this feature scope, reject cloud-backed model routes and remote redirects, and verify the complete flow with outbound networking blocked. Do not change the user's host Ollama configuration as part of scouting. Summaries should distinguish decisions, actions, owners, deadlines, and unknowns, cite supporting transcript segments, and never invent absent owners or dates. Manual notes remain separately editable and preserved when AI reports are regenerated.

## Proposed delivery order and proof

1. **Capture spike, Windows first:** enumerate and select default/manual microphone and render endpoints; capture mic-only, system-only, and both into separate synchronized recoverable files. Then prove the equivalent macOS backend before claiming platform parity.
2. **Recording feature:** long disk-backed sessions, start/stop/pause, meters, disconnect/default-change handling, crash recovery, playback, and local queued transcription. Validate at least a two-hour dual-source session with bounded memory, drift measurements, and recoverability.
3. **Meeting workspace:** timestamped transcript navigation, titles, manual notes/bookmarks, existing-recording reprocessing, WAV import first, Markdown/text export, and local search. Broader codec support and subtitle export follow verified decoder/timing support.
4. **Local intelligence:** summaries/action templates with evidence links, optional local diarization and manual speaker renaming, then cross-meeting Q&A with citations. Cross-meeting voice profiles are a separate opt-in feature.

Acceptance must include headphones and physical speakers, 44.1/48 kHz inputs, USB/Bluetooth disconnects, OS-default changes, permission denial, one-source failure, silence, clipping/overflow, disk-full handling, model cancellation, crash/restart recovery, and slower-than-realtime ASR. Verify offline operation including cold app startup with already provisioned models and no access to model hubs. Report segment/chunk timing honestly and preserve existing dictation regression behavior.

## Verification limits of this scout

The local Windows `sounddevice` enumeration succeeded: 26 input-capable entries and 21 output-capable entries across MME, DirectSound, WASAPI, and WDM-KS. These are API entries, not unique physical devices. No microphone or speaker audio was captured, no private recordings/settings/models were inspected, no competitor was installed, and no dependency or app source was changed. The earlier test run on this interpreter exposed missing `soxr` and unusable Tcl initialization; those environment gaps remain unresolved and prevent full GUI/native validation. Only this research note was added.
