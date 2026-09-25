# Snipvoice interface

Snipvoice adopts Win Design System 1.0 for its Windows manager and adapts the
same hierarchy for macOS. The shared tokens live in `source/ui_theme.py`;
screen builders use those tokens instead of literal colors or fonts.

## Product structure

- **Gravação:** check sources, record, and read the current state. Keep the
  primary recording action and recovery state visible together.
- **Biblioteca:** search and select a recording, then listen first. Notes,
  transcript, summary, questions, and files are peer detail sections. Filters,
  batch actions, and cross-recording questions appear when requested.
- **Ditado:** choose the active model and language, then use the hotkey. Model
  downloads and privacy controls belong in settings. Keep history beside the
  primary save action; corrections, command reload, licenses, and model removal
  live in the secondary actions menu.
- **Configurações:** show one settings section at a time. Changes identify
  whether they apply immediately or to the next recording.

The app keeps Tk and its single GUI-thread root because capture, tray, and
macOS integration depend on that architecture. On macOS, native Aqua controls
keep their own appearance; `ui_theme` paints only the surfaces and text that Tk
can color safely. Tk cannot render Mica, acrylic, or rounded native controls
consistently, so opaque layers and a one-pixel border carry the hierarchy.

## Visual contract

- Windows light: neutral canvas `#F3F3F3`, white content, text `#1A1A1A`,
  action blue `#005FB8`.
- Windows dark: canvas `#202020`, content `#2B2B2B`, text `#F5F5F5`,
  action cyan `#60CDFF`.
- Segoe UI on Windows; platform system fonts elsewhere. Use clear page titles,
  body text, and secondary captions rather than decorative headings.
- Spacing follows the 4/8/12/16/24 portion of the system scale. Neutral Tk
  buttons use a visible fill and stronger border so they remain distinct on
  white cards. The recording and library workspaces were visually checked at
  the 920 × 700 Windows minimum size; the recording page scrolls to its meters.
- Focus, selection, status, and disabled state must remain visually distinct.
  Never use color as the only explanation of an error or recording state.

## Interaction contract

- The current state appears beside the action it governs. Errors identify a
  next step, and technical detail is available on demand.
- Recording and model work stays off the GUI and keyboard callback threads.
- Search, filters, and the selected recording preserve context while the user
  moves among detail sections.
- The main destinations support `Ctrl+1` through `Ctrl+4` in displayed order.
- Destructive operations retain their existing explicit confirmation and
  recoverable recording behavior.

## Verification still required

Before a release, exercise the packaged app on Windows at 100%, 125%, 150%,
and 200% display scale; minimum and default window sizes; light and dark
appearance; keyboard-only use and screen reader; empty and populated library;
and recording, interruption, playback, and recovery. Run a macOS smoke to
confirm Aqua controls and the single-root GUI-thread behavior.
