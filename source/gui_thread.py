"""Single Tk root owned by one dedicated GUI thread.

Tkinter is not thread-safe and a Tcl interpreter belongs to the thread that
created it, so the whole app shares one hidden root created here. Worker
threads never touch Tk directly: they hand a callable to ``call`` (blocking,
returns the result) or ``submit`` (fire-and-forget) and an ``after``-driven
pump runs it on the GUI thread.

Which thread that is depends on the OS. On Windows the root lives on a spawned
worker thread while pystray blocks the main one. On macOS both Aqua Tk and
AppKit demand thread 0, so ``main_thread`` mode designates the main thread as
the GUI thread instead: the caller runs :meth:`adopt_main_thread` then
:meth:`run_mainloop`, and the tray runs detached on the same loop. The
marshaling contract is identical in both modes — only the thread the pump ticks
on changes. See ``source/docs/macos-threading.md``.

On macOS one more rule applies, and breaking it aborts the process rather
than raising: **never call into Tcl/Tk from a Cocoa callback** — a tray menu
action, an NSNotification observer, an NSTimer block. ``_tkinter`` keeps a
single global ``tcl_tstate``, set while Python is inside a Tcl call and cleared
by ``LEAVE_TCL``. A Cocoa callback runs while ``mainloop()`` is already inside
``ENTER_TCL``, so a Tcl call made there clears that global with no
``ENTER_PYTHON`` frame left to restore it, and the next Tcl→Python callback
dies with ``Fatal Python error: PyEval_RestoreThread`` (issue #53). Cocoa
callbacks may therefore only touch Python state — ``submit`` queues, the pump
does the Tk work from inside the Tk loop where the invariant holds.

The keyboard listener thread must never call into this module; dialog
marshaling belongs to the expansion worker path only.
"""

import queue
import threading

import tkinter as tk

from platform_support import tk_runs_on_main_thread


# Pump cadence. Low enough that a dialog feels instant, high enough that an
# idle app is not waking the Tcl interpreter constantly.
PUMP_INTERVAL_MS = 40

_START_TIMEOUT_SECONDS = 10.0


class GuiThread:
    """Owns the process-wide Tk root and marshals work onto its thread."""

    def __init__(self, logger=None, main_thread=None):
        self._logger = logger
        self._queue = queue.Queue()
        self._lock = threading.RLock()
        self._ready = threading.Event()
        self._thread = None
        self._start_error = None
        self._stopping = False
        # Set by the teardown so the pump stops draining into a destroyed root.
        self._destroyed = False
        self._main_thread = (
            tk_runs_on_main_thread() if main_thread is None else bool(main_thread)
        )
        self.root = None

    # -----------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------

    def adopt_main_thread(self):
        """Make the calling thread the GUI thread and create the root there.

        Main-thread mode only (macOS). Does not block: the caller is expected to
        enter :meth:`run_mainloop` once the rest of startup is wired, and queued
        work drains from the moment that loop begins.
        """
        if not self._main_thread:
            raise RuntimeError("adopt_main_thread requires main-thread mode")
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("adopt_main_thread must run on the main thread")

        with self._lock:
            if self._stopping:
                raise RuntimeError("GUI thread is shutting down")
            if self.root is not None:
                return True
            self._start_error = None
            self._thread = threading.current_thread()

        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except Exception:
            # Never leave a thread designated with no root behind it: call()
            # would then run callables inline against ``None``.
            with self._lock:
                self._thread = None
            raise

        self._ready.set()
        return True

    def run_mainloop(self):
        """Block on the Tk loop until the root is destroyed. Main-thread mode only.

        This is what ``run()`` enters on macOS in place of ``icon.run()``.
        """
        if not self._main_thread:
            raise RuntimeError("run_mainloop requires main-thread mode")
        if self.root is None:
            raise RuntimeError("adopt_main_thread must run before run_mainloop")
        if threading.current_thread() is not self._thread:
            raise RuntimeError("run_mainloop must run on the adopted GUI thread")
        self._loop()

    def ensure_started(self, timeout=_START_TIMEOUT_SECONDS):
        """Start the GUI thread if needed and block until the root exists."""
        if self._main_thread:
            with self._lock:
                if self._stopping:
                    raise RuntimeError("GUI thread is shutting down")
                if self.root is not None:
                    return True
                if threading.current_thread() is not threading.main_thread():
                    # Loud on purpose: spawning a thread here is the exact
                    # unsupported configuration this mode exists to prevent.
                    raise RuntimeError(
                        "Tk root not started; on this platform it must be "
                        "adopted on the main thread before any worker call"
                    )
            return self.adopt_main_thread()

        with self._lock:
            if self._stopping:
                raise RuntimeError("GUI thread is shutting down")
            if self._thread is None or not self._thread.is_alive():
                self._ready.clear()
                self._start_error = None
                self._thread = threading.Thread(
                    target=self._run, name="tk-gui", daemon=True
                )
                self._thread.start()

        if not self._ready.wait(timeout):
            raise RuntimeError("Tk GUI thread did not become ready in time")
        if self._start_error is not None:
            raise self._start_error
        return True

    @property
    def running(self):
        if self._thread is None or not self._thread.is_alive():
            return False
        # In main-thread mode the designated thread outlives the root, so
        # liveness alone would keep reporting a torn-down GUI as running.
        return not self._main_thread or self.root is not None

    def _run(self):
        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except Exception as exc:
            self._start_error = exc
            self._ready.set()
            return

        self._ready.set()
        self._loop()

    def _loop(self):
        try:
            self._pump()
            self.root.mainloop()
        except Exception as exc:
            self._log(f"Loop da thread de GUI encerrado com erro: {exc}")
        finally:
            try:
                self.root.destroy()
            except Exception:
                pass
            self.root = None
            # An abnormal exit strands queued callers exactly like stop()
            # would — and a caller blocked in call(timeout=None) is holding
            # the dialog lock, which would refuse every later dialog.
            self._fail_pending()

    def _teardown(self, root):
        # destroy(), not quit(): mainloop exits when the window count hits
        # zero, which is deterministic. quit() relies on _tkinter's quit
        # flag, which proved unreliable once the process has had earlier
        # Tk interpreters (observed on 3.14: stop() hung until the join
        # timeout while the pump kept ticking).
        self._destroyed = True
        root.destroy()

    def stop(self, timeout=5.0):
        """Tear the root down and join the GUI thread."""
        with self._lock:
            if not self.running:
                self._stopping = True
                self._fail_pending()
                return
            self._stopping = True
            thread = self._thread

        if self._main_thread:
            self._stop_main_thread(timeout)
            return

        # Cannot use call(): ensure_started() refuses once _stopping is set.
        self._queue.put((self._teardown, None, None))
        thread.join(timeout)
        self._fail_pending()

    def _stop_main_thread(self, timeout):
        """Tear the root down when the GUI thread is the main thread.

        There is no thread to join — the main thread is the process — so the
        destroy is awaited through the pump instead. Draining the queue before
        it has run would swallow the teardown itself.
        """
        if threading.current_thread() is self._thread:
            # A tray menu callback lands here: on macOS pystray dispatches it
            # on the main thread, from inside the very loop ``mainloop()`` is
            # pumping. Destroying the interpreter mid-dispatch is not safe, so
            # the teardown goes through the queue; ``_loop`` fails the pending
            # queue when it unwinds.
            #
            # The queue rather than a raw ``root.after``: this runs in a Cocoa
            # frame, and scheduling a Tcl timer from there is itself the call
            # that poisons ``tcl_tstate`` and aborts the loop (issue #53).
            self._queue.put((self._teardown, None, None))
            return

        done = threading.Event()
        self._queue.put((self._teardown, {}, done))
        if not done.wait(timeout):
            self._log("Root do Tk não confirmou o encerramento a tempo")
        self._fail_pending()

    def _fail_pending(self):
        """Wake workers whose queued calls will never run.

        Items still queued after the loop exits would otherwise leave their
        callers blocked forever in ``call(timeout=None)`` — ``done`` is only
        set by the GUI thread that no longer exists.
        """
        while True:
            try:
                _func, box, done = self._queue.get_nowait()
            except queue.Empty:
                return
            if box is not None:
                box["error"] = RuntimeError(
                    "GUI thread stopped before the call could run"
                )
            if done is not None:
                done.set()

    # -----------------------------------------------------------------
    # Marshaling
    # -----------------------------------------------------------------

    def call(self, func, timeout=None):
        """Run ``func(root)`` on the GUI thread and return its result.

        Blocks the calling thread until ``func`` returns, re-raising whatever
        it raised. ``timeout=None`` waits indefinitely, which is what a modal
        dialog needs — the user decides when it closes.
        """
        self.ensure_started()
        if threading.current_thread() is self._thread:
            return func(self.root)

        box = {}
        done = threading.Event()
        self._queue.put((func, box, done))
        if not done.wait(timeout):
            raise TimeoutError("GUI call did not complete in time")
        if "error" in box:
            raise box["error"]
        return box.get("value")

    def submit(self, func):
        """Queue ``func(root)`` on the GUI thread without waiting for it.

        Main-thread mode never runs it inline, even when the caller *is* the GUI
        thread. There the caller is a tray menu callback — a Cocoa frame, not a
        Tk one — and any Tcl call made from there poisons ``tcl_tstate`` for the
        running ``mainloop``, aborting the process on its next timer (issue #53,
        and the module docstring for the mechanism). Queuing costs one pump tick
        and nothing is lost. ``call`` cannot do the same: its caller blocks on
        the result, and on the GUI thread that would deadlock.
        """
        self.ensure_started()
        if threading.current_thread() is self._thread and not self._main_thread:
            func(self.root)
            return
        self._queue.put((func, None, None))

    def _pump(self):
        if self.root is None:
            return
        # Reschedule before draining, and unconditionally: a queued callable
        # may open a modal dialog and block here inside a nested event loop,
        # and the next tick is what keeps later requests (and the manager
        # window) responsive. Gating this on the stop flag would also risk
        # dropping the quit sentinel itself. The loop dies with the root.
        try:
            self.root.after(PUMP_INTERVAL_MS, self._pump)
        except Exception:
            return

        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            self._invoke(item)
            if self._destroyed:
                # The teardown was one of the queued items. Anything behind it
                # would run against a destroyed root; ``_loop`` fails those
                # callers as it unwinds.
                return

    def _invoke(self, item):
        func, box, done = item
        try:
            value = func(self.root)
            if box is not None:
                box["value"] = value
        except Exception as exc:
            if box is None:
                self._log(f"Erro em tarefa de GUI: {exc}")
            else:
                box["error"] = exc
        finally:
            if done is not None:
                done.set()

    def _log(self, message):
        if self._logger is not None:
            try:
                self._logger.error(message)
                return
            except Exception:
                pass
        print(message)
