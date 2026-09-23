# Extraction from Sniptype

Snipvoice was extracted from https://github.com/rteoo/sniptype at `bfdbea22bf50bc667c2845f2cecdd92a9fd09101` on 2026-09-14.
The inherited MIT license is retained. This is a clean source extraction, without
Git history, private backups, personal snippet libraries, recordings, or model files.

The voice controller, capture/resampling, pinned model catalog, provider/runtime,
hotkeys, safe target dispatch, recovery history, corrections, and native overlay
are carried over with their regression tests. Clipboard, GUI threading, theme,
and platform helpers are copied so the project runs independently of Sniptype.
The shared trigger/variable helpers support exact spoken-command matching only;
Snipvoice has no text-expansion keyboard listener, market providers, or snippet manager.

User data: `~/.snipvoice`, a user-moved folder recorded in `location.json`, or
`SNIPVOICE_HOME`. Model cache: non-roaming Snipvoice
directory / `SNIPVOICE_VOICE_CACHE`. Legacy Sniptype cache overrides are ignored.
Existing `~/.sniptype` data, recordings, and models are preserved without migration.
Any reuse or copying of an existing model cache is an explicit later operation;
the extraction does not link model-deletion controls to another application's cache.

`commands.json` is an optional Snipvoice-owned dictionary of exact spoken triggers
and literal plain-text replacements. Sniptype dynamic snippets and in-process form
registration are not linked across the process boundary. Dictation can paste into
another application's focused input, including a Sniptype form, through the usual
captured-target safety checks. No IPC or automatic library import is introduced.

Older design/research files are inherited snapshots. Their past package, version,
issue, and release claims describe the predecessor and do not certify Snipvoice.
Live microphone-to-paste, physical device-loss behavior, macOS TCC, signing, and
packaged desktop operation need separate hardware validation before a release.
