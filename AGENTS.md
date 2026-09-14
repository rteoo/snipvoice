# Snipvoice

Snipvoice is a standalone local push-to-talk tray app extracted from Sniptype.
Read the active runtime's global operator contract and SOUL.md before work.

## Architecture and safety

- Entry point: `source/snipvoice.pyw`; there is no text-expansion listener.
- `voice_support.py` owns sessions; `voice_provider.py` and `voice_runtime.py`
  own native inference; `voice_audio.py`/`voice_resampler.py` own capture.
- Keep hotkey callbacks bounded and all capture/inference/download/disk work
  off keyboard threads. Keep Tk on the process's only `GuiThread` root.
- macOS creates Tk on the main thread before constructing the tray; submit
  Cocoa callbacks to the Tk pump and hide the Dock icon after root creation.
- Preserve target/cancellation guards, append-only recoverable recordings,
  atomic JSON, hash-verified model downloads, and safe manual history retry.
- User data is `~/.snipvoice` / `SNIPVOICE_HOME`; cache is independent and
  non-roaming / `SNIPVOICE_VOICE_CACHE`. Never migrate Sniptype data implicitly.
- `commands.json` contains private literal commands. Never copy live libraries,
  recordings, models, settings, logs, or predecessor Git backups into the repo.
- Use `ui_theme` for GUI colors/fonts; native overlays never steal focus.
- `source/docs/extraction.md` defines current boundaries. Inherited research
  is historical and does not certify packaged operation or live transcription.

## Verification

From `source`: `python -m unittest discover -s tests -v`.
From root: `python -m ruff check source` requires the pinned development tool.
The extraction host verified these rules with an existing Ruff 0.16.4 binary;
the development manifest and CI retain the predecessor's Ruff 0.16.3 pin.
Temporary test artifacts belong in `source/tests/tmp` (gitignored).
Existing source dependencies are in `requirements.txt`; pinned capture/runtime
dependencies are in `requirements-voice.txt`. Do not install or alter them implicitly.
Windows/macOS build scripts are inherited and adapted, but physical package
smokes and signing need platform-specific verification before release.

## Git

For the authorized initial project scaffold only, initialize and commit on main.
Subsequent work uses task branches. Preserve signing/hooks. Stage explicit owned
paths; push/PR/release actions need direct user authorization. Do not push changes
to the predecessor as part of creating this repository. `CLAUDE.md` is `@AGENTS.md`.
