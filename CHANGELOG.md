# Changelog

## Unreleased

- Add Whisper Small and Whisper Large v3 Turbo as optional, hash-pinned transcription models for dictation and meetings.

## 3.3.1 - 2026-09-23

- Remove the transparent margin from the Windows icon so it fills tray, Start and taskbar slots like neighbouring icons.
- Keep the Windows GUI pump running when Tcl timer wake-ups stop arriving, which intermittently left queued GUI work unprocessed.

## 3.3.0 - 2026-09-23

- Reorganize the library into a single toolbar with on-demand filters and cross-meeting questions, empty-state guidance, and a recording view split into Notas, Transcrição, Resumo, Perguntar and Arquivos.
- Split Configurações into Geral, Privacidade, Transcrição and Resumos sections instead of one long page.
- Fix dark-mode widgets (checkboxes, read-only fields, level meters, scrollbars and borders), light-mode entry borders, and controls clipped at the minimum window width.
- Translate search-index states to Portuguese and label the library filter and organization fields.
- Support building the clean Windows audio runtime from PowerShell with an MSYS2 toolchain.

## 3.2.0 - 2026-09-17

- Add a private local meeting-memory workspace with recoverable recordings, searchable metadata, transcript navigation, annotations, and rebuildable local indexing.
- Add revision-scoped meeting reports and questions with citations, cross-meeting search, consent and retention controls, and privacy-aware export workflows.
- Modernize the dark appearance and initialize persisted appearance settings reliably.
- Consolidate cross-platform CI validation with native runtime and capture-helper checks across the supported platforms.

## 3.1.0 - 2026-09-15

- Refine the recording workspace with a responsive layout, full-width automation controls, and recording defaults in Configurações.
- Replace raw recording errors with friendly status messages while keeping technical details available on demand.
- Add a clear action for restoring the default recording destination.
- Add safe library deletion with confirmation while preserving audio exported outside the managed recording folder.
- Make the Settings model list respond to the mouse wheel.
- Improve disabled-control contrast and Portuguese interface consistency.

## 3.0.0 - 2026-09-15

- Add independent microphone/system toggles and bounded live two-track waveforms.
- Create an atomic, timestamp-aligned final WAV after recording, with optional conservative microphone cleanup.
- Add a configurable final-audio destination plus opt-in automatic local transcription and summary.
- Generate timestamped recording titles and refine them with three to five transcript-derived words.
- Import WAV, MP3, AAC/M4A, FLAC, OGG, and Opus recordings through a bounded local decoder.
- Make recording the default window, simplify model selection, and present friendly audio-device names without implementation identifiers.
- Add Qwen3.5 0.8B Q4_K_M as a 503 MiB compute-budget summary option.
- Replace IBM Granite 4.2 3B with the first-party LiquidAI LFM2.5-2.6B Q4_K_M model.
- Require an explicit first-download notice for LiquidAI's non-MIT, non-Apache LFM Open License v1.0.

## 2.0.0 - 2026-09-15

- Replace the external Ollama summary adapter with a packaged llama.cpp runtime.
- Add verified in-app downloads for Qwen3.5 2B and 4B, IBM Granite 4.2 3B, and Gemma 4 E2B/E4B GGUF models.
- Add a dedicated summary-model settings tab with selection, removal, cancellation, license details, and resource-size labels.
- Unify voice setup, meeting recording, the library, and summary models in one Fluent-style settings window.
- Keep the meeting tray shortcut focused on the recording tab while reusing the shared application window.
- Retry brief Windows file-sharing conflicts while atomically saving settings and meeting metadata.

## 1.0.0 - 2026-09-14

- Extract Snipvoice as an independent local voice application for Windows and macOS.
- Add push-to-talk dictation, local model management, term corrections, optional commands, and recoverable voice history.
- Add simultaneous microphone and selected speaker-output meeting capture with OS-default or manually pinned devices.
- Add segmented recovery, meeting search, notes, bookmarks, playback, import/export, and resumable transcription revisions.
- Add optional cited summaries through a loopback-only local Ollama adapter.
- Add Windows and macOS native capture helpers, package probes, desktop bundle automation, and a dedicated application icon.
