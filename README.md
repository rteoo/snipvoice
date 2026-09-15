# Snipvoice

<p align="center">
  <img src="source/snipvoice-icon.png" width="128" alt="Snipvoice app icon">
</p>

<p align="center">
  A local-first voice dictation and meeting recorder for Windows and macOS,
  with separate microphone and system-audio tracks.
</p>

<p align="center">
  <a href="https://github.com/rteoo/snipvoice/actions/workflows/ci.yml"><img src="https://github.com/rteoo/snipvoice/actions/workflows/ci.yml/badge.svg" alt="CI status"></a>
  <a href="https://github.com/rteoo/snipvoice/tags"><img src="https://img.shields.io/github/v/tag/rteoo/snipvoice?label=stable" alt="Stable tag"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT license"></a>
</p>

Hold a shortcut, speak, and release. Snipvoice transcribes with an installed
local model and inserts the result at the cursor target captured when recording
started. It can also record meetings from the microphone, speaker output, or
both, while keeping each source in its own recoverable track.

Snipvoice is the standalone voice companion extracted from
[Sniptype](https://github.com/rteoo/sniptype). It has separate settings, models,
recordings, shortcuts, process identity, and installers.

## Highlights

- Local push-to-talk dictation with configurable shortcuts and languages.
- Separate microphone and selected speaker-output recording on Windows and macOS.
- OS-default devices or manually pinned input/output endpoints.
- Crash-recoverable segmented audio, explicit gaps, pause/resume, and partial-session preservation.
- Meeting library with local search, notes, bookmarks, playback, and transcription revisions.
- WAV import plus Markdown, text, JSON, and per-track WAV export.
- Installed-only meeting transcription; processing never downloads a model.
- Cited summaries through a built-in llama.cpp runtime and downloadable local models.
- Deterministic term corrections and optional literal spoken commands.
- No telemetry, transcript logging, implicit cloud storage, or Sniptype data migration.

## Quick start

Download the package for your platform from the
[stable v1.0.0 release](https://github.com/rteoo/snipvoice/releases/tag/v1.0.0).
To run from source with Python installed:

```powershell
git clone https://github.com/rteoo/snipvoice.git
cd snipvoice\source
python -m pip install -r requirements.txt -r requirements-voice.txt
python snipvoice.pyw
```

Use `pythonw snipvoice.pyw` on Windows after setup when you do not need console
output. Models are not bundled. Voice starts disabled until a model is selected
and the feature is enabled from **Configurar voz…**.

### Releases and installers

The current stable release is
[`v1.0.0`](https://github.com/rteoo/snipvoice/releases/tag/v1.0.0):

| Platform | Package |
| --- | --- |
| Windows x64 | Per-user installer and portable ZIP |
| macOS 14.4+ ARM64 | Ad-hoc-signed `.app` bundle in a ZIP |

The packages are not notarized or publisher-signed. Windows SmartScreen and
macOS Gatekeeper may therefore require the standard manual confirmation on first
launch. The Windows installer uses no administrator rights and installs under
`%LOCALAPPDATA%\Programs\Snipvoice`. Application data stays outside the package.

## First use

1. Start Snipvoice and find its icon in the Windows tray or macOS menu bar.
2. Open **Configurar voz…**. The Snipvoice window keeps voice setup, recording, the meeting library, and summary models in separate tabs.
3. In **Voz**, choose a profile and language, then download or import its local model.
4. Enable voice input, hold `ctrl+alt+space`, speak, and release to transcribe.
5. Use **Gravar e configurar** to choose microphone/system sources and record a meeting. The **Gravações e reuniões…** tray shortcut selects this tab in the same window.
6. Open **Resumo local**, choose Qwen3, Granite, or Gemma, and download the model before generating a summary.

Escape cancels active dictation. A failed or interrupted utterance remains in
voice history and can be retried manually without a delayed blind paste.

## Dictation and commands

The default dictation shortcut is `ctrl+alt+space`. Snipvoice captures the target
before opening the microphone, keeps capture and inference off the keyboard
listener, and restores the target only after transcription completes.

Optional spoken commands use `ctrl+alt+shift+space`. Create a private
`commands.json` under the Snipvoice data directory, for example:

```json
{"hello": "Hello, how can I help?"}
```

Use **Recarregar comandos**, then hold the command shortcut and say the exact
trigger. Values are inserted literally. Sniptype libraries and dynamic actions
are deliberately not imported.

## Meetings

The **Gravar e configurar**, **Biblioteca e transcrição**, and **Resumo local** tabs keep meeting
capture independent from dictation inside the main Snipvoice window. Recording
works when dictation is disabled and before any model is installed.

| Capability | Behavior |
| --- | --- |
| Sources | Microphone, speaker output, or both in separate native PCM tracks |
| Devices | Follow the OS multimedia/communications default or pin a stable endpoint |
| Recovery | Append-only 30-second segments, CRC journal, atomic metadata, interrupted-session repair |
| Workspace | Pause/resume, source meters, title, notes, bookmarks, local search, status filters |
| Playback | Seek by timestamp and play one track through the current OS output |
| Processing | Durable local-model revisions with resumable completed chunks |
| Files | Integer-PCM WAV import; Markdown, text, JSON, and PCM16 WAV export |

Selecting an output captures the mix already routed to that device; Snipvoice
does not move another application's audio. Source labels identify tracks, not
individual speakers. Acoustic echo cancellation and diarization are not included.

## Local transcription and summaries

Model downloads happen only after an explicit action in voice settings. Meeting
processing accepts installed catalog models and never downloads one implicitly.
Recordings remain usable before transcription and preserve earlier revisions.

Structured summaries run inside Snipvoice through llama.cpp. The default is
Qwen3 1.7B Q4_K_M; IBM Granite 3.3 2B is tuned for document and meeting
summaries, and Gemma 3 1B is the smallest option. Downloads use a fixed catalog,
stream to a resumable partial file, and become usable only after their exact size
and SHA-256 match. Gemma requires explicit acceptance of its separate terms.
After a model is installed, summary inference makes no network request. Review
cited decisions and action items before using them.

## Data safety and privacy

Settings, optional commands, logs, voice history, and meetings live under
`~/.snipvoice` by default; `SNIPVOICE_HOME` overrides the location. Models use
separate non-roaming caches selected by `SNIPVOICE_VOICE_CACHE` and
`SNIPVOICE_SUMMARY_CACHE`.

Audio and transcripts remain until the user removes them. There is no automatic
retention policy, telemetry, transcript logging, or implicit upload. Do not
commit or share live recordings, personal commands, settings, model files, or
logs. Playback is blocked during recording so Snipvoice does not capture itself.

## Platform status and limitations

Snipvoice packages Windows x64 and Apple Silicon macOS 14.4+.

- **Windows:** speaker output uses WASAPI loopback. The installer and executable are currently unsigned.
- **macOS:** system audio uses Core Audio taps and requires macOS 14.4+. Microphone, System Audio Recording, Input Monitoring, and Accessibility permissions may be requested. The public bundle is ad-hoc signed and not notarized.
- **Linux:** CI covers portable Python behavior, but no Linux capture helper or desktop package is released.
- **Physical acceptance:** CI compiles both helpers and runs non-recording probes. Real-device capture, endpoint switching, permission recovery, two-hour drift, cross-application insertion, and packaged desktop behavior still need physical-host verification.

## Develop and build

Editable Python source and tests live under [`source/`](source). Run:

```powershell
cd source
python -m unittest discover -s tests -v
cd ..
python -m ruff check source
build_release.bat
build_installer.bat
```

On macOS, run `./build_release_macos.sh`. Build tools must already be installed;
the package workflow uses the pinned requirements in `source/requirements-build.txt`.
Both packagers compile the native helper, bundle the local ASR and llama.cpp runtimes, stage the
result, and run `--voice-runtime-probe`, `--summary-runtime-probe`, and `--meeting-capture-probe` before
promotion. See the [development guide](source/docs/development.md) and
[release validation](source/docs/offline-meeting-validation.md).

Release history is documented in [CHANGELOG.md](CHANGELOG.md).

## License

Snipvoice is released under the [MIT License](LICENSE). Packaged builds retain
the predecessor copyright notice and include the applicable dependency index in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
