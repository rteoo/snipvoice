> Historical Sniptype record, retained unchanged from the predecessor. Release, path, and issue references describe Sniptype. See [extraction.md](extraction.md) for the current Snipvoice boundary.

# Voxtype speech-to-text review

**Date:** 2026-08-31
**Scope:** inspect the upstream Voxtype repository and website for improvements to Sniptype's local voice input. This is a research note; no Voxtype code is copied and no Sniptype runtime behavior is changed here.
**Upstream snapshot:** Voxtype `dev` commit `2772bbbaf27c4585ddb91661855fb7e2b8d93492`; latest release `v1.0.1` (published 2026-08-31).

## Decision

Borrow Voxtype's operational patterns, especially explicit capture failure handling, model lifecycle management, conservative text cleanup, and commit-only streaming semantics. Keep Sniptype's existing provider boundary and offline-only product decision. Do not add Voxtype's remote mode, shell post-processing, Linux-specific integration, or a second ASR runtime without a separate approval and dependency review.

Voxtype is a Linux-first Rust daemon with a broad engine matrix. Its strongest lessons are at the reliability seams, not a reason to replace Sniptype's `LocalVoiceProvider`/`transcribe.cpp` path. The project README claims local operation, no telemetry, CPU speed of 9–11× realtime for its then-featured Cohere model, seven engines, dynamic loading, and fallback output paths; those are upstream claims and are not measurements of Sniptype hardware or models. [Voxtype README](https://github.com/peteonrails/voxtype/blob/dev/README.md#readme)

## Implemented adaptation (2026-09-01)

Sniptype now negotiates the microphone's native rate and mono/stereo layout,
normalizes capture to mono 16 kHz float32 through a stateful SoXR stream, and
flushes its tail before inference. Capture faults are structured outcomes:
available canonical audio is preserved as failed history, while transcription
and paste are blocked. History records source format and inference timing and
rejects empty or misaligned retries.

The text-cleanup adaptation is deliberately narrower than Voxtype's. Sniptype
adds opt-in, deterministic user term corrections for dictation and form input,
retains a changed raw transcript in history, and leaves command matching
untouched. Spoken punctuation, filler removal, external commands, Cohere, and
remote processing remain out of scope.

## What Voxtype does

### Capture and audio preparation

Voxtype uses CPAL for cross-platform capture and deliberately puts the non-`Send` audio stream on a dedicated thread, communicating through channels. It discovers the default device or matches a configured device by exact, case-insensitive, then substring name; it converts supported sample formats to mono `f32`, resamples to the configured rate, and has bounded stop/get-samples waits. On stream failure it marks the stream dead and reports that the recording may be truncated; on shutdown it flushes the resampler tail so the end of an utterance is not silently lost. [cpal_capture.rs](https://github.com/peteonrails/voxtype/blob/2772bbbaf27c4585ddb91661855fb7e2b8d93492/src/audio/cpal_capture.rs), [Voxtype v1.0.1 release notes](https://github.com/peteonrails/voxtype/releases/tag/v1.0.1)

The v1.0.1 release is unusually relevant to Sniptype: Voxtype reports that its earlier linear downsampler aliased frequencies above 8 kHz into the speech band, reset fractional position on every callback, and discarded the remaining resampler buffer when push-to-talk ended. The repaired pipeline uses stateful band-limited resampling and flushes the tail. These are upstream regression findings, not independent Sniptype measurements, but they identify a concrete capture-quality gate we currently do not exercise. [Voxtype v1.0.1 release notes](https://github.com/peteonrails/voxtype/releases/tag/v1.0.1), [stateful callback resampling](https://github.com/peteonrails/voxtype/blob/2772bbbaf27c4585ddb91661855fb7e2b8d93492/src/audio/cpal_capture.rs)

It also has optional GTCRN enhancement: a small ONNX speech-enhancement model processes 16 kHz mono audio through STFT frames with recurrent state to suppress noise and echo. This is an optional accelerator/quality layer, not a replacement for ASR. [enhance.rs](https://github.com/peteonrails/voxtype/blob/dev/src/audio/enhance.rs#L0-L9), [enhancement pipeline](https://github.com/peteonrails/voxtype/blob/dev/src/audio/enhance.rs#L20-L43)

Voxtype's VAD is explicitly configurable: energy VAD is fast and model-free; Whisper/Silero VAD is more accurate but needs a model; minimum speech duration prevents silence-only clips from reaching Whisper and reduces hallucinations. [Configuration: VAD](https://github.com/peteonrails/voxtype/blob/dev/docs/CONFIGURATION.md#vad)

### Provider and model lifecycle

The upstream `Transcriber` trait accepts the normalized contract Sniptype already wants—mono 16 kHz `f32` samples to text—and includes optional preparation, timed transcription, streaming capability, and last-detected-language metadata. The factory selects engines behind that trait and compile-time feature gates optional engines. [transcribe/mod.rs](https://github.com/peteonrails/voxtype/blob/dev/src/transcribe/mod.rs#L0-L23), [transcriber trait](https://github.com/peteonrails/voxtype/blob/dev/src/transcribe/mod.rs#L76-L139), [engine factory](https://github.com/peteonrails/voxtype/blob/dev/src/transcribe/mod.rs#L141-L167)

The model manager has three useful lifecycle modes: cached models with LRU eviction, on-demand loading, and a fresh subprocess for GPU isolation. It validates requested models, preloads the primary model only when configured, and can prepare a subprocess while the user is still speaking. [model_manager.rs](https://github.com/peteonrails/voxtype/blob/dev/src/model_manager.rs#L0-L6), [availability and selection](https://github.com/peteonrails/voxtype/blob/dev/src/model_manager.rs#L45-L95), [LRU eviction](https://github.com/peteonrails/voxtype/blob/dev/src/model_manager.rs#L127-L197), [prepare while recording](https://github.com/peteonrails/voxtype/blob/dev/src/model_manager.rs#L199-L289)

The default config makes the trade-off visible: keep a model loaded for response time, or load on demand to reduce idle memory; it also exposes CPU thread count and a short-recording context-window optimization with a warning about repetition loops. [default.toml](https://github.com/peteonrails/voxtype/blob/dev/config/default.toml#L920-L975)

### Streaming and cancellation

Voxtype's streaming output defaults to **commit-only**: partials update status but never touch the cursor; only finalized segments are typed. The rationale is sound for revision-style decoders, where partial text can be rewritten. On cancellation, it tracks the number of actually typed Unicode scalar values and attempts a best-effort backspace rewind. [streaming.rs](https://github.com/peteonrails/voxtype/blob/dev/src/output/streaming.rs#L0-L31), [streaming session state](https://github.com/peteonrails/voxtype/blob/dev/src/output/streaming.rs#L50-L66)

This maps directly to Sniptype's current caution that live ASR is unproven: if live streaming is added later, commit-only final segments should be the default and any partial-typing mode should be an explicit provider capability, not a global assumption.

### Text cleanup

Voxtype has a deterministic text processor for spoken punctuation, case-insensitive word-boundary replacements, filler-word filtering, punctuation/space repair, and an optional spoken “submit” command. Its implementation is careful about multilingual collisions: the default `um` filler is excluded for Portuguese and German because it is a real word there; explicit user replacements remain explicit. [text/mod.rs](https://github.com/peteonrails/voxtype/blob/dev/src/text/mod.rs#L0-L83), [configuration: replacements and filler filtering](https://github.com/peteonrails/voxtype/blob/dev/docs/CONFIGURATION.md#replacements)

It also supports an optional external post-process command with a timeout and fallback to the original transcript on missing command, non-zero exit, timeout, or empty output. That feature is powerful but has a documented 2–5 second latency cost and shell/LLM safety complexity. [configuration: post-processing](https://github.com/peteonrails/voxtype/blob/dev/docs/CONFIGURATION.md#outputpost_process)

### Output and platform integration

Voxtype separates output backends and tries a fallback chain: direct typing, paste/clipboard, and platform-specific tools. Its clipboard implementation deliberately sends stdout to null, writes through stdin, closes stdin to signal EOF, waits for the child, and reports non-zero exit. [clipboard.rs](https://github.com/peteonrails/voxtype/blob/dev/src/output/clipboard.rs#L0-L16), [clipboard output](https://github.com/peteonrails/voxtype/blob/dev/src/output/clipboard.rs#L28-L78)

The product supports compositor-native push-to-talk bindings, toggle mode, status files, notifications, audio feedback, and Linux-specific integrations such as Wayland/X11 typing and MPRIS media pause. It also ships packages through Linux distributions, Homebrew on macOS, and signed reproducible release binaries. These are useful product patterns but mostly outside Sniptype's Windows-first scope. [README usage and keybindings](https://github.com/peteonrails/voxtype/blob/dev/README.md#compositor-keybindings), [README packaging and trust](https://github.com/peteonrails/voxtype/blob/dev/README.md#trust)

## Comparison with Sniptype

| Area | Sniptype today | Voxtype lesson | Judgment |
|---|---|---|---|
| Provider boundary | `LocalVoiceProvider` owns runtime/model lifecycle and wraps `transcribe.cpp`; no cloud fallback | Trait-based transcribers with optional preparation, streaming, timing, and language metadata | Keep Sniptype's boundary; add capability metadata only where a real consumer needs it |
| Capture | Push-to-talk session, bounded audio/history state machine | Dedicated capture thread, explicit device matching, stream-dead state, resampler flush, bounded waits | High-value reliability improvements; adapt to existing `voice_audio`/history seam |
| Endpointing | Hotkey release is authoritative; optional VAD is planned | Energy or model VAD with threshold and minimum speech duration | Add silence rejection only behind measured tests; do not replace release endpoint casually |
| Models | SHA256-pinned on-demand catalog; one resident model; verified resumable downloads | LRU cache, idle eviction, preload/prepare overlap, optional process isolation | Add lifecycle metrics and preparation; one resident model remains safer for Sniptype memory budget |
| Streaming | Completed-utterance path; live ASR not proven | Final-only commit policy and cancellation rewind | Strong design reference for a later streaming provider |
| Cleanup | Provider returns transcript; insertion path preserves clipboard safety | Deterministic punctuation/replacements/filler filtering before optional post-process | Add deterministic, language-aware cleanup as a separate pure helper |
| Output | Clipboard-first Windows insertion with safe multiline behavior and history retry | Explicit output backend chain and fallback | Reuse the failure-state model; do not copy Linux tool fallbacks or typed multiline behavior |
| Privacy | Local-only, no Gemini/cloud fallback approved; saved audio retry is explicit | Local by default, but remote Whisper and shell/LLM hooks exist | Keep remote and arbitrary shell processing out of the default product |
| Platform | Windows-first, macOS support, Tk/pystray/pynput | Linux compositor/evdev/Wayland-first daemon plus macOS packaging | Do not import Linux architecture into Sniptype |

## Prioritized improvements

### P0 — capture failure truthfulness and end-of-stream integrity

**Recommendation:** first negotiate the device's supported native format, convert to mono, and use one stateful band-limited resampler for the whole recording, including an explicit tail flush. Then make device disconnect, stream callback failure, stop timeout, and resampler/tail loss first-class outcomes in Sniptype's audio state machine. Preserve the recording in history as failed or partial, notify the user, and never present a truncated transcript as complete.

**Evidence:** Voxtype marks a failed CPAL stream dead and warns that the transcript may be short or empty; it flushes the resampler before returning samples and bounds stop waits to two seconds. [capture failure handling](https://github.com/peteonrails/voxtype/blob/dev/src/audio/cpal_capture.rs#L186-L216), [stop and flush](https://github.com/peteonrails/voxtype/blob/dev/src/audio/cpal_capture.rs#L257-L354)

**Cost/risk:** medium. This should fit the existing `voice_audio` and `voice_history` seams, but requires Windows device-disconnect tests and careful preservation of the no-blind-paste rule.

### Model implication — do not add Cohere from this review

Voxtype's current headline engine is Cohere Transcribe through a separate ONNX stack. Its implementation supports Portuguese and enables punctuation plus inverse text normalization, but adopting it would add another runtime, model format, packaging matrix, and license review. It should only become a benchmark candidate if the shared pt-BR/English corpus shows a material accuracy gain over Parakeet and Qwen; this review supplies no such Sniptype measurement. [Cohere transcriber implementation](https://github.com/peteonrails/voxtype/blob/2772bbbaf27c4585ddb91661855fb7e2b8d93492/src/transcribe/cohere.rs), [Voxtype README](https://github.com/peteonrails/voxtype/blob/2772bbbaf27c4585ddb91661855fb7e2b8d93492/README.md)

### P0 — expose provider capability and timing evidence

**Recommendation:** extend the local provider's internal status/result metadata with model id, runtime, detected language (when available), preparation time, capture duration, inference duration, and output route. Keep UI-facing behavior backward-compatible.

**Evidence:** Voxtype's transcriber contract explicitly includes `prepare`, timed segments, streaming capability, and last detected language; its manager distinguishes preload, prepare, cached load, and subprocess isolation. [transcriber contract](https://github.com/peteonrails/voxtype/blob/dev/src/transcribe/mod.rs#L85-L139), [lifecycle manager](https://github.com/peteonrails/voxtype/blob/dev/src/model_manager.rs#L199-L297)

**Cost/risk:** low to medium. Mostly instrumentation and result-shape work; avoid persisting sensitive audio or transcript data beyond existing history policy.

### P1 — deterministic, language-aware cleanup

**Recommendation:** add a pure `voice_text_support` stage for opt-in spoken punctuation and user replacement tables, with Portuguese-safe filler defaults and exact word boundaries. Keep the raw provider transcript available in history and make cleanup failures return the original text.

**Evidence:** Voxtype precompiles replacement/cleanup regexes and avoids treating Portuguese `um` as an English filler; its external post-process path falls back to original text on every failure class. [text processor](https://github.com/peteonrails/voxtype/blob/dev/src/text/mod.rs#L9-L83), [post-process failure contract](https://github.com/peteonrails/voxtype/blob/dev/docs/CONFIGURATION.md#outputpost_process)

**Cost/risk:** medium. The main risk is silently changing dictated prose or code; ship disabled by default and test pt-BR, English, names, code-switching, and literal words such as “um”.

### P1 — prepare the selected model during recording

**Recommendation:** if model load latency remains visible, prepare the already-selected model when recording starts while capture proceeds, then await readiness before inference. Do not switch models implicitly and do not open capture before a selected model is known.

**Evidence:** Voxtype starts a subprocess worker and loads the model while the user speaks, explicitly awaiting preparation before transcription to avoid duplicate workers. [prepare_model](https://github.com/peteonrails/voxtype/blob/dev/src/model_manager.rs#L224-L289)

**Cost/risk:** medium to high. Requires proving transcribe.cpp thread/process safety and ensuring cancellation does not leave a resident or orphaned worker. The current one-resident-model rule should remain.

### P2 — commit-only streaming contract, if live ASR is pursued

**Recommendation:** define partials as status-only and commit only finalized text; track typed character count for cancellation rewind. Keep this behind a provider capability and never expose it for Parakeet/Qwen until the runtime produces stable segment semantics.

**Evidence:** Voxtype documents why revision-style partials should not touch the cursor and implements best-effort rewind based on actual typed Unicode scalar count. [streaming policy](https://github.com/peteonrails/voxtype/blob/dev/src/output/streaming.rs#L0-L43), [session tracking](https://github.com/peteonrails/voxtype/blob/dev/src/output/streaming.rs#L50-L66)

**Cost/risk:** high. It changes routing, cancellation, and UI status; no reason to spend this risk before completed-utterance accuracy and latency are proven.

### P2 — optional speech enhancement/VAD experiments

**Recommendation:** benchmark a small denoiser and energy VAD against the existing pt-BR/English corpus and real microphone noise before adopting either. Treat them as measurable preprocessing options, not automatic quality wins.

**Evidence:** Voxtype's GTCRN is a separate 16 kHz STFT/ONNX stage, while its VAD distinguishes cheap energy detection from a model-backed detector and rejects short/silent clips. [GTCRN design](https://github.com/peteonrails/voxtype/blob/dev/src/audio/enhance.rs#L0-L43), [VAD modes](https://github.com/peteonrails/voxtype/blob/dev/docs/CONFIGURATION.md#vad)

**Cost/risk:** high. A second native/ONNX asset raises packaging, licensing, CPU, memory, and failure-surface costs. Do not add the `silero-vad` Python package or a second runtime merely because Voxtype supports one.

## What not to copy

- **Remote Whisper mode or optional remote servers.** It conflicts with the current local/offline product boundary and would create privacy, credentials, and failure-policy work. Voxtype exposes it as a configurable mode; Sniptype should not.
- **Arbitrary shell/LLM post-processing.** It can leak dictated text, execute user-configured commands, add seconds of latency, and produce output that is unsafe for clipboard/paste. If cleanup is added, start with an in-process deterministic transform and preserve original text on failure.
- **Seven-engine feature breadth.** The engine matrix increases build artifacts, model licensing, QA, memory pressure, and user choice cost. Parakeet remains the default and Qwen3-ASR 0.6B remains an explicit local option until corpus evidence supports another model.
- **Linux-specific output fallback behavior.** `wtype`, `dotool`, `ydotool`, evdev, compositor submaps, MPRIS, and Waybar do not map cleanly to Sniptype's Windows clipboard/Tk/pystray architecture.
- **Typing partials directly.** Voxtype's own commit-only policy is the safer default for revision-style ASR and aligns with Sniptype's current reliability stance.
- **Keeping several heavyweight models resident by default.** Voxtype supports LRU caching, but Sniptype's one-resident-model rule is easier to reason about on ordinary Windows machines and avoids memory spikes.

## Proposed implementation order

1. Add P0 capture outcome/tail-loss tests and status metadata without changing model selection.
2. Add deterministic cleanup as an isolated, disabled-by-default pure helper; benchmark against the existing pt-BR/English examples.
3. Add model preparation and timing instrumentation only if measured cold-load latency justifies the concurrency complexity.
4. Revisit VAD, denoising, and streaming only with corpus and hardware measurements that demonstrate a material user-visible win.

## Sources inspected

- [Voxtype repository](https://github.com/peteonrails/voxtype)
- [Voxtype website](https://voxtype.io/)
- [README](https://github.com/peteonrails/voxtype/blob/dev/README.md)
- [Transcriber trait and factory](https://github.com/peteonrails/voxtype/blob/dev/src/transcribe/mod.rs)
- [Model manager](https://github.com/peteonrails/voxtype/blob/dev/src/model_manager.rs)
- [CPAL capture](https://github.com/peteonrails/voxtype/blob/dev/src/audio/cpal_capture.rs)
- [GTCRN enhancement](https://github.com/peteonrails/voxtype/blob/dev/src/audio/enhance.rs)
- [Streaming output](https://github.com/peteonrails/voxtype/blob/dev/src/output/streaming.rs)
- [Clipboard output](https://github.com/peteonrails/voxtype/blob/dev/src/output/clipboard.rs)
- [Text processor](https://github.com/peteonrails/voxtype/blob/dev/src/text/mod.rs)
- [Configuration reference](https://github.com/peteonrails/voxtype/blob/dev/docs/CONFIGURATION.md)
- [Default configuration](https://github.com/peteonrails/voxtype/blob/dev/config/default.toml)
