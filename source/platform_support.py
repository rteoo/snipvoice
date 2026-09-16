"""Cross-platform seam for the OS-specific couplings.

Windows is the only fully-implemented backend today; this module centralizes the
platform decisions (paste modifier, single-instance strategy, autostart install)
so adding a macOS/Linux backend is additive and Windows behavior stays unchanged.

Pure helpers here are unit-tested; the clipboard backends live in
``clipboard_support`` (selected from :func:`current_os`) and the single-instance
mutex remains in ``snipvoice`` (see README "Cross-platform status").
"""

import base64
import ctypes
import ntpath
import os
import re
import shlex
import subprocess
import sys
import threading
import time

try:
    import fcntl as _fcntl
except ImportError:  # Windows uses the named mutex in the real app.
    _fcntl = None


APP_NAME = "Snipvoice"
APPLICATION_ACTIVATION_TIMEOUT_SECONDS = 2.0
_LOCKFILE_HANDLES = {}
_LOCKFILE_HANDLES_LOCK = threading.Lock()
_EMPTY_LOCK_STALE_SECONDS = 1.0
_FALLBACK_OWNER_NAME = "owner.pid"
_FALLBACK_RECLAIM_NAME = ".reclaim"


def current_os():
    """Return 'windows', 'darwin' (macOS) or 'linux'."""
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


IS_WINDOWS = current_os() == "windows"
IS_MAC = current_os() == "darwin"
IS_LINUX = current_os() == "linux"


def paste_modifier_is_cmd():
    """True where the paste shortcut is Cmd+V (macOS) rather than Ctrl+V."""
    return IS_MAC


# ---------------------------------------------------------------------------
# Insertion timings (clipboard paste and trigger erase)
# ---------------------------------------------------------------------------

# The paste path is snapshot -> set -> settle -> Cmd/Ctrl+V -> restore, and the
# trigger erase types one backspace per character. All three delays were tuned
# on Windows; macOS needs its own defaults because every clipboard operation is
# a subprocess round-trip there rather than an in-process ctypes call.
# See ``source/docs/macos-insertion.md`` for the measurements behind the values.
_INSERTION_TIMING_DEFAULTS = {
    "windows": {
        "clipboard_settle_delay": 0.05,
        "paste_restore_delay": 0.12,
        "erase_key_delay": 0.01,
    },
    "darwin": {
        # ``pbcopy``/``osascript`` only exit once NSPasteboard holds the payload,
        # and the write itself already costs ~12 ms, so the Windows settle margin
        # is pure added latency here (measured: readable with zero extra delay).
        "clipboard_settle_delay": 0.02,
        "paste_restore_delay": 0.12,
        "erase_key_delay": 0.01,
    },
    "linux": {
        # ``xclip``/``wl-copy`` fork a daemon that owns the selection, so the
        # payload is not necessarily servable the instant the tool returns.
        "clipboard_settle_delay": 0.05,
        "paste_restore_delay": 0.12,
        "erase_key_delay": 0.01,
    },
}

INSERTION_TIMING_KEYS = tuple(_INSERTION_TIMING_DEFAULTS["windows"])

# A delay is a human-scale pause, not a schedule. Anything past this is a typo
# (seconds written as milliseconds) that would freeze the listener thread.
_MAX_INSERTION_DELAY = 2.0


def default_insertion_timings(system=None):
    """Return this OS's insertion delays, in seconds."""
    defaults = _INSERTION_TIMING_DEFAULTS.get(system or current_os())
    return dict(defaults or _INSERTION_TIMING_DEFAULTS["linux"])


def _valid_delay(value):
    # bool is an int; a `true` in settings.json must not become a 1 second delay.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return 0 <= value <= _MAX_INSERTION_DELAY


def insertion_timings(settings=None, system=None):
    """Resolve the insertion delays, applying valid ``settings.json`` overrides.

    Out-of-range or non-numeric overrides fall back to the platform default
    instead of propagating into the keyboard hot path; call
    :func:`invalid_timing_overrides` to report them.
    """
    timings = default_insertion_timings(system)
    for key, value in (settings or {}).items():
        if key in timings and _valid_delay(value):
            timings[key] = float(value)
    return timings


def invalid_timing_overrides(settings=None):
    """Return the timing keys present in ``settings`` whose value was rejected."""
    return [
        key
        for key in INSERTION_TIMING_KEYS
        if key in (settings or {}) and not _valid_delay(settings[key])
    ]


def tk_runs_on_main_thread():
    """True where the Tk root must live on the main thread rather than a worker.

    macOS only. Aqua Tk *is* a Cocoa app — ``tk.Tk()`` instantiates the process's
    shared ``NSApplication`` — and AppKit only drives its event loop on thread 0,
    so the worker-thread root that Windows tolerates crashes or misrenders there.
    ``GuiThread`` reads this to decide whether ``ensure_started`` spawns a thread
    or adopts the main one; the ``call``/``submit`` contract is identical either
    way. See ``source/docs/macos-threading.md``.
    """
    return IS_MAC


def tray_menu_updates_on_gui_thread():
    """True where rebuilding the tray menu must happen on the GUI (main) thread.

    macOS only. pystray's darwin backend builds the ``NSMenu`` and calls
    ``setMenu_`` directly on whatever thread invokes ``update_menu``, with no
    main-thread hop of its own; AppKit may only be mutated from the main thread,
    which is the thread the ``GuiThread`` pump runs on there. A background
    (task-runner) caller must therefore route the update through the pump. The
    win32 backend posts an internal message and is safe from any thread, so
    Windows stays a direct call. See ``source/docs/macos-threading.md``.
    """
    return IS_MAC


def tray_icon_options():
    """Return the extra ``pystray.Icon`` kwargs this OS needs. Empty on Windows.

    On macOS the tray cannot start its own ``NSApplication``: there is one per
    process (``+sharedApplication``) and Tk already created it. Handing pystray
    that instance as ``darwin_nsapplication`` makes it attach its status item to
    the loop ``root.mainloop()`` is driving, which is what allows
    ``icon.run_detached()`` instead of a second, impossible, main loop.

    **Call this only after the Tk root exists** — ``sharedApplication()`` creates
    a plain ``NSApplication`` when none is around yet, and pystray would then be
    integrating with a loop nobody runs.
    """
    if not IS_MAC:
        return {}
    import AppKit  # transitive dependency of pystray's darwin backend

    return {"darwin_nsapplication": AppKit.NSApplication.sharedApplication()}


def hide_dock_icon():
    """Drop the Dock icon on macOS. **Call after the Tk root exists.**

    ``LSUIElement`` in the bundle's Info.plist is not enough: Aqua Tk sets
    ``NSApplicationActivationPolicyRegular`` on the shared ``NSApplication``
    while it initializes, and a runtime policy overrides the plist — so the
    packaged ``.app`` shows a Dock icon and an app menu despite being built
    menu-bar-only. Putting the policy back to *accessory* after Tk has had its
    say is what makes the plist key stick, and it is equally what gives a
    source checkout the menu-bar-only behavior the bundle has.

    Accessory keeps windows usable: a Toplevel still maps, activates and takes
    keyboard focus through the existing ``lift()``/``focus_force()`` calls.

    Returns True when the policy was applied, False off macOS or if AppKit
    refused it — never raises, because a Dock icon is a cosmetic failure and
    must not take the tray down with it.
    """
    if not IS_MAC:
        return False
    try:
        import AppKit

        app = AppKit.NSApplication.sharedApplication()
        return bool(
            app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
        )
    except Exception:
        return False


def capture_frontmost_application():
    """Return the external macOS app active before an expansion dialog opens.

    Tk activates Snipvoice when a modal expansion dialog takes focus. The
    generated Cmd+V must go back to the editor that owned focus before that
    dialog, not to Snipvoice's hidden root. Call this on the macOS GUI/main
    thread immediately before building the dialog.
    """
    if not IS_MAC:
        return None
    try:
        import AppKit

        app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None or int(app.processIdentifier()) == os.getpid():
            return None
        return app
    except Exception:
        return None


def restore_frontmost_application(app):
    """Reactivate a macOS app captured by :func:`capture_frontmost_application`.

    Activation failure is non-fatal: callers retain the historical focus
    behavior and the expansion path can still leave its payload on the
    clipboard. Call this on the macOS GUI/main thread after the dialog closes.
    """
    if not IS_MAC or app is None:
        return False
    try:
        import AppKit

        return bool(
            app.activateWithOptions_(
                AppKit.NSApplicationActivateIgnoringOtherApps
            )
        )
    except Exception:
        return False


def _win32_user32():
    """Return the Win32 user32 DLL. Isolated so tests never patch ctypes.windll."""
    return ctypes.windll.user32


def capture_text_target():
    """Foreground app/window that should receive a voice insertion.

    macOS reuses the AppKit application handle already used by expansion
    dialogs. Windows stores the foreground HWND. The caller must invoke this
    on hotkey press, before any Snipvoice UI can take focus. Returns None
    when the foreground owner is this process or cannot be read.
    """
    if IS_MAC:
        return capture_frontmost_application()
    if not IS_WINDOWS:
        return None
    try:
        user32 = _win32_user32()
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = ctypes.c_ulong(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if int(pid.value) == os.getpid():
            return None
        return ("hwnd", int(hwnd))
    except Exception:
        return None


def wait_for_restored_application(
    app,
    timeout_seconds=APPLICATION_ACTIVATION_TIMEOUT_SECONDS,
):
    """Block until a captured macOS app is frontmost, or fail.

    Must run off the Tk/AppKit main thread. Uses the same
    ``NSWorkspaceDidActivateApplicationNotification`` barrier as expansion
    dialogs. ``restore_frontmost_application`` only submits activation and
    must not be used on the insertion path.
    """
    if not IS_MAC or app is None:
        return False
    ready = threading.Event()
    errors = []

    def failed(message):
        errors.append(message)
        ready.set()

    cancel = restore_application_when_ready(app, ready.set, failed)
    try:
        if not ready.wait(timeout_seconds):
            return False
        return not errors
    finally:
        cancel()


def restore_text_target(target):
    """Bring a captured text target to the foreground. False if it is gone."""
    if target is None:
        return False
    if IS_MAC:
        return wait_for_restored_application(target)
    if not IS_WINDOWS:
        return False
    if not (isinstance(target, tuple) and len(target) == 2 and target[0] == "hwnd"):
        return False
    try:
        user32 = _win32_user32()
        hwnd = int(target[1])
        if not user32.IsWindow(hwnd):
            return False
        return bool(user32.SetForegroundWindow(hwnd))
    except Exception:
        return False


def text_target_is_alive(target):
    """True when the captured target still exists."""
    if target is None:
        return False
    if IS_MAC:
        try:
            return int(target.processIdentifier()) > 0
        except Exception:
            return False
    if IS_WINDOWS and isinstance(target, tuple) and target[0] == "hwnd":
        try:
            return bool(_win32_user32().IsWindow(int(target[1])))
        except Exception:
            return False
    return False


def activate_application_when_ready(
    on_active,
    on_failed,
    timeout_seconds=APPLICATION_ACTIVATION_TIMEOUT_SECONDS,
):
    """Request macOS activation and call ``on_active`` after Cocoa confirms it.

    Activation of an accessory application is asynchronous. Mapping a Tk
    Toplevel and immediately calling ``focus_force`` can therefore paint a
    focused entry while physical keys still go to the previous app. The native
    notification is the readiness barrier.

    Both callbacks may run from Cocoa or a timer thread and must not call Tcl/Tk
    directly; callers queue Tk work through ``GuiThread.submit``. The returned
    callable cancels the observer and timeout and is safe to call more than
    once.
    """
    if not IS_MAC:
        on_active()
        return lambda: None

    state = {
        "cancelled": False,
        "done": False,
        "token": None,
        "timer": None,
    }
    lock = threading.Lock()
    center = None

    def finish(callback, *args):
        with lock:
            if state["cancelled"] or state["done"]:
                return
            state["done"] = True
            token = state["token"]
            timer = state["timer"]
            state["token"] = None
            state["timer"] = None
        if center is not None and token is not None:
            center.removeObserver_(token)
        if timer is not None:
            timer.cancel()
        callback(*args)

    def cancel():
        with lock:
            if state["cancelled"]:
                return
            state["cancelled"] = True
            token = state["token"]
            timer = state["timer"]
            state["token"] = None
            state["timer"] = None
        if center is not None and token is not None:
            center.removeObserver_(token)
        if timer is not None:
            timer.cancel()

    def complete(_notification=None):
        finish(on_active)

    def fail(message):
        finish(on_failed, message)

    try:
        import AppKit

        app = AppKit.NSApplication.sharedApplication()
        if app.isActive():
            complete()
            return cancel

        center = AppKit.NSNotificationCenter.defaultCenter()
        state["token"] = center.addObserverForName_object_queue_usingBlock_(
            AppKit.NSApplicationDidBecomeActiveNotification,
            app,
            None,
            complete,
        )
        timer = threading.Timer(
            timeout_seconds,
            fail,
            args=("Timed out waiting for Snipvoice to receive keyboard focus",),
        )
        timer.daemon = True
        state["timer"] = timer
        timer.start()
        options = (
            AppKit.NSApplicationActivateAllWindows
            | AppKit.NSApplicationActivateIgnoringOtherApps
        )
        running_app = AppKit.NSRunningApplication.currentApplication()
        activation_accepted = running_app.activateWithOptions_(options)
        if app.isActive():
            complete()
        elif not activation_accepted:
            fail("macOS refused to activate Snipvoice")
        return cancel
    except Exception as exc:
        fail(f"Could not activate Snipvoice: {exc}")
        return cancel


def restore_application_when_ready(app, on_active, on_failed):
    """Activate a captured macOS application and confirm it became frontmost.

    The callbacks must not touch Tcl/Tk. The expansion worker supplies a
    ``threading.Event`` callback and performs its bounded wait off the GUI
    thread before it is allowed to synthesize Cmd+V.
    """
    if not IS_MAC or app is None:
        on_active()
        return lambda: None

    state = {"cancelled": False, "done": False, "token": None}
    lock = threading.Lock()
    center = None

    def finish(callback, *args):
        with lock:
            if state["cancelled"] or state["done"]:
                return
            state["done"] = True
            token = state["token"]
            state["token"] = None
        if center is not None and token is not None:
            center.removeObserver_(token)
        callback(*args)

    def cancel():
        with lock:
            if state["cancelled"]:
                return
            state["cancelled"] = True
            token = state["token"]
            state["token"] = None
        if center is not None and token is not None:
            center.removeObserver_(token)

    def complete():
        finish(on_active)

    def fail(message):
        finish(on_failed, message)

    try:
        import AppKit

        target_pid = int(app.processIdentifier())
        workspace = AppKit.NSWorkspace.sharedWorkspace()

        def target_is_frontmost():
            frontmost = workspace.frontmostApplication()
            return (
                frontmost is not None
                and int(frontmost.processIdentifier()) == target_pid
            )

        if target_is_frontmost():
            complete()
            return cancel

        center = workspace.notificationCenter()

        def activated(notification):
            user_info = notification.userInfo() or {}
            activated_app = user_info.get(AppKit.NSWorkspaceApplicationKey)
            if (
                activated_app is not None
                and int(activated_app.processIdentifier()) == target_pid
            ):
                complete()

        state["token"] = center.addObserverForName_object_queue_usingBlock_(
            AppKit.NSWorkspaceDidActivateApplicationNotification,
            None,
            None,
            activated,
        )
        accepted = app.activateWithOptions_(
            AppKit.NSApplicationActivateIgnoringOtherApps
        )
        if target_is_frontmost():
            complete()
        elif not accepted:
            fail("macOS refused to return focus to the previous application")
        return cancel
    except Exception as exc:
        fail(f"Could not return focus to the previous application: {exc}")
        return cancel


def focus_tk_window_when_ready(
    dialog,
    focus_target,
    on_key,
    on_failed,
    timeout_seconds=APPLICATION_ACTIVATION_TIMEOUT_SECONDS,
):
    """Focus a mapped Tk dialog and confirm its exact ``NSWindow`` became key.

    Application activation alone is insufficient: Aqua can map a Toplevel and
    paint its entry as focused while the native ``TKWindow`` still routes
    physical keys elsewhere. Tk's exported drawable bridge identifies the
    exact ``NSWindow`` without title matching. The native observer never calls
    Tcl/Tk; callers queue their visible reveal through ``GuiThread.submit``.

    This function itself must be called from the Tk pump because ``winfo_id``
    and ``focus_force`` are Tk operations.
    """
    if not IS_MAC:
        focus_target.focus_force()
        on_key()
        return lambda: None

    state = {
        "cancelled": False,
        "done": False,
        "token": None,
        "timer": None,
    }
    lock = threading.Lock()
    center = None

    def finish(callback, *args):
        with lock:
            if state["cancelled"] or state["done"]:
                return
            state["done"] = True
            token = state["token"]
            timer = state["timer"]
            state["token"] = None
            state["timer"] = None
        if center is not None and token is not None:
            center.removeObserver_(token)
        if timer is not None:
            timer.cancel()
        callback(*args)

    def cancel():
        with lock:
            if state["cancelled"]:
                return
            state["cancelled"] = True
            token = state["token"]
            timer = state["timer"]
            state["token"] = None
            state["timer"] = None
        if center is not None and token is not None:
            center.removeObserver_(token)
        if timer is not None:
            timer.cancel()

    def complete(_notification=None):
        finish(on_key)

    def fail(message):
        finish(on_failed, message)

    try:
        import AppKit
        import objc

        get_nswindow = ctypes.CDLL(None).Tk_MacOSXGetNSWindowForDrawable
        get_nswindow.argtypes = [ctypes.c_void_p]
        get_nswindow.restype = ctypes.c_void_p
        pointer = get_nswindow(dialog.winfo_id())
        if not pointer:
            fail("Tk did not expose a native window for the input dialog")
            return cancel

        native_window = objc.objc_object(c_void_p=pointer)
        if not native_window.canBecomeKeyWindow():
            fail("The input dialog cannot become the macOS key window")
            return cancel

        center = AppKit.NSNotificationCenter.defaultCenter()
        state["token"] = center.addObserverForName_object_queue_usingBlock_(
            AppKit.NSWindowDidBecomeKeyNotification,
            native_window,
            None,
            complete,
        )
        timer = threading.Timer(
            timeout_seconds,
            fail,
            args=("Timed out waiting for the input dialog to receive keyboard focus",),
        )
        timer.daemon = True
        state["timer"] = timer
        timer.start()

        # Register before both checks and the focus request so neither a mapped
        # key window nor a synchronous key transition can be missed.
        if native_window.isKeyWindow():
            complete()
        else:
            focus_target.focus_force()
            if native_window.isKeyWindow():
                complete()
        return cancel
    except Exception as exc:
        fail(f"Could not focus the input dialog: {exc}")
        return cancel


def pin_tray_backend(environ=None):
    """Pin pystray to the win32 backend on Windows. Return the value set, or None.

    Belt-and-braces only: pystray already resolves a single ``win32`` candidate
    when ``sys.platform == 'win32'``, and the frozen build ships that backend as
    the spec's hidden import. Off Windows the same pin is fatal — it forces a
    backend that cannot import — so it must not be set there. Must run *before*
    ``import pystray``, which reads the variable at import time.
    """
    env = os.environ if environ is None else environ
    if current_os() != "windows":
        return None
    return env.setdefault("PYSTRAY_BACKEND", "win32")


# ---------------------------------------------------------------------------
# Single instance via PID lockfile (used where a native mutex is unavailable)
# ---------------------------------------------------------------------------

def _pid_is_running(pid):
    if pid <= 0:
        return False

    if IS_WINDOWS:
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return exit_code.value == STILL_ACTIVE
            return True
        finally:
            kernel32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    return True


def acquire_lockfile(path):
    """Acquire a PID lockfile. Return True if acquired, False if already held.

    A stale lock (whose PID is no longer running) is reclaimed. This is the
    portable single-instance strategy for non-Windows backends; Windows keeps its
    named mutex.
    """
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    except OSError:
        # If we cannot manage the lockfile, do not block startup.
        return True

    pid = os.getpid()
    absolute_path = os.path.abspath(path)

    # macOS/Linux have an advisory kernel lock. It is the actual ownership
    # primitive: it is acquired atomically, survives PID-file rewrites, and is
    # released automatically if the process crashes. The file itself may remain
    # after exit; its PID is diagnostic only and is overwritten by the next owner.
    if _fcntl is not None:
        with _LOCKFILE_HANDLES_LOCK:
            existing = _LOCKFILE_HANDLES.get(absolute_path)
            if existing is not None:
                return existing[0] == pid
            try:
                descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
            except OSError:
                return True
            try:
                _fcntl.flock(
                    descriptor,
                    _fcntl.LOCK_EX | _fcntl.LOCK_NB,
                )
            except (BlockingIOError, OSError):
                os.close(descriptor)
                return False
            try:
                os.ftruncate(descriptor, 0)
                os.write(descriptor, str(pid).encode("ascii"))
                os.fsync(descriptor)
            except OSError:
                # The kernel lock still guarantees exclusivity. Keep it rather
                # than fail open and admit a second global listener.
                pass
            _LOCKFILE_HANDLES[absolute_path] = (pid, descriptor)
            return True

    # Test-only/defensive fallback for hosts without fcntl. A directory is the
    # ownership primitive because mkdir is atomic. Stale cleanup first claims a
    # marker inside that directory and renames the exact claimed directory out
    # of the way, so it cannot unlink a replacement created by another process.
    for _ in range(5):
        try:
            os.mkdir(path, 0o755)
        except FileExistsError:
            if not os.path.isdir(path):
                # Fail closed for a legacy file lock. Production POSIX hosts
                # use fcntl, and deleting a file-format lock safely would need
                # a compare-and-unlink primitive the standard library lacks.
                return False

            owner_path = os.path.join(path, _FALLBACK_OWNER_NAME)
            try:
                with open(owner_path, "r", encoding="utf-8") as handle:
                    raw_pid = handle.read().strip()
            except FileNotFoundError:
                raw_pid = ""
            except OSError:
                return False

            # A newly-created lock can be observed between mkdir() and the
            # owner's PID write. Fail closed during that short window, but
            # reclaim an old empty directory left by a creator that crashed before
            # publishing its PID.
            if not raw_pid:
                try:
                    age = max(0.0, time.time() - os.path.getmtime(path))
                except OSError:
                    continue
                if age < _EMPTY_LOCK_STALE_SECONDS:
                    return False
            try:
                existing_pid = int(raw_pid)
            except ValueError:
                existing_pid = 0

            if existing_pid == pid:
                return True
            if existing_pid > 0 and _pid_is_running(existing_pid):
                return False

            reclaim_path = os.path.join(path, _FALLBACK_RECLAIM_NAME)
            try:
                reclaim_descriptor = os.open(
                    reclaim_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o644,
                )
            except FileExistsError:
                return False
            except OSError:
                return False
            try:
                os.write(reclaim_descriptor, str(pid).encode("ascii"))
            finally:
                os.close(reclaim_descriptor)

            # The original creator may have published its PID while this
            # contender was deciding that the empty directory was stale.
            try:
                with open(owner_path, "r", encoding="utf-8") as handle:
                    claimed_pid = int((handle.read().strip() or "0"))
            except (FileNotFoundError, ValueError):
                claimed_pid = 0
            except OSError:
                claimed_pid = -1
            if claimed_pid > 0 and _pid_is_running(claimed_pid):
                try:
                    os.remove(reclaim_path)
                except OSError:
                    pass
                return False

            stale_path = (
                f"{path}.stale-{pid}-{threading.get_ident()}-{time.time_ns()}"
            )
            try:
                os.rename(path, stale_path)
            except FileNotFoundError:
                continue
            except OSError:
                try:
                    os.remove(reclaim_path)
                except OSError:
                    pass
                return False
            for stale_name in (_FALLBACK_OWNER_NAME, _FALLBACK_RECLAIM_NAME):
                try:
                    os.remove(os.path.join(stale_path, stale_name))
                except FileNotFoundError:
                    pass
                except OSError:
                    return False
            try:
                os.rmdir(stale_path)
            except OSError:
                return False
            continue
        except OSError:
            # Preserve the historical fail-open behavior for an unreadable data
            # directory or another filesystem error unrelated to contention.
            return True

        owner_path = os.path.join(path, _FALLBACK_OWNER_NAME)
        try:
            with open(owner_path, "x", encoding="ascii") as handle:
                handle.write(str(pid))
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            try:
                os.rmdir(path)
            except OSError:
                pass
            return True
        return True

    return False


def release_lockfile(path):
    """Remove our lockfile if it still holds our PID. Best effort."""
    if _fcntl is not None:
        absolute_path = os.path.abspath(path)
        with _LOCKFILE_HANDLES_LOCK:
            existing = _LOCKFILE_HANDLES.get(absolute_path)
            if existing is None or existing[0] != os.getpid():
                return
            _owner_pid, descriptor = _LOCKFILE_HANDLES.pop(absolute_path)
        try:
            _fcntl.flock(descriptor, _fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(descriptor)
        except OSError:
            pass
        return

    if os.path.isdir(path):
        owner_path = os.path.join(path, _FALLBACK_OWNER_NAME)
        try:
            with open(owner_path, "r", encoding="utf-8") as handle:
                if int((handle.read().strip() or "0")) != os.getpid():
                    return
            os.remove(owner_path)
            os.rmdir(path)
        except (ValueError, OSError):
            pass
        return

    try:
        with open(path, "r", encoding="utf-8") as handle:
            if int((handle.read().strip() or "0")) != os.getpid():
                return
        os.remove(path)
    except (ValueError, OSError):
        pass


# ---------------------------------------------------------------------------
# Autostart install (per-OS location and payload)
# ---------------------------------------------------------------------------

def autostart_target_path(app_name=APP_NAME):
    """Return the per-OS path where the autostart entry lives."""
    home = os.path.expanduser("~")
    system = current_os()
    if system == "windows":
        return os.path.join(
            os.environ.get("APPDATA", os.path.join(home, "AppData", "Roaming")),
            "Microsoft", "Windows", "Start Menu", "Programs", "Startup",
            f"{app_name}.lnk",
        )
    if system == "darwin":
        return os.path.join(home, "Library", "LaunchAgents", f"com.{_slug(app_name)}.plist")
    return os.path.join(home, ".config", "autostart", f"{_slug(app_name)}.desktop")


def _slug(name):
    return "".join(ch.lower() if ch.isalnum() else "-" for ch in name).strip("-")


def macos_launch_agent(app_name, program_path, arguments=None):
    """Return the LaunchAgent plist XML for macOS autostart."""
    label = f"com.{_slug(app_name)}"
    argv = [program_path] + list(arguments or [])
    argv_xml = "".join(f"<string>{_xml_escape(arg)}</string>" for arg in argv)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        f"  <key>Label</key><string>{label}</string>\n"
        "  <key>ProgramArguments</key>\n"
        f"  <array>{argv_xml}</array>\n"
        "  <key>RunAtLoad</key><true/>\n"
        "</dict>\n"
        "</plist>\n"
    )


def _xml_escape(value):
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def linux_desktop_entry(app_name, program_command):
    """Return the .desktop file content for Linux autostart."""
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={app_name}\n"
        f"Exec={program_command}\n"
        "X-GNOME-Autostart-enabled=true\n"
    )


def default_autostart_command():
    """Return the argv that starts this app: packaged exe, or the source launcher.

    Mirrors ``build_release.bat``: the frozen build points straight at the
    executable, a source checkout at ``pythonw snipvoice.pyw``.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable]

    launcher = os.path.join(os.path.dirname(os.path.abspath(__file__)), "snipvoice.pyw")
    interpreter = sys.executable
    if current_os() == "windows":
        windowless = os.path.join(os.path.dirname(interpreter), "pythonw.exe")
        if os.path.exists(windowless):
            interpreter = windowless
    return [interpreter, launcher]


AUTOSTART_ABSENT = "absent"
AUTOSTART_CURRENT = "current"
AUTOSTART_STALE = "stale"


def read_autostart_command(app_name=APP_NAME):
    """Return the argv the existing autostart entry launches, or None if absent.

    macOS and Linux are plain file reads; Windows needs WScript.Shell to open the
    ``.lnk``, which is a PowerShell round-trip — never call this from a tray menu
    callback (see :func:`autostart_state`).
    """
    path = autostart_target_path(app_name)
    if not os.path.exists(path):
        return None

    system = current_os()
    if system == "windows":
        return _read_windows_shortcut(path)
    if system == "darwin":
        return _read_macos_launch_agent(path)
    return _read_linux_desktop_entry(path)


def classify_autostart(existing, command=None):
    """Classify an entry's argv as absent, current or stale. Pure; no I/O but a stat.

    Presence alone means nothing: three writers own the same Startup entry (the
    tray toggle, ``build_release.bat`` and the Inno Setup task), so an entry left
    by a deleted ``dist`` copy or by a different install would otherwise report as
    enabled while login starts nothing — or starts the wrong copy.

    ``existing`` is what :func:`read_autostart_command` returned (``None`` when
    there is no entry).
    """
    if existing is None:
        return AUTOSTART_ABSENT
    if not autostart_target_exists(existing):
        return AUTOSTART_STALE
    expected = list(command or default_autostart_command())
    return AUTOSTART_CURRENT if _same_command(existing, expected) else AUTOSTART_STALE


def autostart_target_exists(existing):
    """True when everything the entry points at is still on disk.

    Separates the two kinds of stale: a dead pointer (deleted ``dist`` folder,
    removed venv or checkout) that nobody meant to keep, versus a live entry
    owned by another install of the app. Only the first is safe to rewrite
    unasked. Every argv element we ever write is a path (the exe, or
    interpreter + script), so one missing element makes the entry dead at
    login — a surviving interpreter does not keep it alive.
    """
    return bool(existing) and all(os.path.exists(arg) for arg in existing)


def autostart_state(app_name=APP_NAME, command=None):
    """Read the autostart entry and classify it. Worker-thread only on Windows.

    An entry we cannot read counts as stale: it cannot be shown as enabled when
    we were unable to confirm what it launches.
    """
    try:
        existing = read_autostart_command(app_name)
    except OSError:
        return AUTOSTART_STALE
    return classify_autostart(existing, command)


def is_autostart_enabled(app_name=APP_NAME):
    """True when the autostart entry exists *and* points at this install."""
    return autostart_state(app_name) == AUTOSTART_CURRENT


def _same_command(left, right):
    if len(left) != len(right):
        return False
    return all(_same_path(a, b) for a, b in zip(left, right))


def _same_path(left, right):
    # Every argv element we write is a path; Windows paths are case-insensitive
    # and tolerate mixed separators, so compare them normalized. ntpath rather
    # than os.path: the two are the same module on Windows, but this branch
    # applies Windows path rules and must do so wherever it is evaluated.
    if current_os() == "windows":
        return ntpath.normcase(ntpath.normpath(left)) == ntpath.normcase(ntpath.normpath(right))
    return left == right


def _read_windows_shortcut(path):
    script = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$sc = $ws.CreateShortcut({_ps_quote(path)}); "
        "Write-Output $sc.TargetPath; "
        "Write-Output $sc.Arguments"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        creationflags=0x08000000,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise OSError(f"Falha ao ler o atalho de inicialização: {detail}")

    lines = (result.stdout or "").splitlines()
    target = lines[0].strip() if lines else ""
    arguments = lines[1].strip() if len(lines) > 1 else ""
    if not target:
        return []
    return [target] + _split_windows_args(arguments)


def _split_windows_args(arguments):
    if not arguments:
        return []
    return [arg.strip('"') for arg in shlex.split(arguments, posix=False)]


def _read_macos_launch_agent(path):
    with open(path, encoding="utf-8") as handle:
        content = handle.read()
    match = re.search(r"<key>ProgramArguments</key>\s*<array>(.*?)</array>", content, re.DOTALL)
    if not match:
        return []
    return [_xml_unescape(value) for value in re.findall(r"<string>(.*?)</string>", match.group(1), re.DOTALL)]


def _xml_unescape(value):
    return value.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _read_linux_desktop_entry(path):
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("Exec="):
                return shlex.split(line[len("Exec="):].strip())
    return []


def install_autostart(app_name=APP_NAME, command=None):
    """Create the per-user autostart entry and return its path.

    ``command`` is an argv list; it defaults to :func:`default_autostart_command`.
    Raises ``OSError`` when the entry could not be written — callers surface the
    failure instead of reporting a success that did not happen.
    """
    argv = list(command or default_autostart_command())
    if not argv:
        raise OSError("Comando de inicialização automática vazio.")

    path = autostart_target_path(app_name)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    system = current_os()
    if system == "windows":
        _write_windows_shortcut(path, argv)
    elif system == "darwin":
        _write_text_file(path, macos_launch_agent(app_name, argv[0], argv[1:]))
    else:
        _write_text_file(path, linux_desktop_entry(app_name, _join_command(argv)))

    if not os.path.exists(path):
        raise OSError(f"Entrada de inicialização automática não foi criada: {path}")
    return path


def remove_autostart(app_name=APP_NAME):
    """Remove the autostart entry. Return True if one was removed."""
    path = autostart_target_path(app_name)
    if not os.path.exists(path):
        return False
    os.remove(path)
    return True


def _write_text_file(path, content):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


def _join_command(argv):
    if current_os() == "windows":
        return subprocess.list2cmdline(argv)
    return " ".join(shlex.quote(arg) for arg in argv)


def _ps_quote(value):
    value = str(value)
    # Smart quotes are PowerShell delimiters; encode unusual characters so
    # neither delimiters nor newlines can change the generated command.
    if any(ord(char) < 32 or ord(char) > 126 for char in value):
        payload = base64.b64encode(value.encode("utf-8")).decode("ascii")
        return "([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + payload + "')))"
    return "'" + str(value).replace("'", "''") + "'"


def _write_windows_shortcut(path, argv):
    """Create a Startup .lnk via WScript.Shell (same COM object as build_release.bat)."""
    target = argv[0]
    arguments = subprocess.list2cmdline(argv[1:]) if len(argv) > 1 else ""
    # The app dir is where the last argument lives (the exe when frozen, the
    # .pyw launcher from source), not where the interpreter is installed.
    # argv holds Windows paths, so split them with ntpath (== os.path here).
    working_dir = ntpath.dirname(argv[-1])
    if not os.path.isdir(working_dir):
        working_dir = ntpath.dirname(target)
    script = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$sc = $ws.CreateShortcut({_ps_quote(path)}); "
        f"$sc.TargetPath = {_ps_quote(target)}; "
        f"$sc.Arguments = {_ps_quote(arguments)}; "
        f"$sc.WorkingDirectory = {_ps_quote(working_dir)}; "
        f"$sc.IconLocation = {_ps_quote(target + ',0')}; "
        "$sc.Save()"
    )
    # CREATE_NO_WINDOW: the app runs under pythonw, a console flash would be visible.
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        creationflags=0x08000000,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise OSError(f"Falha ao criar o atalho de inicialização: {detail}")
