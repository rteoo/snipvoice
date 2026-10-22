# Snipvoice interface

Snipvoice adopts the **Teô Personal Design System 2.7.1, Windows family** (Ink &
Copper on the shared native neutral surfaces) for its Windows manager, and
adapts the same hierarchy for macOS. This file is the project's adoption
record: what was adopted, how it maps onto Tk, where Tk forces a deviation, and
which release gates have evidence.

## Adoption record

| Item | Decision |
|---|---|
| Design system | Teô Personal Design System 2.7.1, Windows native family |
| Ownership | `source/ui_theme.py` owns every color, font, metric and control face. Screen builders ask it for tokens, type roles and themed controls; literal colors and font families in the GUI modules fail `tests/test_ui_theme.py`. |
| Primary workflow | Record a meeting (Gravação), find and read it (Biblioteca), dictate anywhere with the hotkey (Ditado). Settings stay secondary. |
| Stack | Tk/ttk on the process's single `GuiThread` root. No WinUI, no new toolkit: capture, tray and macOS integration depend on this architecture. |
| Platforms | Windows 10/11 (primary; this adoption was checked on Windows 11 at 100% scale), macOS (Aqua, not verified in this adoption), Linux (literal palette, unsupported GUI). |
| Appearance | **Dark by default.** Explicit Light and System stay available; an unset or unreadable stored value resolves to Dark. A stored `system` or `light` is kept. |
| Density | **Compact**: 32px minimum for controls and rows (buttons, menu buttons, entries and comboboxes measure 33px; list rows 32px). Every `ui_theme.button` is Compact, so an action row never mixes heights. |
| ttk theme | `clam` in both appearances on Windows (Vista ignores the palette). macOS keeps `aqua`. |

## Visual contract

### Surfaces and roles

Windows family values, as `ui_theme` tokens. Hierarchy comes from opaque
surface steps and one-pixel lines; Tk cannot render Mica or acrylic.

| Role (`ui_theme` token) | Dark | Light |
|---|---|---|
| Navigation and window base (`surface_alt`) | `#292929` | `#e9e8e7` |
| Page (`surface`) | `#232323` | `#f6f5f4` |
| Content, fields, controls (`card`, `field`, `control`) | `#1c1c1e` | `#ffffff` |
| Selection, hover, pressed (`select_bg`, `surface_hover`, `control_active`) | `#484848` | `#e8e6e3` |
| Decorative lines (`border`, `divider`) | `#454545` | `#d8d8d8` |
| Control boundary (`field_border`, `control_border`) | `#999ca3` | `#7b8088` |
| Text (`text`) | `#F2EFE9` | `#202630` |
| Secondary text (`text_muted`) | `#bcbcbc` | `#606060` |
| Disabled text (`disabled_fg`, text blended 45% over the control) | `#7C7B79` | `#9B9DA2` |
| Primary action fill / pressed (`accent` / `accent_active`) | `#E6A05A` / `#CE8845` | `#E6A05A` / `#BE7836` |
| Label on copper (`text_on_accent`) | `#15100A` | `#15100A` |
| Selection marker (`accent_mark`) | `#E6A05A` | `#A0520A` |
| Link and quiet action (`link`) | `#EDB47C` | `#A0520A` |
| Keyboard focus (`focus_ring`) | `#F0C293` | `#263C56` |
| Success / warning / danger / info (`*`, `*_bg`) | `#73E0A1`/`#13271D` · `#FFD27A`/`#342A17` · `#FF8A85`/`#351718` · `#ADC1DC`/`#202E40` | `#16643B`/`#E6F6EC` · `#7A4A00`/`#FFF1D6` · `#9B2C2C`/`#FCE8E8` · `#263C56`/`#E8EEF5` |
| Chart primary / secondary / neutral | `#E6A05A` / `#8CA8CF` / `#788697` | `#A0520A` / `#263C56` / `#7B8492` |

Copper marks the one next action and the current selection. It never washes a
surface, never colors body text, and never means danger.

On macOS, Aqua's dynamic system colors keep the surfaces and text in System
mode; only the accent, link and focus roles take the Ink & Copper values. An
explicit Light or Dark preference uses the opaque table above, which is also
the macOS family's neutral hierarchy.

### Dictation and recording HUD

The overlay floats over arbitrary applications, so it stays dark in both
appearances (`overlay_*` tokens): surface `#1c1c1e`, line `#454545`, text
`#F2EFE9`, secondary `#bcbcbc`. Its bars carry one tone:

| Tone | Color | Used for |
|---|---|---|
| live | `#E6A05A` | Listening, listening for a command, recording |
| busy | `#8CA8CF` | Transcribing, saving, local processing, final audio export |
| ok | `#73E0A1` | Inserting text into the active field |
| muted | `#bcbcbc` | Recording paused |
| trouble | `#FF8A85` | Recording interrupted or failed, capture error (bars static) |

The macOS panel draws the live dot in copper and uses the system green, red,
gray and accent colors for the other tones.

### Type

Windows uses the family ramp in pixels, scaled by Tk's own DPI factor, with
Segoe UI Variable's optical faces when GDI lists them (Text up to 14px, Display
from 18px) and Segoe UI otherwise. Semibold uses the dedicated semibold face;
bold is synthesized only where no semibold face exists.

| Role | Size | Weight | `Theme` method | Legacy `font(n)` |
|---|---|---|---|---|
| Caption | 12px | regular | `caption()` | 8 |
| Body | 14px | regular | `body()` | 9, 10 |
| Body strong | 14px | semibold | `body_strong()` | 9/10 bold |
| Heading | 18px | semibold | `heading()` | 11, 12 |
| Title | 28px | semibold | `title()` | 14 |
| Literal code | 14px | regular | `mono()` (Cascadia Mono, else Consolas) | — |

`TkDefaultFont` and `TkTextFont` follow the body role, so unstyled comboboxes
and labels match. macOS and Linux keep the legacy point scale shifted to the
platform's default size. Call sites still use legacy sizes; later passes move
them to the named roles.

### Controls

- `ui_theme.button(parent, text, command, variant)` builds every manager
  button: `primary` (copper fill, one per decision area), `neutral` (content
  fill with the control boundary), `quiet` (link-colored text), `danger`
  (danger pair). A disabled button shows no copper and no danger fill. On
  macOS it returns a native Aqua button and paints nothing.
- Focus is a 2px ring in `focus_ring` on buttons, entries, text fields,
  comboboxes, lists and the playback slider. The idle edge (1px, or none on
  navigation and filled buttons) gives its pixels back from padding, so focus
  never moves a control.
- Entries, editable text fields (chat composer, summary focus, report editor,
  command text) and list boxes draw a flat 1px `field_border` edge in both
  appearances (`Theme.entry_chrome()`). Read-only text panes (transcript,
  segment, summary, cross-recording answer, recording notice) draw the
  decorative line instead, and take the same focus ring when clicked into.
- Navigation: the selected sidebar item sits on the selection plane with a
  3×16 `accent_mark` marker and semibold text. Section switchers show the
  marker only (a 2px underline when horizontal). Labels never turn copper.
- Lists use 32px rows and the selection plane. Column headings sit on the
  content plane in semibold with a 1px decorative line under them (and between
  columns), so a selected first row never merges into the heading. Meters and
  the waveform use the chart roles (microphone primary, system secondary, idle
  or paused neutral).
- The Biblioteca list reads Estado · Gravação · Data · Duração. The state is a
  written word (see *Recording states*) whose row takes its tone; the title is
  cut with "…" to its column; the date is local (`meeting_text.
  format_local_timestamp`); the duration is right-aligned. A selected row
  draws the selection text, so the word, not the color, carries the state.
  The title keeps at least 190px (about 20 characters): as the list narrows,
  Duração goes first, then Data, and a column shows only when the title
  keeps that minimum. The widths follow the language, so the sizes are a
  guide: in pt-BR the minimum and default windows show Estado · Gravação and
  1440×900 shows all four. The list starts at 47% of the panes, never
  narrower than its toolbar and never so wide that the selected recording's
  controls clip, also when a first recording arrives in an empty library.
  The count under the list is plural-correct ("1 gravação", "12 gravações")
  and names the page only when there are pages.
- `ui_theme.info_bar(parent, ui, tone, title, body, action, details, copy, when,
  dismiss)` is the in-page message: tone fill with a 1px tone edge (the fill
  alone is 1.0–1.14:1 against the page), a glyph per tone (⊗ ⚠ ⓘ ✓, until the
  app adopts an icon set), a semibold title (what happened; it wraps), an
  optional body (what it affects and what to do), at most one neutral action,
  and technical text behind "Detalhes". "Copiar" appears when the caller passes
  `copy(text, done)`, which writes through the app's clipboard backend off the
  GUI thread; the bar says "Copiado" only after it succeeds and otherwise
  "Selecione o texto e pressione Ctrl+C". `when` writes the severity word and the
  local time at the right of the title, which wraps to make room ("Erro às
  14:02", "Aviso às 14:02"; Informação, Concluído), so the severity never rests
  on color or glyph alone.
  `dismiss` adds a quiet "Fechar". Tones: danger, warning, info, success.
- `ui_theme.MessageBar` is a page's one feedback slot. It has no height until the
  page has something to say; trouble is an InfoBar and everything else one quiet
  caption line ("Título salvo · 14:02"). A new message replaces the last, so a
  page never stacks a history. `StatusChip` writes the recorder's state beside a
  drawn tone dot ("● Gravando · 00:45:54"): the word carries the meaning, the dot
  only the tone, and copper means live.
- The selected recording's header shows its state word in its tone, then its
  date and duration. A failed processing step raises a danger InfoBar under it,
  a cancelled one a warning InfoBar, with the step's own fix: "Transcrever
  novamente", "Preparar áudio final" (also when a retranscription needs the
  final audio first) or "Gerar resumo". The bar steps aside while a job works on
  the recording; when the job ends the bar returns only with the store's
  current answer, never the failure it replaced. An empty transcript says why:
  processing now (read from the same job as the header, so it never repeats the
  failure the job replaced), failed, cancelled, or not yet processed.
- Win32 shows a Tk button's active colors only while it is pressed, so
  `accent_active` is the pressed role. Tk has no hover state for these
  buttons; the primary stays `accent` under the pointer.

Spacing follows the 4/8/12/16/24 portion of the system scale.

### Tk deviations from the Windows family

| Family rule | Snipvoice on Tk | Why |
|---|---|---|
| 4px control corners | Square corners | Tk has no reliable rounded-corner primitive. |
| Mica window base | Opaque `#292929`/`#e9e8e7` base | Tk cannot render materials; opaque fallback. |
| Focus ring with an offset | 2px ring touching the control; Win32 push buttons also draw their dotted focus rectangle inside it | Tk has no outline offset. Win32 never draws a push button's highlight ring, so the ring is the button's default ring, which Win32 always pairs with its own focus rectangle. The dark ring on a copper fill is 1.35:1 against the fill but ≥5.59:1 against every surface around it. |
| 0.45 disabled opacity | `disabled_fg` blend | Tk has no opacity. |
| Fluent icons in navigation | Text-only navigation | Not adopted yet. |
| Copper selection pill in lists | Selection plane only | Not adopted yet. A Treeview can carry the pill as an image in its tree column (`#0`); until a list adopts it, the plane alone marks selection. In Light the plane is 1.25:1 against white content (a DS 2.7.1 gap). |
| Copper selection pill in combobox popdowns | Selection plane only | The popdown is a Tk listbox, which cannot draw a marker or an image. |
| State tone on one list cell | The whole row takes the state's tone; Concluída rows stay in text color | Only Tk 9 can tone one cell (`tag cell`), and the release bundles ship Python 3.12 with Tk 8.6. On Tk 8.6.12 and 9.0.4 a selected row draws `select_fg` over any tag (probed), which keeps dark danger off the selection plane (4.02:1). |
| Hover on filled and neutral buttons | No hover face on Windows | Win32 Tk paints a button's active colors only while it is pressed. |
| Checkbox focus ring | Win32's dotted focus rectangle | Tk checkboxes draw no highlight ring on Windows. |

### Contrast (computed by `tests/test_ui_theme.py`)

On navigation / page / content / selection:

| Pair | Dark | Light |
|---|---|---|
| Text | 12.68 / 13.70 / 14.83 / 7.97 | 12.42 / 13.96 / 15.20 / 12.20 |
| Secondary text | 7.66 / 8.28 / 8.96 / 4.82 | 5.14 / 5.78 / 6.29 / 5.05 |
| Control boundary | 5.29 / 5.72 / 6.19 / 3.33 | 3.25 / 3.65 / 3.97 / 3.19 |
| Focus ring | 8.90 / 9.61 / 10.41 / 5.59 | 9.21 / 10.35 / 11.27 / 9.05 |
| Selection marker | 6.61 / 7.14 / 7.73 / 4.16 | 4.63 / 5.20 / 5.67 / 4.55 |
| Label on copper (idle / pressed) | 8.59 / 6.51 | 8.59 / 5.34 |

## Product structure

- **Gravação:** check sources, record, and read the current state. Keep the
  primary recording action and recovery state visible together. Idle shows one
  transport control, **Iniciar gravação** (copper, with the shortcut beside it
  when one is set). While capturing, **Parar e salvar** takes its place as the
  copper action beside **Pausar** (**Retomar** while paused); a control that
  cannot act is not drawn. Each source's meter carries a written health line
  ("Recebendo sinal", "Sem áudio · há 1 min 05 s", "Não incluído nesta
  gravação"). A readiness strip leads the independently scrollable left setup
  card: a quiet line when local models are ready, otherwise compact rows for
  pending or unknown inventory, missing models, current processing, recent-page
  failures, dictation off and startup recovery. Each actionable row has one
  command. It does not enlarge the right-hand transport. Waveform lanes hide
  while idle and return for preview or capture. A combined lost-source warning
  labels the longest interruption; captions retain each source's own duration.
- **Biblioteca:** search and select a recording, then read the full transcript.
  Full text with paragraphs is the default; users can switch to timestamps
  afterward without running transcription again. Copy and export use that
  presentation. Audio, summary, questions, and files remain accessible.
  The manual Notes tab is removed; existing notes and bookmarks remain in saved data.
  Transcription has no manual speaker-label, highlight, or clip controls; existing
  annotations remain stored for compatibility.
  There are no manual category, tag, person, series, or status filters.
  New recordings use installed transcription and summary models automatically;
  unavailable models are skipped without downloads. Summaries are readable and
  saved automatically. **Resumo** starts with its readable result. A collapsed
  secondary row opens generation and its format picker for meeting notes,
  interviews, one-on-ones, sales calls, and feedback conversations. Optional
  focus guidance is collapsed under Personalizar. Generate saves a new version;
  the complete readable result and Copy remain in the main view. Version history
  and custom format management appear when requested. Saved chat answers do not
  replace the summary shown when a recording is reopened.
  Reselection preserves the chosen detail section and transcript presentation;
  a saved summary becomes the default only before an explicit section choice.
  The selected recording's processing banner shows the controller stage,
  elapsed time since observation and Cancel. Only real audio-export progress
  supplies a percentage. A completed summary offers Copy (Ctrl+Shift+C) and
  selects Resumo only while its original navigation/section context remains.
  **Chat** keeps a temporary conversation for each recent recording, with a
  bottom composer and sources, Copy, and Save on each answer. Follow-ups receive
  bounded context; transcript segments remain the source of factual evidence.
- **Ditado:** choose the active model and language, then use the hotkey. Model
  downloads and privacy controls belong in settings. Keep history beside the
  primary save action; corrections, command reload, licenses, and model removal
  live in the secondary actions menu.
- **Configurações:** show one settings section at a time. Changes identify
  whether they apply immediately or to the next recording.

## Interaction contract

- Readiness inventory runs through the background bridge, admitting at most
  one outstanding read per second. Summary inventory reuses its existing
  off-thread load. Pending/unknown inventory never becomes an all-clear line.
  Transcription selection follows the controller's actual installed fallback
  order without refreshing manifests. A bounded 64-entry in-memory cache avoids
  rehashing legacy models; model-file or manifest stat changes invalidate a hit.
  Like persisted verified-stat metadata, an external rewrite preserving every
  stat identity is outside this optimization's detection boundary. The probe
  downloads nothing and writes no manifest.
- WP5 source validation covers eight dark/light, size and language combinations
  with actual window captures. The final full suite ran 1,549 tests in 198.969s
  and matched all 60 expected NumPy failure headers exactly, with 20 skipped.
  This is a baseline match, not an all-green suite or packaged-build proof.

- The current state appears beside the action it governs. Errors identify a
  next step, and technical detail is available on demand.
- **Feedback is written on the page where it happens.** There is no global footer.
  Each destination (Gravação, Biblioteca, Configurações) owns one message slot
  (`MessageBar`) above its content. What went wrong is a toned InfoBar with what
  happened, what it affects, one action and the cause behind Detalhes (with
  Copiar); a routine outcome is one quiet line with its time; success without a
  receipt says nothing. A slot empties when the user leaves the page, when the
  next message replaces it, and, for a message about one recording, when another
  recording is chosen. Only a standing condition (local recovery pending) outlives
  navigation, and it cannot be dismissed. A message written while the user is
  elsewhere waits on its own page. The technical text is bounded (4 KiB, paths
  redacted) and kept per page and recording, never in one shared string; no
  message sends the user to another tab ("veja a aba Gravação" is gone, and
  `tests/test_i18n.py` fails if it returns).
- **Recording trouble is loud and names its source.** A source that delivers no
  audio shows a warning InfoBar above the recorder state once it has been silent
  for 3 seconds (a headset switch mends itself in less): "Sem áudio do sistema
  há 1 min 05 s", that the recording continues and resumes by itself, and
  **Trocar dispositivo**, which takes the user to that source's selector and says
  a new choice only applies to the next recording (the capture helper cannot
  switch endpoints live). The same source reads "Sem áudio · há …" under its
  meter and in the state line ("Gravando · 00:45:54 · Áudio do sistema: sem
  áudio"); the dot turns warning. An error the recorder reports with no source
  lost stays as a dismissable bar until it is dismissed or ends. At 1040×700 the
  meters, the warning and the transport stay in view; only the hint line under the
  recorder may move below the fold.
- **Cancel exists only while there is something to cancel.** The semantic-index,
  index-repair, cross-recording, summary and model-download Cancel controls, and
  the processing one, are drawn only while their job runs, inside a reserved
  fixed-size slot in their row, so the controls beside them
  and the page below do not move. A control that cannot act says what it waits
  for in the line beside it ("Aguarde o resumo terminar.", "Transcrever de novo
  requer o áudio final. Use Preparar áudio final, em Arquivos.", "O áudio final
  está completo; não há nada a recuperar.").
- **Chat answers fail visibly.** A failed answer is a danger InfoBar in the
  conversation ("Não foi possível responder", **Tentar novamente**, the cause
  behind Detalhes); retry puts the question back and sends it again. A missing
  summary model is a warning notice above the composer with **Abrir Configurações
  › Modelos**. The uncertainty label is always written and takes its tone from the
  level: low is secondary text, medium warning, high danger.
- Recording and model work stays off the GUI and keyboard callback threads.
- Search and the selected recording preserve context while the user
  moves among detail sections.
- The main destinations support `Ctrl+1` through `Ctrl+4` in displayed order.
- Destructive operations retain their existing explicit confirmation and
  recoverable recording behavior. Deleting from the trash is permanent, so
  "Excluir definitivamente…" and "Esvaziar expirados…" open a themed dialog that
  names the target (the recording, or how many expired), states that it cannot
  be undone and is not a forensic erase, and enables its danger button only
  after "EXCLUIR" ("DELETE" in English) is typed. Cancelar is the safe way out.
- The trash opens at 720×420 with every action visible: its action row and
  status line claim the bottom first and the list gives up rows. The first row
  is selected; Enter restores, Delete asks to delete permanently, Esc closes and
  returns focus to the control that opened it. "Restaurar" is the primary and,
  like "Excluir definitivamente…", needs a selected row; "Esvaziar expirados…"
  needs an expired one, and the count says so when none has expired
  ("2 gravações na lixeira · nenhuma expirada"). A finished action's receipt
  leads the refreshed count ("Gravação …1c2a9e8b restaurada na Biblioteca · 1
  gravação na lixeira · 1 expirada"). The trash and the typed-confirmation
  dialog take the app icon from the host window (`set_window_icon`); Tk's
  default icon never reaches a transient dialog.
- Closing or rebuilding the manager (theme or language change) never
  cancels a recording's work: its capture, final audio, transcript and summary
  keep running. A close stops only the chat, cross-recording and summary
  answers that window asked for. The processing, summary, cross-recording and
  chat Cancel controls exist only while their job runs (see the Cancel rule
  below). The processing one is a plain "Cancelar" under a line that names the
  running step ("Em execução: a transcrição.") and wraps at any width, so no job
  name can clip the button at the 1040x700 minimum; the chat one returns the
  question to the composer.
- A held Start names its holder under the button: "Aguardando o resumo de
  “Reunião 14/10”." The recorder status never reads "Pronto" while Start is
  disabled; it leads with Processando, Testando or Aguardando, and the waveform
  caption ("Estado: Processando") repeats that word. When processing holds it,
  the secondary "Interromper e gravar" confirms the consequence and records as
  soon as the job stops. For a step of the recording's own pipeline (final audio,
  transcription, summary, temporary file cleanup) it says the original audio
  stays and the step is redone in Biblioteca; for any other job (chat answer,
  export, import, source test) it says only that the task stops and can be run
  again later. The tray item reads "Iniciar gravação (aguardando o
  resumo)", and a refused hotkey start names the holder.
- Focus, selection, status, and disabled state must remain visually distinct.
  Never use color as the only explanation of an error or recording state.

## Operations extensions (proposed DS 2.8)

Adopted as project rules ahead of the cross-app operations layer; they reuse
2.7.1 tokens only.

| Rule | Snipvoice adoption |
|---|---|
| OPS-01 one state per view | "Pronto" only when Start works. An idle recorder held by a job reads Processando (Testando for a source test); held by anything else, Aguardando. The status line and the waveform caption always show the same word. |
| OPS-08 jobs | Closing never cancels. A blocked command names its blocker and offers an override that states its consequence ("Interromper e gravar"); the tray label and the hotkey message say the same. The controller names its running job and current step (`processing_kind` in its snapshot). |
| OPS-01 recording states | Every recording has one written state in the library list and in its header (table below). A job working on it (its own pipeline, a generated summary, an import) reads Processando until it ends. |
| OPS-04 failure is not pending | A failed or cancelled processing step is an InfoBar with its reason behind Detalhes and its fix; an empty transcript says whether it is being processed, failed, was cancelled or is still due. |
| OPS-05 time, size, identity | Library and trash dates are local ("Hoje 14:02", "Ontem 09:10", "qua. 14/10 09:10", "14/10 09:10", "14/10/2025"); trash expiry counts down ("em 23 d", then "Expirada"); sizes are decimal with the locale's mark ("46,2 MB"); a missing, malformed or unconvertible date reads "Sem data"; a size rounds before it picks its unit ("100 MB", "1,0 MB", never "1000 kB"). The trash shows a short id ("…1c2a9e8b", the id's random tail) until trashed recordings keep their titles. |
| OPS-07 confirmation ladder | Moving to the trash stays a single confirmation; deleting from it (one recording or every expired one) needs the typed word. |
| OPS-06 feedback channels | One `MessageBar` per destination, cleared on navigation; trouble is an InfoBar with Detalhes and one action, a routine outcome one quiet line. Project extension of the receipt: `<outcome> · HH:MM`, local 24-hour time (the proposal's "Salvo às 14:32" form). A refused command says why and points to its blocker on its own page. |
| OPS-07 busy and disabled | Cancel exists only while its job runs and never moves a sibling; a disabled recovery control names its prerequisite beside it. Not adopted yet: a busy control keeping its label (Gerar resumo still reads "Gerando…"). |
| OPS-01 recording words | The recorder's state word and its tone dot: Gravando (copper), Pausado (secondary), Pronto (success), Processando and Aguardando (info), a lost source or audio resources still in use (warning), a failed recorder (danger). A lost source adds its name after the word. |

Recording states, in the list column and the detail header:

| Saved status | pt-BR | en | Tone |
|---|---|---|---|
| A job is working on it | Processando | Processing | info |
| `recording` | Gravando | Recording | info |
| `completed` | Concluída | Done | none (text color in the list, secondary text in the header) |
| `partial` | Parcial | Partial | warning |
| `interrupted` | Interrompida | Interrupted | warning |
| `failed` | Falhou | Failed | danger |
| `cancelled` | Cancelada | Cancelled | neutral (secondary text) |
| anything else | Desconhecido | Unknown | neutral |

The list reads the capture outcome the library index stores. A failed or
cancelled transcription or summary shows in the recording's header, not in its
row, until the index carries processing outcomes.

Feedback words, one per concept (the shared glossary of the operations layer):

| Concept | pt-BR | en |
|---|---|---|
| Severity beside the time | Erro · Aviso · Informação · Concluído | Error · Warning · Information · Done |
| Retry a failed thing | Tentar novamente | Retry |
| Technical detail, copy it | Detalhes · Copiar · Copiado | Details · Copy · Copied |
| Dismiss a message | Fechar | Close |
| Stop and keep a recording | Parar e salvar | Stop and save |
| Change a lost source's device | Trocar dispositivo | Switch device |
| Go to where a model is installed | Abrir Configurações › Modelos | Open Settings › Models |

Job names, as they complete "Aguardando …", "Interrompendo …" and "Em execução: …":

| Job | pt-BR | en |
|---|---|---|
| Final audio (automatic or recovered) | o áudio final | the final audio |
| Transcription | a transcrição | the transcription |
| Summary (automatic or generated) | o resumo | the summary |
| Raw capture cleanup | a liberação dos arquivos temporários | the temporary file cleanup |
| Chat answer | a resposta do chat | the chat answer |
| Cross-recording question | a pergunta entre gravações | the cross-recording question |
| Saving an answer | o salvamento da resposta | saving the answer |
| Audio import / model import | a importação de áudio / do modelo | the audio / model import |
| Text, audio or report export | a exportação | the export |
| Source test | o teste das fontes | the source test |
| Leases without a job | a cópia do áudio final · a reprodução · a retenção local | the final audio copy · playback · local retention |

## Release gates

Each gate is passed, failed, or not tested, with its evidence. A browser or
scratch-harness screenshot cannot pass a packaged-build gate.

| Gate | Status | Evidence |
|---|---|---|
| Palette and contrast | Passed | `tests/test_ui_theme.py` pins both palettes and checks 4.5:1 text and 3:1 boundaries, focus, marks and chart roles. |
| Dark and Light appearance | Passed (source build) | Real manager built from source with synthetic data, both appearances, Gravação, Biblioteca detail, Ditado and every Configurações section. |
| Live System appearance changes | Failed | The manager re-reads the OS theme only when it is rebuilt. |
| Windows contrast themes | Not tested | Palette does not yet yield to high-contrast system colors. |
| Minimum window 1040 × 700 and default 1120 × 820 | Passed (source build, 100% scale) | No control narrower or shorter than requested at either size; the recording page fits without scrolling at the minimum (`tests/test_voice_gui.py`). The library with a selected recording and its InfoBar clips nothing at 1040×700, 1120×820 and 1440×900 in both appearances (scratch harness on the source build). With a lost source the recording page keeps meters, warning and transport in view at 1040×700 (`tests/test_voice_gui.py`); a Library message bar fits at the minimum width (`tests/test_meeting_gui.py`). |
| Compact 32px density | Passed | Buttons, menu buttons, entries and comboboxes measure 33px, list rows 32px, at 96 DPI; `tests/test_ui_theme.py` and `tests/test_voice_gui.py` check that the Ditado action row keeps one height. |
| Visible keyboard focus | Passed (source build) for buttons, menu buttons, comboboxes, entries, text fields, list boxes, lists, the slider and navigation | Before/after captures in both appearances, including the chat composer, the summary focus field and the citation list; `tests/test_ui_theme.py` checks that text fields and list boxes keep their size through focus. Checkboxes keep Win32's dotted rectangle. |
| Full keyboard path, tab order | Not tested | |
| Display scaling 125/150/200%, mixed DPI | Not tested | The source declares no DPI awareness; fonts follow Tk's scaling factor. |
| Narrator / UI Automation | Not tested | |
| Reduced motion | Failed | The HUD bars animate regardless of the system animation setting. |
| macOS Aqua controls and HUD | Not tested | Requires a Mac. |
| Packaged app smoke (Windows, macOS) | Not tested | |

Before a release, exercise the packaged app on Windows at 100%, 125%, 150%,
and 200% display scale; minimum and default window sizes; light and dark
appearance; keyboard-only use and screen reader; empty and populated library;
and recording, interruption, playback, and recovery. Run a macOS smoke to
confirm Aqua controls and the single-root GUI-thread behavior.
