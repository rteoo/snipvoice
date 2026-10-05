# Changelog

## Unreleased

- Batch contiguous native audio packets to reduce disk synchronization pressure while preserving gaps and pause/stop tails.
- Show recording time, pause state, and partial/interrupted status in the floating indicator while the manager is minimized or hidden.
- Preserve queued audio when stopping races the native helper's exit; continue reporting overflow and unexpected exits.
- Reject inconsistent runtime, installer, or bundle version/channel metadata before Windows release preparation.
- Name transcription and summary models by what they offer, such as "Equilibrado · Parakeet" and "Máxima qualidade · Gemma 4 E4B", instead of bare model codes.
- List models from lowest to highest quality in Ditado, Configurações > Gravação, and Configurações > Modelos.

## 1.2.0 - 2026-09-28

- Split Arquivos into the final audio and the original (raw) tracks, with the format and size of each. Download the final audio as saved, or each original track on its own as MP3.
- Remove the original tracks together, and only once a final audio file is saved; automatic retention follows the same rule. The confirmation warns that the recording can no longer be transcribed again, and afterwards playback and downloads use only the final audio.
- Stop pre-selecting every original track for removal, and never fall back to removing all tracks when none is chosen.
- Add Configurações > Privacidade > "Remover as faixas originais automaticamente após N dias". The app applies it at startup and every six hours, only to recordings with a saved final audio and a finished transcript. The previous raw-audio policy was stored but never applied.
- Rewrite Configurações > Privacidade in plain language instead of workspace codes: the recording notice names its language, "Nunca salvar respostas do chat" replaces the Q&A mode, and trash lifetime reads in whole days.
- Apply "Mover gravações para a lixeira automaticamente após N dias" in the same pass. It moves recordings to the restorable trash and never purges; the meeting policy was also stored but never applied before.
- Build the final audio about 10x faster after a recording (about 20 s instead of 3-5 minutes for a 24-minute meeting); Ajustar volume and audio downloads get the same speed-up.
- Keep the microphone boost through loud moments: one knock or laugh no longer disables it for the whole recording. Loud overlaps are limited instead of clipped, and damaged samples become silence.
- Raise your voice to at least the call's speech level in the final audio when the call is louder.
- Rebuild the MP3 final audio in place when using Ajustar volume instead of adding "(2).mp3" copies.
- Transcribe recordings with the dictation model by default ("Igual ao ditado" in Configurações > Gravação); an explicitly chosen recording model is kept.
- Release the dictation model from memory after 10 minutes without use (about 0.8 GB for the default); the next dictation reloads it in about a second, and quick follow-ups stay instant.
- Cut idle CPU use from about 1.3% to 0.3% of a core while the window is closed.

## 1.1.0 - 2026-09-26

Redesign the manager so pages use the window's height instead of scrolling.

- Move identity, navigation, and the 100% local badge into a left sidebar, with the app icon beside the name; the header, tab strip, and per-page titles are gone. Ctrl+1 through Ctrl+4 still switch pages.
- Split Gravação into two columns: title and sources on the left; the waveform, level meters, a large clock, and the Iniciar/Pausar/Parar controls on the right.
- Move the library's search and actions into the recording list column so the selected recording gets the full height, and put the player above the transcript in place of the separate Ouvir tab.
- Show scrollbars only when a page overflows, and fit long device names in the source pickers on macOS.
- Raise the manager's minimum width to 1040 px on Windows and 1100 px on macOS to make room for the sidebar.

## 1.0.0 - 2026-09-26

First stable release. Earlier releases were renumbered, and the same commits
are tagged under the new names:

| Former | Now |
| --- | --- |
| 1.0.0, 2.0.0 | 0.1.0, 0.2.0 |
| 3.0.0, 3.1.0, 3.2.0 | 0.3.0, 0.4.0, 0.5.0 |
| 3.3.0, 3.3.1, 3.3.2 | 0.6.0, 0.6.1, 0.6.2 |
| 3.4.0-beta.1 to beta.3 | 1.0.0-beta.1 to beta.3 |

- Add an interface language option under Configurações > Geral > Idioma. Brazilian Portuguese remains the default, and English (US) covers the whole interface: tabs, dialogs, tray menu, status and error messages, exports, and the Windows installer's own options. Switching rebuilds the window without restarting.
- Promote the 1.0.0 beta recording workflow to stable: the Win Design System manager, saved-recording replay, the pre-recording source check, the isolated summary runtime, and the quiet-speech boost in final recordings.
- Ship the Microsoft Store MSIX again, versioned 1.0.0.0.

## 1.0.0-beta.3 - 2026-09-25

- Apply the Win Design System to the manager's light and dark appearance, improve control contrast, and add Ctrl+1 through Ctrl+4 destination shortcuts.
- Keep both recording tracks and the level meters reachable when the window is at its minimum size, with a quiet status footer.
- Simplify the library filters and give empty, filtered, and error states direct next actions while keeping saved-recording playback first.
- Put corrections, command reload, licenses, and model removal in the dictation page's secondary menu.
- Omit the duplicate-version Store MSIX from this beta; the Windows installer and portable ZIP remain available.

## 1.0.0-beta.2 - 2026-09-25

- Isolate the local summary runtime from transcription's native audio libraries to avoid the Windows entry-point failure seen in beta 1.
- Raise quiet microphone speech in the derived final recording without hard-gating low-level audio or changing the recoverable raw track.
- Add saved-recording replay with source selection and seeking, and simplify the library's opening view by moving advanced tools behind on-demand controls.
- Style the product name as SnipVoice in the app, installer, macOS permission prompts, and README; the executable, data folders, and existing installs are unchanged.

## 1.0.0-beta.1 - 2026-09-25

- Add a three-second microphone and system-source check with live meters and a waveform before recording, without creating a meeting or saving audio.
- Focus the recording screen on title, sources, waveform, and transport controls; move destination and automation defaults to Configurações > Gravação.
- Start or replace playback from a selected transcript row with a double-click or Enter.

## 0.6.2 - 2026-09-23

- Group transcription and summary model downloads under Configurações > Modelos, with Transcrição and Resumos tabs, and give neutral buttons a fill and border that stand out on cards.

- Add Configurações > Modelos > Pasta dos modelos to move downloaded transcription and summary models to another folder, including one shared with other apps; the GGUF files stay usable by compatible apps and unrelated files in that folder are never touched.

- Add Configurações > Geral > Pasta de dados to move all Snipvoice data (settings, dictation history, recordings and the meeting library) to another folder, such as another drive; the app restarts and verifies the copy before removing the old folder.

- Add Whisper Small, Whisper Large v3 Turbo and Whisper Large v3 as optional, hash-pinned transcription models for dictation and meetings.

## 0.6.1 - 2026-09-23

- Remove the transparent margin from the Windows icon so it fills tray, Start and taskbar slots like neighbouring icons.
- Keep the Windows GUI pump running when Tcl timer wake-ups stop arriving, which intermittently left queued GUI work unprocessed.

## 0.6.0 - 2026-09-23

- Reorganize the library into a single toolbar with on-demand filters and cross-meeting questions, empty-state guidance, and a recording view split into Notas, Transcrição, Resumo, Perguntar and Arquivos.
- Split Configurações into Geral, Privacidade, Transcrição and Resumos sections instead of one long page.
- Fix dark-mode widgets (checkboxes, read-only fields, level meters, scrollbars and borders), light-mode entry borders, and controls clipped at the minimum window width.
- Translate search-index states to Portuguese and label the library filter and organization fields.
- Support building the clean Windows audio runtime from PowerShell with an MSYS2 toolchain.

## 0.5.0 - 2026-09-17

- Add a private local meeting-memory workspace with recoverable recordings, searchable metadata, transcript navigation, annotations, and rebuildable local indexing.
- Add revision-scoped meeting reports and questions with citations, cross-meeting search, consent and retention controls, and privacy-aware export workflows.
- Modernize the dark appearance and initialize persisted appearance settings reliably.
- Consolidate cross-platform CI validation with native runtime and capture-helper checks across the supported platforms.

## 0.4.0 - 2026-09-15

- Refine the recording workspace with a responsive layout, full-width automation controls, and recording defaults in Configurações.
- Replace raw recording errors with friendly status messages while keeping technical details available on demand.
- Add a clear action for restoring the default recording destination.
- Add safe library deletion with confirmation while preserving audio exported outside the managed recording folder.
- Make the Settings model list respond to the mouse wheel.
- Improve disabled-control contrast and Portuguese interface consistency.

## 0.3.0 - 2026-09-15

- Add independent microphone/system toggles and bounded live two-track waveforms.
- Create an atomic, timestamp-aligned final WAV after recording, with optional conservative microphone cleanup.
- Add a configurable final-audio destination plus opt-in automatic local transcription and summary.
- Generate timestamped recording titles and refine them with three to five transcript-derived words.
- Import WAV, MP3, AAC/M4A, FLAC, OGG, and Opus recordings through a bounded local decoder.
- Make recording the default window, simplify model selection, and present friendly audio-device names without implementation identifiers.
- Add Qwen3.5 0.8B Q4_K_M as a 503 MiB compute-budget summary option.
- Replace IBM Granite 4.2 3B with the first-party LiquidAI LFM2.5-2.6B Q4_K_M model.
- Require an explicit first-download notice for LiquidAI's non-MIT, non-Apache LFM Open License v1.0.

## 0.2.0 - 2026-09-15

- Replace the external Ollama summary adapter with a packaged llama.cpp runtime.
- Add verified in-app downloads for Qwen3.5 2B and 4B, IBM Granite 4.2 3B, and Gemma 4 E2B/E4B GGUF models.
- Add a dedicated summary-model settings tab with selection, removal, cancellation, license details, and resource-size labels.
- Unify voice setup, meeting recording, the library, and summary models in one Fluent-style settings window.
- Keep the meeting tray shortcut focused on the recording tab while reusing the shared application window.
- Retry brief Windows file-sharing conflicts while atomically saving settings and meeting metadata.

## 0.1.0 - 2026-09-14

- Extract Snipvoice as an independent local voice application for Windows and macOS.
- Add push-to-talk dictation, local model management, term corrections, optional commands, and recoverable voice history.
- Add simultaneous microphone and selected speaker-output meeting capture with OS-default or manually pinned devices.
- Add segmented recovery, meeting search, notes, bookmarks, playback, import/export, and resumable transcription revisions.
- Add optional cited summaries through a loopback-only local Ollama adapter.
- Add Windows and macOS native capture helpers, package probes, desktop bundle automation, and a dedicated application icon.
