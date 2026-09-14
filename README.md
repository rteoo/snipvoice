# Snipvoice

Local push-to-talk dictation for Windows and macOS, extracted from
[Sniptype](https://github.com/rteoo/sniptype).

Hold a shortcut, speak, and release to insert the transcript at the captured
cursor target. Audio stays local; models download only after an explicit choice.
The app includes model/profile selection, configurable hotkeys, deterministic
term corrections, a recording indicator, and crash-recoverable audio history.
Failed recordings can be retried into History/clipboard without delayed blind paste.

The **Gravações e reuniões…** tray workspace records microphone input, speaker
output, or both into separate recoverable tracks. Each source can follow the OS
default or stay pinned to a selected device. Recording also works with dictation
disabled and without a model. Pause, notes/bookmarks, local search, track playback,
WAV import, transcription revisions, and Markdown/text/JSON/WAV export are included.
Meeting transcripts use installed local models and never start model downloads.

This is a **0.1.0 beta source project**, not a certified desktop release.
Offline tests do not prove live ASR, microphone/device behavior, or macOS permissions.

## Run

```powershell
cd source
python -m pip install -r requirements.txt -r requirements-voice.txt
python snipvoice.pyw --show-settings
```

Use `pythonw snipvoice.pyw` on Windows after setup. Voice starts disabled.
Open **Configurar voz…**, choose a model and language, and enable recording.
Default dictation shortcut: `ctrl+alt+space`. Release completes the utterance;
Escape cancels. A model is never included in the repository or installer.

For meetings, first build the native helper with
`source\native\build_windows_capture.bat` on Windows or
`sh source/native/build_macos_capture.sh` on macOS 14.4+. The existing MSVC/Windows
SDK or Apple SDK must be provisioned separately. Open **Gravações e reuniões…**,
choose your input/output devices, and click **Iniciar gravação**. The optional
recording toggle shortcut starts unassigned; configure a chord independent of
dictation and commands. Selecting an output device captures its current mix;
it does not move another application's playback to that device.

After recording, select the meeting and transcribe using an installed model.
Import matching catalog model files from disk through the workspace, or download
explicitly through voice settings while no meeting owns the runtime. Resumed jobs
skip durable completed chunks. Source labels identify audio tracks, not people;
timestamps describe audio chunks, not individual words. Headphones reduce speaker
audio leaking into the microphone; acoustic echo cancellation is not implemented.

Optional structured summaries use a manually installed local Ollama model at
`127.0.0.1:11434`. No runtime installation or model pull is performed. Configure
Ollama with cloud features disabled (`OLLAMA_NO_CLOUD=1`) and verify with outbound
networking blocked. Review the cited decisions/action items before using them.

Optional spoken commands use `ctrl+alt+shift+space`. Create a private
`commands.json` in the Snipvoice data folder, for example:

```json
{"hello": "Hello, how can I help?"}
```

Use **Recarregar comandos**, then hold the command shortcut and say the exact
trigger. Replacement values are literal text; Sniptype libraries, dynamic
providers, and form registration are not imported automatically.

## Data and privacy

Settings, optional commands, logs, and recordings belong to `~/.snipvoice`
(`SNIPVOICE_HOME` overrides it). Models belong to the non-roaming Snipvoice
cache (`SNIPVOICE_VOICE_CACHE` overrides it). Sniptype data is never migrated.
Audio and transcripts remain in history until the user removes them; no automatic
retention policy is implemented. Do not commit recordings, personal commands,
settings, models, or logs. No telemetry or transcript logging is added.
Meeting recordings belong to the independent `meetings` subdirectory. Playback
is blocked during recording to avoid capturing Snipvoice's own playback.

## Verify and build

```powershell
cd source
python -m unittest discover -s tests -v
cd ..
python -m ruff check source
build_release.bat
build_installer.bat
```

macOS: `./build_release_macos.sh`. Build tools must already be installed.
Both packagers require the pinned native runtime and run `--voice-runtime-probe`
and the non-recording `--meeting-capture-probe` before promotion. They compile and
bundle the owned capture helper. The installer has its own AppId and does not replace Sniptype.
See [extraction boundaries](source/docs/extraction.md) and
[development](source/docs/development.md). Inherited research/design records are
historical evidence, not current Snipvoice release certification.

MIT license; the predecessor's copyright and
[third-party notices](THIRD_PARTY_NOTICES.md) are retained.
