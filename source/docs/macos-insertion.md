> Historical Sniptype record, retained unchanged from the predecessor. Release, path, and issue references describe Sniptype. See [extraction.md](extraction.md) for the current Snipvoice boundary.

# macOS insertion path: paste timings, erase, secure input

Historical notes on [runtime insertion](../runtime_support.py) and
[per-platform timings](../platform_support.py). The original work item predates
this repository's current issue numbering. The expansion insertion path was
initially tuned against Windows timing. This
documents what the delays are for, which values macOS uses and why, and what a
real-host pass still has to confirm by hand.

> **STATUS: historical, partial host verification.** The pasteboard half was
> measured on the original host (recorded as macOS 15 / Darwin 25.5.0, with
> Python 3.14.6; OS labels not rechecked), and the constants follow from that
> measurement. The **synthesized-keystroke half was not exercised**: it needs
> Input Monitoring *and* Accessibility granted to the running process, which is
> a TCC decision no script can make for itself. The manual matrix at the bottom
> is the remaining work, and it needs a human at a granted Mac.
>
> The original report later recorded both grants on that host; the app's probe
> reports `Monitoramento de Entrada=granted, Acessibilidade=granted` — so the
> setup was no longer the obstacle at that time. Current grants and the new
> package's paste behavior have not been reverified. "Getting a Mac into a state
> where the matrix can run" below is what that took; it is the part worth not
> rediscovering.

## The sequence

Typing a trigger produces, in order:

1. **Erase** — one synthesized backspace per trigger character, on the listener
   thread, with `erase_key_delay` between them (`_erase_chars`).
2. **Snapshot** — read the current clipboard text, under the global paste lock.
3. **Set** — write the payload to the clipboard.
4. **Settle** — sleep `clipboard_settle_delay`, so the payload is really there
   before anything asks for it.
5. **Paste** — press the paste shortcut: Cmd+V on macOS, Ctrl+V elsewhere
   (`platform_support.paste_modifier_is_cmd`).
6. **Restore** — for eligible single-line pastes, sleep `paste_restore_delay`,
   then restore the snapshot if the clipboard still holds the payload.
   Multi-line pastes intentionally retain their payload on the clipboard.

Steps 4 and 6 are the two guesses in the design. Step 4 guards against pasting
before the clipboard is populated; step 6 against restoring before the target
app has read it. Neither has a completion signal to wait on, on either OS.

## The values

Resolved by `platform_support.insertion_timings(settings)`; defaults per OS,
each key overridable from `settings.json`.

| Key | Windows | macOS | Linux |
| --- | --- | --- | --- |
| `clipboard_settle_delay` | 0.05 | **0.02** | 0.05 |
| `paste_restore_delay` | 0.12 | 0.12 | 0.12 |
| `erase_key_delay` | 0.01 | 0.01 | 0.01 |

An override that is not a number, is negative, or exceeds 2 s is ignored and
logged at startup — a delay written in milliseconds (`"erase_key_delay": 10`)
would otherwise freeze the listener thread for ten seconds per character.

### Why macOS settles faster

Measured on this host, 15 iterations per payload, using the app's own
`Clipboard` backend:

| Payload | `set_content` | readable with **zero** added delay |
| --- | --- | --- |
| plain, single line | ~13 ms | 15/15 |
| multi-line | ~12 ms | 15/15 |
| rich text (`osascript`) | ~12 ms | 15/15 |

`pbcopy` and `osascript` are separate processes that exit only after
NSPasteboard holds the data, so by the time `set_content` returns the payload is
already servable — and the subprocess round-trip has itself burned ~12 ms, more
than a fifth of the Windows settle window. Windows writes the clipboard
in-process with `SetClipboardData` in microseconds and needs its own margin;
copying that margin to macOS is latency for nothing. 20 ms is kept as slack, not
as a requirement.

Linux keeps the Windows value: `xclip`/`wl-copy` fork a daemon that owns the
selection, so tool exit does not prove the selection is servable yet.

### Why `paste_restore_delay` did not move

It is the only delay whose correct value depends on the *target application*
reading the pasteboard, and that could not be exercised here (see status). It
stays at the Windows value on every OS until someone measures it against real
targets; the key exists so that tuning it is a settings edit, not a patch.

macOS does have more natural slack than the number suggests: the restore path
reads the clipboard (~8 ms) and writes it (~12 ms) through subprocesses, so the
real window between Cmd+V and the snapshot landing is ~20 ms wider than on
Windows.

## Secure input

macOS Secure Keyboard Entry — Terminal's menu item, a focused password field,
the login window — is a separate gate from TCC and one the user cannot grant.
While it is on, the system drops synthesized events in both directions: the
listener never sees the trigger, and backspaces and Cmd+V never land.

`macos_permissions.secure_input_enabled()`
(`Carbon/IsSecureEventInputEnabled`) is checked in `_dispatch_expansion`
**before the erase**, and a positive answer aborts the expansion with a tray
notification (60 s cooldown) and a log line. Checking before the erase is the
point: the trigger is left exactly as the user typed it rather than partially
eaten by backspaces, and no synthesized key is emitted that some other app might
receive when focus moves.

The probe is best-effort by design — a missing symbol or a raising call reports
"not secure" and expansion proceeds as before, because a probe failure must not
disable the app. In practice the case usually never reaches this check at all:
secure input also blocks the listener, so the trigger is typically never
detected in the first place. The gate covers the window where secure input turns
on between detection and insertion.

## Getting a Mac into a state where the matrix can run

Four traps sit between a working checkout and a granted app, and every one of
them fails *silently* — the app starts, the tray appears, and nothing expands.
All four were hit on a real host; this is the order that works.

**1. Launch the bundle through LaunchServices, never its binary directly.**
Running `Sniptype.app/Contents/MacOS/Sniptype` from a shell makes macOS
attribute TCC to the *responsible process* — the terminal — so the app reports
both grants `denied` no matter what is ticked in System Settings. `open` gives
the process the bundle's own identity. To point a launch at a throwaway library
without losing that:

```bash
open --env SNIPTYPE_HOME=/tmp/matrix-home ~/Applications/Sniptype.app
```

That is the only way to run the matrix against test snippets while the real
`~/.sniptype` library stays untouched.

**2. Install the bundle somewhere permanent before granting anything.** A grant
is pinned to the bundle it was given to; granting a copy inside a git worktree
or a temp dir means re-granting as soon as that path goes away. `~/Applications`
is the per-user location and matches the Windows per-user install.

**3. Sign with a stable identity, or every rebuild silently revokes the grants.**
Under ad-hoc signing TCC pins the grant to the binary's cdhash, so a rebuild
orphans the System Settings row: it keeps the app's name and icon, stays ticked,
and matches nothing on disk. **Un-ticking and re-ticking does not fix it** — the
row has to be selected and removed with `−`, then re-added with `+`. Any real
code-signing identity avoids this entirely (see `build_release_macos.sh`).

Note `codesign` cannot reach a private key from a non-interactive shell — it
fails with `errSecInternalComponent` because there is no one to answer the
keychain prompt. Run the build from an interactive terminal once and choose
*Always Allow*; after that the partition list stops asking.

**4. Automating the *typing* needs a grant of its own.** Driving the matrix from
a script (System Events `keystroke`, or pynput) requires Accessibility for the
terminal running it, which is a second, broader grant — it is system-wide
keylogging for anything that terminal launches. Check it with:

```bash
osascript -e 'tell application "System Events" to return UI elements enabled'
```

`false` means synthesized keystrokes will be dropped. Typing the triggers by
hand needs none of this and is the intended way to run the checklist below.

## Remaining manual matrix

Run on a Mac with both Input Monitoring and Accessibility granted to the app
(tray → permissions entry if either is missing; a grant needs an app restart —
the frameworks read TCC at process start). Confirm the grants landed by reading
the app's own startup probe rather than trusting the System Settings checkbox,
which can be a stale row:

```bash
grep "Permissões do macOS:" ~/.sniptype/logs/sniptype.log | tail -1
```

For each target — **TextEdit**, a **browser textarea** (Safari or Chrome),
**Mail**, and a **chat app** (WhatsApp Web) — expand:

- [ ] a single-line snippet
- [ ] a multi-line snippet
- [ ] a rich-text snippet

and check, each time:

- [ ] the trigger is **fully erased** (no leftover characters, nothing eaten
      from the text before it)
- [ ] the payload pastes **once**, complete, with newlines intact
- [ ] the previous clipboard is **restored** (single-line snippets only —
      multi-line deliberately does not restore; see the ceiling comment in
      `TextInserter._restore_clipboard`)
- [ ] typing the trigger **fast**, mid-sentence, produces the same result

Then the failure cases:

- [ ] Terminal with **Secure Keyboard Entry** on: the trigger stays on screen
      untouched, a tray notification explains why, nothing is pasted.
- [ ] a **password field**: same, and the field is never corrupted.

If a target needs a different `clipboard_settle_delay` or `paste_restore_delay`,
record the value and the target here and change the macOS default in
`platform_support._INSERTION_TIMING_DEFAULTS` — not the shared one.
