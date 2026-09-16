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

## Public repository privacy gate

This repository is public. Treat committed files, commit/tag messages and
identities, PR descriptions, CI logs, and release assets as permanent disclosures.

Before committing or publishing:

- Stage only explicit task-owned paths. Inspect the full staged diff and file
  list, including untracked additions and binary contents/metadata. Do not commit
  generated artifacts, installers, archives, diagnostic dumps, or backups merely
  because they were produced during the task.
- Never include credentials, tokens, cookies, private keys, signing material,
  `.env` contents, live settings, recordings/transcripts, clipboard/snippet data,
  personal emails, phones, addresses, CPF/CNPJ identifiers, household/device
  details, private network endpoints, confidential client data, or private
  product/roadmap details. Use synthetic fixtures and generic paths (`$HOME`,
  `%USERPROFILE%`); sanitize screenshots and examples. Preserve legitimate public
  license/copyright attribution. Documented synthetic test credentials are allowed
  only for their narrow fixture purpose; never broadly allowlist real secrets.
- Run the available secret scanner on staged content before committing and on
  every outgoing commit/ref before pushing. Manually review privacy data and
  metadata that scanners miss. If no scanner is available, disclose the gap and
  complete a documented manual review; never claim a scanner ran. Separately run
  `git diff --cached --check` for formatting.
- Set repository-local `user.email = rteoo@users.noreply.github.com` and
  `user.useConfigOnly = true`. Verify effective author/committer identities before
  each commit and tagger identity before an annotated tag, including environment
  and command-line overrides. New owner-authored metadata must use that noreply
  address. Preserve legitimate third-party contributor attribution.
- Before an authorized push, inspect the exact remote/refspecs and every outgoing
  commit/tag, message, and reachable history. Never merge or push a pre-redaction
  branch/tag that reintroduces private identities or data. `.gitignore`, noreply
  configuration, and a clean working tree do not prove tracked files or history
  safe. Preserve hooks, signing, secret-scanning push protection, and branch
  protections; never bypass them.
- If a leak is found, stop committing/publishing the affected material. Report
  only redacted categories and locations, never the sensitive value. Deleting a
  file later does not erase Git/PR/release history. Credential rotation, history
  rewrites, force pushes, ref deletions, and external cleanup need explicit
  authorization for exact targets. This policy grants no push, PR, release, or
  history-rewrite authorization.
