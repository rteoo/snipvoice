> Historical Sniptype record, retained unchanged from the predecessor. Release, path, and issue references describe Sniptype. See [extraction.md](extraction.md) for the current Snipvoice boundary.

# Voice Input — Evaluation and Integration Plan

## Implementation status (2026-09-08)

The opt-in module is implemented and remains default-off. The current Windows
release target is 3.5.0; the macOS ARM64 artifact remains the 3.5.0 beta 2 preview.
**Live microphone-to-paste ASR is not certified by the offline tests.** The
remaining physical-desktop items below need separate target-machine evidence.

| Implemented and covered | Remaining physical evidence or product decision |
|---|---|
| Default-off push-to-talk with dedicated observer and exact end-of-hold semantics | Live microphone-to-paste ASR on Windows x64 and macOS ARM64 |
| Dictation, spoken-trigger, and form-field dispatch without `_dispatch_expansion` | Parakeet and Qwen latency/accuracy adoption-gate measurements on target hardware |
| User-selectable Balanced/Parakeet, Compact/Qwen 0.6B, and Accuracy/Qwen 1.7B profiles; Qwen forces automatic language detection | Handy-alongside shortcut and focus composition test |
| SHA256 catalog and range-validated resumable model downloads | Real model-download interruption and recovery smoke on target machines |
| Exact pinned release dependencies plus packaged native-runtime probe and Windows package smoke | Windows installer install/upgrade/uninstall smoke test |
| ARM64 macOS package probe and physical focus check for the non-activating recording panel | Signed macOS build from an interactive keychain session and granted-TCC paste smoke |
| Failed backend import, controller construction, or profile swap leaves normal expansion available | Nemotron live streaming and its OpenMDW-1.1 review |
| Cancellation stops capture, calls `session.cancel()`, and boundedly joins workers | iOS (on hold) |
| Manager **Entrada por voz** tab mirrors tray enable, live status, and settings | Physical manager/tray synchronization smoke on supported desktops |
| Provider boundary owns runtime/model lifecycle; local transcribe.cpp behavior remains unchanged | Additional providers only after an explicit product decision |
| Append-only audio journal, atomic history metadata, startup recovery, and safe manual retry | User-controlled retention and deletion policy |
| Native device-rate capture normalized to mono 16 kHz float32 with stateful SoXR conversion and tail flush | Physical device-loss and final-word smoke tests on supported desktops |
| Structured capture issues, canonical failed-audio history, inference timing, and opt-in deterministic term corrections | Broader cleanup only after corpus evidence; no filler removal or LLM post-processing |

Enabling **Entrada por voz** without the optional native backend reports
unavailable. That is expected. iOS is out of scope until this Windows-first
path is proven.

The manager tab is a discoverability surface for the existing voice controls;
it does not change the packaged-runtime and live-ASR release gates.

Remaining work is measurement and physical packaged-runtime validation, not
more product surface. Do not add `sherpa-onnx` until there is a pinned ONNX
catalog; the catalog artifacts are GGUF for transcribe.cpp.

## Evaluation

This document also records why the feature is an opt-in module rather than a
fork. It was originally framed as "which Wispr Flow alternative should we
fork?" The short answer is that forking is the wrong shape; the reasoning is
below.

## Evidence and its limits

Everything marked **[verified]** was read from source in a shallow clone of the
project on 2026-08-13, or pulled from the GitHub API on the same date.
**[reported]** means it comes from the project's own README, docs, or model
catalog metadata and was not independently confirmed. **[inference]** is
reasoning, not measurement.

**No application was installed, built, or benchmarked on Sniptype's target
machines.** The model comparison now includes reproducible measurements from
the model authors and portable runtimes, but those results come from other
hardware. Before committing to an engine, run the same models on the supported
Windows and macOS targets — that remains the largest evidence gap.

## Verdict

Do not fork any of the three candidates. Build voice as an opt-in module with
download-on-demand models and explicit routes into the existing insertion,
trigger-expansion, and form-input paths.

The product exposes four explicit model profiles:

1. **Balanced — default:** Parakeet TDT 0.6B v3 Q8/INT8.
2. **Compact Qwen — optional:** Qwen3-ASR-0.6B Q8.
3. **Maximum accuracy — optional:** Qwen3-ASR-1.7B Q8.
4. **Live streaming — optional:** Nemotron 3.5 ASR Streaming 0.6B Q8/INT8.

The user selects a profile; the app never changes profiles or downloads a
larger model automatically. Only one ASR model is resident at a time. The
default first release remains push-to-talk with completed-utterance decoding;
the live-streaming profile adds partial transcripts without changing transcript
routing or insertion semantics.

Prefer one native inference runtime for all four profiles. `transcribe.cpp`
currently supports their Q8 GGUF builds through a common C API and publishes
same-runtime CPU benchmarks. Benchmark its Python binding and packaged native
library first. Retain `sherpa-onnx` as the fallback for Parakeet and Nemotron if
the unified runtime fails packaging or behavior gates; do not ship two inference
stacks merely to expose an optional profile.

Audio capture uses `sounddevice`. A SHA256-pinned
`silero_vad.onnx` is optional because hotkey release already supplies an
endpoint. When using sherpa, load it through sherpa's VAD API. Do **not** add
the `silero-vad` Python package: its published package requires `torch` and
`torchaudio`, recreating the bundle weight this plan aims to remove.
The capture module normalizes the selected device stream to mono
16 kHz float32 before inference. When the input device runs at another sample
rate, it uses the pinned `soxr==1.1.0` streaming resampler and flushes its delay line
on stop; a successful recording must not silently lose the final resampler
tail. `soxr` bundles libsoxr under LGPLv2.1+ and PFFFT under its BSD-like
license, so the packaged third-party notice must ship both attributions.

Rationale in [Why not a fork](#why-not-a-fork) and
[Proposed shape](#proposed-shape).

### Adoption gates

Set up a repeatable corpus and measurement harness before changing project
dependencies. The provisional go/no-go gates are:

| Dimension | Balanced default | Compact Qwen | Maximum accuracy | Live streaming |
|---|---|---|---|---|
| Result latency | Warm end-of-speech → paste p95 ≤ 1.5 s for utterances up to 15 s | Warm end-of-speech → paste p95 ≤ 2.0 s | Warm end-of-speech → paste p95 ≤ 3.0 s; UI labels the slower profile | First stable partial p95 ≤ 1.0 s; final paste p95 ≤ 1.5 s after release |
| Cold model load | ≤ 10 s, with visible non-blocking progress | ≤ 12 s, with visible non-blocking progress | ≤ 20 s, with visible non-blocking progress | ≤ 10 s, with visible non-blocking progress |
| Inference speed | real-time factor ≤ 0.5 | real-time factor ≤ 0.5 | real-time factor ≤ 0.8 | sustained real-time factor ≤ 0.5 |
| Peak process RSS | ≤ 1.5 GB | ≤ 3 GB and never selected automatically | ≤ 6 GB and never selected automatically | ≤ 1.5 GB |
| Accuracy | pt-BR and en-US each no worse than 10% relative WER above Whisper Medium Q8; named entities separate | Must remain within 15% relative WER of Balanced and offer a corpus-specific robustness win | Must beat Balanced materially on the same corpus: ≥10% relative WER reduction overall or ≥15% named-entity error reduction | Must remain within 20% relative WER of Balanced while meeting partial-latency gate |

Common gates apply to every profile:

| Dimension | Gate |
|---|---|
| Idle cost while voice is enabled | < 1% CPU; no open microphone stream |
| Packaged support | Windows x64 and macOS ARM64 smoke tests pass from clean installs |
| Failure behavior | denied permission, missing device, corrupt model, offline download, cancellation, and paste failure all return safely to idle |

An optional profile failing its own gate does not block the Balanced release;
it stays hidden or experimental until it passes.

The corpus must include punctuation, Brazilian names and addresses, English
technical vocabulary, mixed pt-BR/English speech, trigger names, mapping items,
and form-field values. Record model version, quantization, hardware, OS, Python,
thread count, cold/warm state, latency, CPU, RSS, and transcript for every run.

### Model profiles and current evidence

WER is lower-is-better. Model-card rows and runtime benchmarks use different
normalization and datasets unless explicitly stated, so they define the
benchmark order, not the final winner. Full citations and qualifications live
in [voice-model-value-research.md](voice-model-value-research.md).

| Profile | Model | Accuracy evidence | Q8 footprint | Portable CPU evidence | Product definition |
|---|---|---|---:|---|---|
| **Balanced (default)** | Parakeet TDT 0.6B v3 Q8/INT8 | NVIDIA FLEURS: pt 4.76%, en 4.85% on the reference checkpoint. In transcribe.cpp, English LibriSpeech test-clean is 1.94% Q8 versus 1.95% F32. Quantized pt-BR WER is unmeasured. | 740 MB GGUF; about 640 MB sherpa ONNX | 27–29x realtime on M4 Max CPU; 7–8x on Ryzen 4750U CPU | Completed-utterance push-to-talk. Smallest accuracy-oriented default. No live partials. |
| **Compact Qwen** | Qwen3-ASR-0.6B Q8 | transcribe.cpp Q8 scores 2.11% on LibriSpeech test-clean. No isolated pt-BR result exists, so the local corpus remains the adoption gate. | 850 MB GGUF | About 16–17x realtime on M4 Max CPU; 4.1–4.6x on Ryzen 4750U CPU | Explicit opt-in alternative between Parakeet and Qwen 1.7B. Completed-utterance decoding with automatic language detection. |
| **Maximum accuracy** | Qwen3-ASR-1.7B Q8 | Qwen reports FLEURS-en 3.35% and 4.90% multilingual FLEURS across a set including Portuguese; no isolated pt-BR WER. transcribe.cpp Q8 scores 1.61% on LibriSpeech test-clean versus BF16 1.62%. | 2.08 GB GGUF | About 8x realtime on M4 Max CPU; 1.9–2.1x on Ryzen 4750U CPU | Explicit opt-in download with resource warning. Completed-utterance decoding in v1. Use only after it proves a material corpus win. |
| **Live streaming** | Nemotron 3.5 ASR Streaming 0.6B Q8/INT8 | NVIDIA at 560 ms with language supplied: pt 5.65%, en 7.99%. transcribe.cpp Q8 at the 1.12 s tier scores 7.88% English versus reference 7.99%. Quantized pt-BR WER at 560 ms is unmeasured. | 716 MB GGUF; about 650 MB sherpa ONNX | 28–30x realtime on M4 Max CPU; 7–8x on Ryzen 4750U CPU | Stateful partial transcripts. Default to 560 ms and an explicit `pt-BR` or `en-US` language hint; auto-detect remains optional because published English accuracy is weaker. |

Whisper large-v3-turbo Q8 remains a benchmark candidate, not a user profile.
Its transcribe.cpp build is 845 MB and scores 2.01% on LibriSpeech test-clean,
but published CPU throughput is only 0.6–0.9x realtime on Ryzen 4750U and
1.4–2.3x on M4 Max. Add it only if the shared pt-BR/code-switching corpus shows
a material accuracy or robustness win; using transcribe.cpp avoids a second
CTranslate2/faster-whisper dependency path.

Reference-precision Parakeet is a benchmark control, not a product profile: its
2.51 GB artifact has no demonstrated accuracy advantage over Q8 sufficient to
justify a roughly 3.4x larger download. Keep it out of the model catalog unless
the local corpus proves otherwise.

## Candidates evaluated

| | Handy | Buzz | OpenWhispr |
|---|---|---|---|
| Repo | `cjpais/Handy` | `chidiwilliams/buzz` | `OpenWhispr/openwhispr` |
| Stars / created | 29.4k · 2025-02 | 20.9k · 2022-09 | 5.4k · 2025-06 |
| Latest release | v0.9.5 · 2026-08-08 | v1.4.4 · 2026-03-14 | v1.8.3 · 2026-08-13 |
| Open issues | 152 | 22 | 287 |
| Stack | Rust + Tauri 2 | Python + PyQt | Electron 41 + JS |
| License | MIT | MIT | MIT |
| Hotkey → type into active app | yes | **no** | yes |
| Telemetry | none found | **PostHog, default on** | opt-in, default off |
| Account required | no | no | optional, skippable |
| Hidden cost | none | none | 2k words/week free cap (cloud path only) |

All figures **[verified]** via GitHub API, 2026-08-13.

### Handy — strongest of the three

- **No telemetry.** Grepped the full Rust and TypeScript tree for PostHog,
  Sentry, Amplitude, Segment, and Google Analytics. No hits, and no analytics
  SDK among the dependencies. **[verified]**
- **Cloud post-processing is opt-in and off.** `default_post_process_enabled()`
  returns `false` in `src-tauri/src/settings.rs:593`. The OpenAI / Anthropic /
  Groq / DeepSeek endpoints present in the tree are BYOK LLM cleanup of already
  transcribed text, not the transcription path. **[verified]**
- **Model downloads are SHA256-verified against a catalog compiled into the
  binary**, which serves as the trust anchor; mirror entries lacking a hash are
  rejected. Updates are minisign-signed, served from GitHub releases.
  **[verified]** This is a stronger supply-chain posture than most commercial
  equivalents and is the single most worthwhile pattern to copy.
- **Security-aware code.** `secure_input.rs` handles macOS Secure Event Input
  with an explicit guarantee that key identity is never logged. API keys are
  redacted in `Debug` output, with tests asserting it. **[verified]**
- **Tauri capabilities are scoped** — filesystem access limited to `$APPDATA`
  rather than the whole disk. **[verified]**

Weaknesses: `"csp": null` in `tauri.conf.json` (no Content-Security-Policy) and
an `assetProtocol` scope of `**`. Low practical risk given the frontend loads
only bundled local assets **[inference]**. BYOK API keys are stored in the Tauri
store as plaintext JSON under `%APPDATA%` rather than in Windows Credential
Manager — only relevant if post-processing is enabled **[verified]**.

Model catalog: 67 entries **[verified]**, current generation (Parakeet TDT v3,
Qwen3-ASR, Canary, Nemotron Streaming, Moonshine, Whisper). Sizes below are the
default quantization for each; the speed and accuracy scores are Handy's own
catalog metadata and **are not independent benchmarks** **[reported]**.

| Model | Default quant | Size | Langs | pt | Speed | Acc |
|---|---|---|---|---|---|---|
| Canary 180M Flash | Q8_0 | 218 MB | 4 | no | 98 | 88 |
| Parakeet Unified EN 0.6B | Q8_0 | 731 MB | 1 | no | 79 | 90 |
| Nemotron Streaming 3.5 | Q8_0 | 751 MB | 28 | **yes** | 84 | 82 |
| Whisper Medium | Q8_0 | 832 MB | 99 | **yes** | 42 | 84 |
| Cohere Transcribe | Q5_K_M | 1.77 GB | 14 | **yes** | 63 | 92 |

Parakeet TDT 0.6B v3 (25 languages, pt included) is also in the catalog and is
the likely pt-BR + English choice. **[verified]**

### Buzz — wrong category

Buzz has **no global hotkey and no injection into the active application**. Its
only output paths are clipboard copy and the transcript viewer. It is a
transcription studio — files, YouTube URLs, diarization, SRT export — not a
dictation layer. **[verified]** It cannot serve as the basis for this feature
regardless of its other qualities.

Separately, worth knowing: it reports to PostHog on every launch.

```python
# buzz/widgets/application.py:83
posthog.capture(distinct_id=self.settings.get_user_identifier(), event="app_launched",
                properties={app, locale, system, release, machine, version})
```

Persistent UUID, on by default, opt-out only via the `BUZZ_DISABLE_TELEMETRY`
environment variable — documented in `docs/preferences.md`, not the README.
No audio or transcript content is transmitted. **[verified]**

Its residual value to us is packaging know-how, covered below.

### OpenWhispr — open-core, not community open source

Functionally the closest competitor to Handy and genuinely cross-platform, but
the repo contains `WorkspaceBillingCard`, `UpgradePrompt`, `useBillingPortal`,
`InviteTeammateDialog`, plan tiers free/pro/team/business/enterprise, and hosted
`auth.openwhispr.com` / `api.openwhispr.com` with Google and Microsoft OAuth.
**[verified]**

- Paid tier: "Upgrade to Pro — unlimited transcriptions from as little as
  $8/month", gated behind a rolling 2,000 words/week cap. The cap applies **only
  to their hosted cloud transcription**; local models and BYOK are unlimited,
  and the sign-in step is skippable (`skipAuth` in `OnboardingFlow.tsx`).
  **[verified]** Honest, but the roadmap is steered by a commercial tier.
- Credential handling is **better than Handy's** — OS keychain first, Electron
  `safeStorage` fallback, in `src/helpers/secretCrypto.js`. Has a `SECURITY.md`
  with a 48h acknowledgement / 7d critical-fix SLA. **[verified]**
- Electron hardening is **worse**: two windows (Control Panel, floating overlay)
  run with `webSecurity: false`, disabling same-origin policy, to allow
  `file://`-origin fetches to auth and model APIs without CORS. The in-code
  rationale is honest but a main-process proxy would avoid it.
  `contextIsolation: true` and `nodeIntegration: false` throughout limit the
  blast radius. **[verified]**
- 287 open issues against 1,943 commits in ~14 months. **[verified]**

### Others surveyed

- **FluidVoice** (9.8k stars, GPL-3.0) and **VoiceInk** (5.9k stars,
  non-standard license, $39.99) — both **macOS-only**, and both license-hostile
  to a closed-source codebase. **[verified]**
- **WhisperWriter** (1.1k stars) is recommended by most "best of 2026" listicles
  and has had **no commit since 2024-08**. Unmaintained. **[verified]**
- Every other Windows-capable option is under 500 stars: `opentypeless` (447),
  `infiniV/VoiceFlow` (406), `VoiceSnap` (82), `drajb/whisper-local` (22, already
  stale). **[verified]**

**Search-result warning:** queries for "open source Wispr Flow alternative"
surface articles from `whisperstream.io`, `speakoflow.com`, `voicekeyboardpro.com`,
`getvoibe.com`, and `heymumble.com` — all competing paid products publishing
their own rankings. Treat as marketing. All findings here come from the repos.

## Why not a fork

Forking imports an entire application to obtain one subsystem, and commits us to
its upgrade path indefinitely. Concretely:

- **Handy** is Rust + Tauri. Consuming it means either rewriting Sniptype in
  Rust or shipping and maintaining a Rust sidecar process.
- **OpenWhispr** is Electron + JavaScript, and open-core.
- **Buzz** is the only stack match (Python, MIT) and is the wrong product
  category entirely.

The inference subsystem we need is available directly as libraries. The product
still needs its own recording lifecycle, microphone permission flow, target-app
tracking, transcript routing, model delivery, and failure handling; none of
those should be imported by forking a second desktop application.

## What Sniptype already provides

Several useful integration seams are already built and shipping. **[verified]**
They reduce the work, but they are not a push-to-talk implementation.

| Dictation requirement | Existing implementation |
|---|---|
| Global keyboard event source | `pynput` listener, `sniptype.pyw:3593`; currently `on_press` only, with no chord/release lifecycle |
| Inject text into focused app | `clipboard_support.py` paste path, incl. rich text |
| Capture / restore frontmost app | macOS dialog path only: `platform_support.capture_frontmost_application()` |
| macOS keyboard/paste permissions | `macos_permissions.py` covers Input Monitoring and Accessibility, not microphone access |
| Tray, autostart, installer, cross-platform | already shipping |

Missing: configurable push-to-talk input, press/release state, audio capture,
VAD, inference, microphone permissions, target tracking, transcript routing,
model delivery, cancellation, and packaged native-library verification. This is
still a bounded feature inside Sniptype, not a reason to fork another app.

## Proposed shape

Not yet approved. Adding any of these changes the dependency set and needs
sign-off per the global dependency policy.

- **`transcribe.cpp`** (MIT) — leading unified ASR runtime candidate. Its C API
  and Python binding cover Parakeet v3, Qwen3-ASR-1.7B, and Nemotron 3.5 from
  Q8 GGUF files, with CPU, Metal, Vulkan, and CUDA backends. Verify binding
  stability, thread cancellation, Unicode paths, native-library discovery, and
  PyInstaller behavior before adoption.
- **`sherpa-onnx`** (Apache-2.0) — fallback runtime for Parakeet and Nemotron,
  and a possible Silero VAD host. Its wheel ships compatible ONNX Runtime
  binaries; the unrelated `onnxruntime` currently over-collected into `dist`
  is not a reusable dependency and must not sit beside sherpa's copy in the
  frozen build. It does not provide the same ready-made Qwen3-ASR-1.7B path.
- **`silero_vad.onnx` model asset** — optional trimming/endpoint component.
  Push-to-talk release remains the authoritative endpoint, so VAD must prove
  value rather than become a prerequisite. If used through sherpa, do not also
  bundle a second standalone ONNX Runtime. Never install the `silero-vad`
  Python package, whose `torch`/`torchaudio` dependencies defeat the lean design.
- **`sounddevice`** for capture, using a non-blocking callback and a bounded
  queue. Its PortAudio binary must be verified in both packaged targets.

OpenWhispr uses sherpa-onnx for Parakeet. Handy has moved across native runtimes
and remains a useful product and download-security reference, not proof that
any particular Python/native packaging path works for Sniptype.

### Recording lifecycle

Voice is a single-owner state machine:

```text
unavailable --enable/install--> loading --model ready--> idle
loading --cancel/error--> unavailable
idle --hotkey press--> recording
recording --live audio chunks--> append-only journal + recording (display-only partials)
recording --hotkey release/VAD--> transcribing/finalizing
transcribing/finalizing --result--> routing --insert/expand/update--> idle
recording/transcribing/finalizing/routing --cancel/error/shutdown--> history + idle
```

Download and load the selected profile's model on a worker when the user
enables voice.
Show non-blocking progress and expose a clear ready state; a hotkey received
while the model is unavailable or loading must not begin recording later and
drop the start of the utterance. Do not open the microphone until the model is
ready. The first version is push-to-talk: press starts, release stops. Toggle
mode can follow only if real use shows a need.

Profile behavior is explicit:

- **Balanced** and **Maximum accuracy** buffer the utterance and decode after
  release. They never simulate live output by repeatedly decoding a growing
  buffer.
- **Live streaming** may show non-activating partial text while the key is held,
  but partials are display-only. Only the finalized transcript enters the
  dispatcher and target application.
- Changing profiles waits for or cancels the active session, unloads the old
  model, then downloads/loads the new one. A failed switch leaves the previous
  valid profile available; it never falls forward into a surprise model.
- Store profile and language separately. `Auto`, `pt-BR`, and `en-US` are the
  first supported language choices. Nemotron defaults to the explicit locale
  because its published English auto-detect WER is worse; Parakeet and Qwen may
  use auto detection after corpus validation.

Rules:

1. Extend the listener with `on_release` and a configurable chord. Listener
   callbacks only update guarded Python state and enqueue work; they never open
   devices, load models, run inference, touch Tk, or write files.
2. One session lock owns recording through insertion. Auto-repeat and a second
   hotkey press are ignored while a session is active.
3. The audio callback only copies frames into bounded inference and journal
   queues. A journal worker appends float32 audio to disk while a session is
   active; the capture/inference workers own the stream, VAD, inference, and
   cleanup. Queue overflow or journal failure fails loudly instead of growing
   memory or pretending the recording is recoverable.
4. Escape cancels. App shutdown closes the stream, signals workers, joins them
   with a bound, and performs no paste after shutdown begins.
5. Device disappearance, empty speech, inference failure, and insertion failure
   leave no stuck state. Audio and lifecycle metadata live in
   `voice-history/`; transcript text is stored there after successful inference
   but is never written to application logs.
6. Retry is explicit and target-safe: it re-runs the recorded audio through the
   selected provider and copies the result. It never pastes into the target
   captured by an earlier process or session.

### Provider boundary

`voice_provider.py` owns provider readiness, model/runtime preparation,
transcription, streaming, cancellation, and profile deletion. The controller
owns the hotkey session and dispatch only. `LocalVoiceProvider` is the sole
provider: it wraps transcribe.cpp and the SHA256-pinned model catalog. This seam
does not add a network provider or alter the local model profiles.

`voice_history.py` owns append-only audio, atomic metadata transitions, startup
classification of interrupted sessions, and retry eligibility. History is not
automatically pruned because retention/deletion needs an explicit user-facing
policy rather than silent loss of recorded audio.

### Target and permission handling

Capture the intended text target when the hotkey is pressed, before any app UI
can take focus. Extend `platform_support.py` with a cross-platform target handle
and bounded restore/readiness seam: macOS can build on the existing AppKit path;
Windows needs its own foreground-window implementation. If the target closed or
cannot be restored, leave the transcript on the clipboard and notify instead of
pasting into an arbitrary application.

On macOS, add `NSMicrophoneUsageDescription` to the staged `Info.plist` before
the existing re-sign step, add microphone status to onboarding, and test denied,
granted, and restart-required behavior. Input Monitoring and Accessibility
remain separate grants. On Windows, surface privacy denial, no default device,
device-in-use, and mid-recording disconnect as actionable errors.

Secure Keyboard Entry must be checked before external voice insertion just as
it is before trigger erasure today. A blocked paste preserves the transcript on
the clipboard. Any recording indicator must be non-activating; it cannot steal
the text target.

### Transcript routing

Do not feed every transcript back through the keyboard listener. Add a dedicated
voice-result dispatcher with three explicit modes:

1. **Dictation** — default. Treat the transcript as literal text and pass it to
   `TextInserter`; trigger-looking words remain literal.
2. **Voice command** — a separate configured hotkey. Normalize the complete
   transcript, require an exact match to an effective direct or mapping trigger,
   and invoke expansion without erasing characters. No fuzzy trigger execution
   in the first version.
3. **Form input** — when the captured target is a Sniptype form field, marshal
   the value through `GuiThread` and update that widget directly. Never synthesize
   a global paste into the app's own Tk window.

The dispatcher may share trigger-index lookup and `_run_expansion` internals,
but it must not call `_dispatch_expansion`, whose contract assumes a physical
trigger was typed and must be erased. Tests must cover trigger collisions,
disabled/renamed dynamic triggers, mapping items, rich text, cancellation, and
focus changes between recording and completion.

### Model delivery

Patterns to copy from Handy (MIT permits reuse with attribution):

1. **SHA256-pinned model catalog compiled into the binary** as trust anchor.
   Pin the archive URL, compressed size, archive digest, expected extracted
   files, product profile, upstream model id and commit, runtime format,
   quantization, capabilities, minimum/recommended memory, license,
   attribution, and source URL. The catalog contains four Q8 profiles; F32
   Parakeet is a benchmark fixture, not a user option.
2. **Download models on first use into a dedicated non-roaming cache**, never
   the installer or `%APPDATA%`. Resolve it through a support module with an
   explicit override for tests and power users; do not silently place 640 MB to
   2.08 GB of regenerable assets in the snippet-data or sync-export directory.
3. **Any cloud post-processing off by default.** Capture stays local; cleanup is
   opt-in.

The settings UI shows the download size, installed size, profile purpose,
license, and a plain-language resource warning before download. Qwen's 2.08 GB
model is never preloaded or selected automatically. Multiple profiles may be
cached, but the model manager keeps only one resident. Deleting the active model
first disables voice cleanly, then removes only the exact catalog-owned cache
directory after explicit confirmation.

The downloader must check free space for both archive and extraction, follow a
bounded redirect policy, support progress/cancel/resume/retry, stream into a
temporary file, verify size and SHA256 before extraction, reject absolute or
traversing archive paths, validate the complete extracted manifest, and promote
the model directory atomically. Keep the last valid version until the new one
passes a load smoke test. Provide explicit delete/re-download controls and make
offline failure recoverable without restarting the app.

Reference **Buzz's build tooling** (`Buzz.spec`, `hatch_build.py`,
`installer.iss`) for whisper.cpp/ffmpeg packaging on Windows, which is the
genuinely painful part. Take nothing else from it.

### Licensing constraint

Sniptype is MIT-licensed. Handy, Buzz, and OpenWhispr are MIT; code copied
from them must retain the applicable license and attribution and record the
source commit. **FluidVoice is GPL-3.0** and VoiceInk's license is non-standard
— do not copy from either. **[verified]**

The application licenses do not cover the proposed runtime and model artifacts:

| Artifact | License / obligation |
|---|---|
| `transcribe.cpp` | MIT; ship the license and retain attribution for the exact bundled commit |
| `sherpa-onnx` | Apache-2.0; ship the license and any applicable NOTICE material |
| `sounddevice` / PortAudio | verify and ship their license notices for the exact approved versions |
| `soxr` / libsoxr / PFFFT | Python-SoXR/libsoxr is LGPLv2.1+; PFFFT is BSD-like; ship the exact notice and attribution |
| `silero_vad.onnx` | verify the model asset's MIT provenance and retain attribution |
| Parakeet TDT 0.6B v3 | CC-BY-4.0; provide credit, license link, source, and modification/quantization disclosure |
| Qwen3-ASR-0.6B and 1.7B | Apache-2.0; ship the license, source link, model identity, and Q8 conversion attribution |
| Nemotron 3.5 ASR Streaming 0.6B | Open Model Derivative Works License 1.1; legal/package review must confirm redistribution, attribution, and quantized-derivative obligations before enabling download |

Before distribution, add a third-party notices surface covering every bundled
native library and every downloaded model. The model catalog owns the same
license metadata so the download UI can show it before installation.

## The product argument

A plain dictation feature is not worth building: Handy already does it well and
may compose with us today. Whether Handy's injected text reaches pynput as the
character events needed for trigger expansion is platform- and injection-path
dependent. **[inference — untested, and the first experiment to run.]**

The version that justifies the work is voice as an **input method for the
expander**: dictate into a snippet form field, speak a trigger to fire a
mapping, dictate into a variable and let `variable_support.py` and
`rich_text_support.py` handle it. That composition is unavailable to Handy or
Wispr Flow, and it is the only argument for the feature living here rather than
in a separate app running alongside.

## Side finding — PyInstaller is over-collecting

Unrelated to voice, found while measuring bundle weight. **[verified]**

`dist/Sniptype/_internal` contains:

```
torch          365 MB
cv2             99 MB
scipy           51 MB
transformers    42 MB
onnxruntime     34 MB
torchvision     12 MB
```

No source file imports `torch`, `cv2`, `transformers`, `onnxruntime`, `scipy`,
`numpy`, or `pandas` directly — `pandas`/`numpy` arrive legitimately via
`yfinance`, but `torch`, `cv2`, `transformers`, and `torchvision` do not appear
to be reachable at all. `Sniptype.spec` has `excludes=[]`. PyInstaller is
collecting the modules through some reachable import or analysis hook in the
current host environment; which edge causes each collection is not yet
established. **[inference]** on the cause; the import absence and sizes are
measured.

Effect: roughly 520 MB of apparent dead weight in the 215 MB installer / 821 MB
unpacked bundle, present since at least 3.0.0. The estimate that the installer
could approach 40 MB is **[inference]** until a clean build proves it.

Fix this independently of voice, but do not hand-edit `Sniptype.spec`: both
release scripts generate their specs. Rebuild from an isolated environment
containing only the declared runtime dependencies plus PyInstaller, inspect the
analysis graph and warnings, and identify the importing module or hook for each
unexpected package. If exclusions remain necessary, add verified
`--exclude-module` flags or a project hook to both durable build scripts. Run
source and packaged smoke tests for stock snippets, clipboard insertion, tray,
and startup before accepting the size reduction.

## Open questions

1. Does Handy alongside Sniptype already solve the need? Untested; cheapest
   possible outcome. **Test before writing code.**
2. Does `transcribe.cpp` package and behave reliably on Windows x64 and macOS
   ARM64 through its Python binding, including cancellation and Unicode paths?
3. On the same real corpus, does Qwen3-ASR-1.7B Q8 materially beat Parakeet Q8
   for pt-BR, en-US, named entities, and code-switching enough to justify its
   roughly fourfold CPU cost and 2.08 GB download?
4. Does Nemotron 560 ms meet the live partial-latency gate without an
   unacceptable pt-BR/en-US accuracy loss, and should 1.12 s be selectable?
5. Does the ~740 MB default download survive contact with real users, and is a
   2.08 GB optional accuracy model acceptable with an explicit warning?
6. Which push-to-talk chord avoids collisions with existing applications and
   remains observable as both press and release on supported platforms?
7. Should the first release support only the default microphone, or is device
   selection required for the target users?

## Next steps

See [Implementation status](#implementation-status-2026-09-08). In order:

1. Run the Handy composition test on Windows and macOS.
2. Rebuild from an isolated environment and confirm the PyInstaller excludes
   actually drop torch/transformers/onnxruntime.
3. Package `transcribe.cpp` on Windows x64 and macOS ARM64, then run the
   Balanced adoption gates on the named hardware.
4. Keep Accuracy and Live streaming hidden until they pass their own gates
   and the Nemotron OpenMDW-1.1 review.
5. Add downloader resume/retry so a dropped 740 MB transfer does not start over.
