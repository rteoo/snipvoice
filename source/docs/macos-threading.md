> Historical Sniptype record, retained unchanged from the predecessor. Release, path, and issue references describe Sniptype. See [extraction.md](extraction.md) for the current Snipvoice boundary.

# macOS tray + Tk threading model

Historical investigation of pystray and Tcl/Tk main-thread ownership on macOS.
The implemented contract is in [GuiThread](../gui_thread.py) and
[platform support](../platform_support.py); the original work item predates
this repository's current issue numbering.

> **STATUS: implemented; historical host evidence.** Option 1 was prototyped and then
> implemented: the main thread runs Tk's `mainloop()` and the tray runs
> detached on the `NSApplication` Tk created. The original record lists macOS 15
> and Darwin 25.5.0 (OS labels not rechecked), Python 3.14.6, Tk 9.0.3,
> pystray 0.19.5, and pyobjc-Cocoa 12.2.1 — see
> "What was measured" below. The Windows path is unchanged: it takes the same
> branch it always did. These measurements do not certify the current package;
> a fresh macOS desktop smoke pass remains necessary.

## The collision

On Windows the process runs two event loops on two threads and it works:

- `main()` → `Sniptype.run()` → `pystray.Icon.run()` **blocks the main
  thread** with the win32 message loop (`sniptype.pyw`, end of `run()`).
- The process's only `tk.Tk()` root lives on a **dedicated worker thread**
  (`gui_thread.py`, class `GuiThread`). Workers marshal onto it with
  `call`/`submit`; a `root.after(40ms, …)` pump drains the queue. Tk on Windows
  tolerates living on a non-main thread as long as *one* thread owns it.
- The pynput listener runs on its own thread and never touches Tk.

macOS breaks the "two loops, two threads" assumption on two independent counts:

1. **AppKit owns the main thread.** pystray's `darwin` backend drives an
   `NSApplication` / `NSRunLoop`. Cocoa requires that loop to run on thread 0
   (the main thread). This is not a pystray choice; it is an AppKit invariant.
2. **Aqua Tk owns the main thread.** The macOS Tk build (Aqua) is itself a Cocoa
   application: creating a `tk.Tk()` instantiates the shared `NSApplication`, and
   Tk's event handling is only reliable on the main thread. Off-main-thread Tk on
   macOS crashes or misrenders — the exact failure mode `GuiThread` relies on
   *not* happening on Windows.

So both frameworks want the main thread, and both want to run an
`NSApplication` loop. The saving grace — and the hinge of the whole design — is
that **there is only one `NSApplication` per process** (`+sharedApplication` is a
singleton). Tk creates it; pystray can be told to reuse it instead of trying to
own its own.

## The invariant that must survive

Whatever lands, the `GuiThread` public contract is load-bearing and must not
change (AGENTS.md, and every worker in the expansion path depends on it):

- Workers call `GuiThread.call(func, timeout)` (blocking, returns the result,
  re-raises) and `GuiThread.submit(func)` (fire-and-forget).
- The keyboard listener **never** calls into Tk.
- There is exactly one `tk.Tk()` in the process.

What may change is the *physical thread* the pump runs on. On Windows it is a
spawned worker thread; on macOS it must become the main thread. The marshaling
API is identical either way — `call`/`submit` push onto a queue drained by a
`root.after` pump; the pump does not care which thread it ticks on, only that
all Tk access is funnelled through it. This is why the abstraction was built the
way it was, and it is what makes the port tractable.

## Options evaluated

### Option 1 — main thread runs Tk `mainloop()`, tray runs detached (IMPLEMENTED)

Invert ownership on macOS:

1. On the main thread, create the single `tk.Tk()` root (this instantiates the
   shared `NSApplication`).
2. Obtain that singleton: `AppKit.NSApplication.sharedApplication()`.
3. `pystray.Icon(..., darwin_nsapplication=<that NSApplication>)` then
   `icon.run_detached()` — the kwarg goes to the constructor; pystray
   0.19.5's own `run_detached` docstring documents exactly this parameter for
   macOS: *"Pass an instance of `NSApplication` retrieved from the library with
   which you are integrating … This will allow this library to integrate with
   the main loop."* pystray then schedules its status-item work onto the shared
   run loop instead of starting a second one.
4. Enter `root.mainloop()` on the main thread. That drives the shared
   `NSRunLoop`; the detached tray rides it; the `root.after` pump keeps draining
   the `GuiThread` queue.

`GuiThread` stays the public surface. On macOS it never spawns a thread:
`adopt_main_thread()` designates the main thread as the GUI thread and
`run_mainloop()` installs the pump and blocks, which is what `run()` enters
instead of `icon.run()`. `call`/`submit` are unchanged. The keyboard listener
still never touches Tk.

Why this is the bet: it is the only option where each framework runs on the
thread it demands **and** the two share the one `NSApplication` the platform
allows, using an integration seam pystray ships specifically for it. No second
event loop, no off-main-thread Tk.

Risks that were open before the Mac run, and how they closed:
- **Call signature — the design guess was wrong in a way that matters.**
  `darwin_nsapplication` is an **`Icon` constructor** kwarg, not a
  `run_detached` one: `run_detached(self, setup=None)` is the whole signature,
  and `_base.Icon.__init__` is what harvests `darwin_`-prefixed kwargs into
  `self._options`. The darwin `Icon.__init__` reads `nsapplication` there *and
  creates the `NSStatusItem` right then*, so the icon must also be constructed
  on the main thread. Hence `platform_support.tray_icon_options()` feeds the
  constructor, not the call.
- The `setup=` callback does fire on a separate thread (observed:
  `Thread-1 (setup_handler)`). `on_tray_ready` only sets `icon.visible` and
  spawns background tasks, so nothing there touches Tk.
- `AppKit` is present transitively via pystray's darwin backend
  (`pyobjc-framework-Cocoa`); no new top-level dependency, and
  `requirements.txt` is unchanged.
- `icon.stop()` in detached mode only calls `NSApp stop:` — the darwin backend
  removes its status item in `_run`'s `finally`, which detached mode never
  enters. Harmless: `quit_app` tears the root down and the process exits.

### Option 2 — keep `icon.run()` on main, keep `GuiThread` off-main (LIKELY FAILS, cheap to test)

Change nothing but the platform seam: let pystray block the main thread as on
Windows and leave the Tk root on its worker thread. This is attractive only
because it is zero architectural change. It puts Tk off the main thread on
macOS, which is precisely the unsupported configuration. Expected outcome:
crashes or beachballs on the first dialog. Worth a 15-minute empirical check on
the Mac purely to confirm the failure and rule it out — do not build on it.

### Option 3 — macOS-native tray backend behind a `platform_support` seam (does not solve the core problem)

Swap pystray for a native menubar library (e.g. `rumps`) on macOS, kept behind a
seam so Windows/Linux stay on pystray. This does **not** dissolve the collision:
`rumps` also runs `NSApplication` on the main thread and still has to share it
with Aqua Tk — the same one-NSApplication problem, minus pystray's ready-made
`darwin_nsapplication` integration hook. It also adds a runtime dependency, which
per repo rules needs explicit approval, and it forks the tray/menu/autostart
surface across two libraries. Reserve as a fallback only if Option 1 proves that
pystray's darwin detached mode is broken on the target macOS/pystray combo.

## What shipped

Option 1, behind three seams — no `sys.platform` checks were scattered:

- `platform_support.tk_runs_on_main_thread()` — the single predicate. True only
  on macOS.
- `platform_support.tray_icon_options()` — the extra `pystray.Icon` kwargs per
  OS: `{}` on Windows, `{"darwin_nsapplication": <shared NSApplication>}` on
  macOS. **Must be called after the Tk root exists**, because
  `sharedApplication()` creates a bare `NSApplication` when none is around yet
  and pystray would then integrate with a loop nobody runs. There is a test
  pinning that order.
- `GuiThread` main-thread mode — `adopt_main_thread()` designates the calling
  (main) thread as the GUI thread and creates the root there; `run_mainloop()`
  installs the pump and blocks. `call`/`submit` are untouched: they already
  compared against `self._thread`, which is simply `threading.main_thread()`
  now. `ensure_started()` in this mode **refuses** from a worker rather than
  spawning a thread — silently spawning one is the exact configuration that
  aborts the process on macOS, so it fails loudly instead.

`Sniptype.run()` branches once, at the end:

```python
if platform_support.tk_runs_on_main_thread():
    self.icon.run_detached(setup=self.on_tray_ready)
    self.gui.run_mainloop()          # blocks, as icon.run() does on Windows
else:
    self.icon.run(setup=self.on_tray_ready)
```

Two smaller consequences worth knowing:

- **Tray menu callbacks run on the main thread on macOS**, dispatched from
  inside the loop `mainloop()` is pumping — so they are already *on* the GUI
  thread, and `call`/`submit` run them inline instead of queueing (which would
  deadlock against a pump they are blocking).
- Because of that, `GuiThread.stop()` called from the GUI thread itself (which
  is what `quit_app` does on macOS) schedules the `destroy()` on the next tick
  rather than running it inline: tearing the Tcl interpreter down in the middle
  of a Cocoa menu dispatch is not safe.

## What was measured

macOS 15 (Darwin 25.5.0), Python 3.14.6, Tk 9.0.3, pystray 0.19.5,
pyobjc-framework-Cocoa 12.2.1. Three prototypes, escalating:

1. **Framework isolation.** `tk.Tk()` on main → `sharedApplication()` →
   `Icon(..., darwin_nsapplication=app).run_detached()` → `root.mainloop()`.
   The shared application is Tk's own `TKApplication` subclass and pystray
   attaches to it (`icon._app is app`). Status item created, visible, menu
   attached; a `Toplevel` opened from the `after` pump mapped; clean exit.
2. **Real `GuiThread`, real `Icon`.** Worker `call` returned a dialog's value
   (`PETR4`) after blocking ~520 ms while the pump ran the dialog; a raising
   `call` re-raised `boom` in the caller; `submit` ran on the main thread;
   tray visible with a menu; `stop()` from a worker ended the loop; exit 0.
3. **The real app.** `Sniptype.run()` with `SNIPTYPE_HOME` pointed at a
   scratch dir: tray icon visible with its menu, "Gerenciar Snippets" opened
   the manager `Toplevel` through the real tray action, an expansion-style
   dialog was shown and answered through `GuiThread.call`, and
   `quit_app(icon, None)` unwound the loop with exit 0. No crash, no beachball.

Against the issue's acceptance criteria:

- [x] Tray icon renders in the menu bar with a working menu.
- [x] "Gerenciar Snippets" opens the manager window (a Tk `Toplevel`).
- [x] An expansion dialog shows and returns a value to the worker thread via
      `GuiThread.call`.
- [x] No crashes, no beachballs across the above.
- [x] `GuiThread.call`/`submit` semantics unchanged (blocking result + re-raise;
      fire-and-forget), now covered by `MainThreadModeTkTests` against a real
      root — that class runs on any host with a display, macOS included.
- [ ] **Keyboard listener expanding a trigger is NOT verified.** pynput reports
      *"This process is not trusted! Input event monitoring will not be possible
      until it is added to accessibility clients."* — the listener needs macOS
      Accessibility permission, which is a separate concern from the threading
      model and belongs to the keyboard/permissions issue, not this one. What
      this issue owns is verified: the listener thread starts and never touches
      Tk.
- [ ] **Windows regression run is outstanding** — no Windows host was available.
      The full suite is green on macOS (**884 tests, OK, 50 skipped**), and the
      Windows branch is covered there by `test_tray_startup` and by every
      `GuiThread` test pinning `main_thread=False`, but the suite has not been
      executed on Windows since this landed.

### Test-harness hazard found along the way

On macOS Tk 9.0.3, creating a root and destroying it *outside any mainloop*
leaves the Aqua interpreter in a state where a **later** root destroyed from
inside an `after` callback traps the process (SIGTRAP, no Python traceback —
`faulthandler` does not catch it). The app never does this (one root, one
lifetime), but a test module that probes Tk availability by building a
throwaway root does. `test_gui_thread` therefore probes **out of process**.
Worth remembering before adding another Tk probe anywhere in the suite.

## Options 2 and 3

Not pursued: Option 1 worked on the first prototype. Option 2 (off-main Tk on
macOS) stays ruled out on framework grounds — `test_manager_gui_smoke` and the
worker-mode `GuiThread` tests still skip on macOS for exactly that reason.
Option 3 (`rumps`) remains the fallback only if a future pystray/macOS
combination breaks detached mode; it would add a runtime dependency and fork
the tray surface, and it does not dissolve the one-`NSApplication` collision.

## References

- `source/gui_thread.py` — the pump + marshaling contract to preserve.
- `source/platform_support.py` — where the OS seam belongs.
- `source/sniptype.pyw` — `Sniptype.run()` (tray + listener startup) and
  `main()`.
- pystray 0.19.5 `Icon.run_detached` docstring — the `darwin_nsapplication`
  integration contract this design rests on.
