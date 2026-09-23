"""Direct tests for the single-Tk-root marshaling thread (``gui_thread``).

``gui_thread.py`` owns the process's only ``tk.Tk()`` root on a dedicated
thread; worker threads reach it only through ``call`` (blocking, returns the
result, re-raises errors) and ``submit`` (fire-and-forget). That thread is a
spawned one on Windows and the main thread on macOS (issue #24) — every class
here pins its mode explicitly rather than inheriting the host's default, so the
same expectations are asserted wherever the suite runs. These tests hammer
the marshaling contract adversarially: concurrent callers must not cross wires,
a raising callable must re-raise in the *caller* with a usable traceback, a
raising ``submit`` must not kill the pump, and every after-shutdown path must be
a defined refusal rather than a deadlock.

Determinism rules (mirrors the suite): thread coordination uses
``threading.Event``/``join`` with timeouts, never bare sleeps, and every GUI
thread is torn down in ``tearDown`` so a wedged test fails the runner instead of
hanging it. The worker-mode real-Tk classes are skipped where no display is
available and on macOS, where the worker-thread root is not something AppKit
permits (issue #24), exactly as ``test_manager_gui_smoke`` decides it. The
main-thread-mode real-Tk class runs anywhere a display exists, macOS included —
a root on the main thread is legal on every platform.
"""

import os
import subprocess
import sys
import threading
import traceback
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import gui_thread as gt
from gui_thread import GuiThread
from platform_support import IS_MAC


TK_AVAILABLE = False
TK_SKIP_REASON = "Tk display not available"
MAIN_TK_CHILD_ENV = "SNIPVOICE_MAIN_TK_TEST_CHILD"
IN_MAIN_TK_CHILD = os.environ.get(MAIN_TK_CHILD_ENV) == "1"

if IS_MAC:
    TK_SKIP_REASON = "macOS requires AppKit on the main thread; the Tk root runs on a worker thread"
else:
    try:
        _probe = GuiThread(main_thread=False)
        _probe.ensure_started()
        _probe.stop()
        TK_AVAILABLE = True
    except Exception:
        pass


# Probed out of process on purpose. GuiThread cannot probe this mode in-process
# (main-thread mode has no loop running yet, so its stop() would have nothing to
# drain the teardown), and a raw throwaway root cannot either: on macOS Tk 9.0.3
# a root created and destroyed outside any mainloop leaves the Aqua interpreter
# in a state where a *later* root destroyed from inside an ``after`` callback
# traps the process (SIGTRAP, no Python traceback). The app only ever builds one
# root, so this is a test-harness hazard rather than an app bug — the subprocess
# keeps it out of the runner entirely.
MAIN_TK_AVAILABLE = (
    subprocess.run(
        [sys.executable, "-c", "import tkinter; tkinter.Tk().destroy()"],
        capture_output=True,
    ).returncode
    == 0
)


def run_in_daemon(target, timeout=5.0):
    """Run ``target`` in a daemon thread and join with a timeout.

    Returns ``(thread, holder)`` where ``holder`` has ``"result"`` or ``"error"``.
    A still-alive thread after the join is the observable signature of a deadlock,
    which the caller asserts against so a hang becomes a failure, never a wedge.
    """
    holder = {}

    def runner():
        try:
            holder["result"] = target()
        except BaseException as exc:  # noqa: BLE001 - record everything a caller might see
            holder["error"] = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join(timeout)
    return thread, holder


@unittest.skipUnless(TK_AVAILABLE, TK_SKIP_REASON)
class GuiThreadCallTests(unittest.TestCase):
    def setUp(self):
        self.gui = GuiThread(main_thread=False)
        self.gui.ensure_started()
        # Events a test uses to occupy the single GUI thread; always released in
        # tearDown so a blocked pump can drain the teardown sentinel and join.
        self._release_events = []

    def tearDown(self):
        for event in self._release_events:
            event.set()
        self.gui.stop()

    def _blocker(self):
        event = threading.Event()
        self._release_events.append(event)
        return event

    # -- lifecycle --------------------------------------------------------
    def test_ensure_started_is_idempotent(self):
        self.assertTrue(self.gui.running)
        self.assertIsNotNone(self.gui.root)
        first_thread = self.gui._thread
        self.assertTrue(self.gui.ensure_started())
        self.assertIs(self.gui._thread, first_thread, "a second start must not spawn a new thread")

    # -- call -------------------------------------------------------------
    def test_call_runs_on_the_gui_thread_and_returns_the_value(self):
        caller = threading.current_thread()
        ran_on = self.gui.call(lambda _root: threading.current_thread(), timeout=10)
        self.assertIsNot(ran_on, caller)
        self.assertIs(ran_on, self.gui._thread)

    def test_call_receives_the_shared_root(self):
        self.assertIs(self.gui.call(lambda root: root, timeout=10), self.gui.root)

    @unittest.skipUnless(sys.platform == "win32", "guards a Windows Tcl notifier defect")
    def test_queued_calls_complete_while_the_gui_thread_waits_only_for_messages(self):
        # Deterministic stand-in for the deaf-timer stall seen on CI: the GUI
        # thread parks in a wait that timer messages cannot end, as Tcl's event
        # wait behaves once its WM_TIMER wake-ups stop arriving. Only a posted
        # message (QS_POSTMESSAGE) releases it.
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        wait = user32.MsgWaitForMultipleObjects
        wait.argtypes = [wintypes.DWORD, ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD, wintypes.DWORD]
        wait.restype = wintypes.DWORD
        qs_postmessage, infinite = 0x0008, 0xFFFFFFFF
        parked = threading.Event()
        gui_thread_id = []

        def park(_root):
            gui_thread_id.append(threading.get_native_id())
            parked.set()
            wait(0, None, False, infinite, qs_postmessage)

        self.gui.submit(park)
        self.assertTrue(parked.wait(10))
        # Never leave the thread parked if the assertion fails.
        self.addCleanup(user32.PostThreadMessageW, gui_thread_id[0], 0, 0, 0)
        self.assertEqual(self.gui.call(lambda _root: 42, timeout=5), 42)

    def test_call_propagates_the_exception_with_a_usable_traceback(self):
        def boom(_root):
            raise ValueError("kaboom-from-gui")

        with self.assertRaises(ValueError) as ctx:
            self.gui.call(boom, timeout=10)

        self.assertIn("kaboom-from-gui", str(ctx.exception))
        rendered = "".join(
            traceback.format_exception(type(ctx.exception), ctx.exception, ctx.exception.__traceback__)
        )
        # The traceback must still point at the frame that actually raised, on the
        # GUI thread, not merely at the re-raise site in call().
        self.assertIn("boom", rendered)
        self.assertIn("kaboom-from-gui", rendered)

    def test_call_error_does_not_kill_the_pump(self):
        with self.assertRaises(RuntimeError):
            self.gui.call(self._raise_runtime, timeout=10)
        # The very next call must still be served.
        self.assertEqual(self.gui.call(lambda _root: "alive", timeout=10), "alive")

    @staticmethod
    def _raise_runtime(_root):
        raise RuntimeError("boom")

    def test_concurrent_calls_do_not_cross_wires(self):
        """Each caller owns a private result box; results must never swap."""
        worker_count = 24
        start = threading.Barrier(worker_count + 1)
        results = {}
        errors = {}

        def worker(index):
            start.wait(10)
            try:
                # A distinct transform per caller: a shared box would return
                # some other worker's doubled value.
                results[index] = self.gui.call(lambda _root, n=index: (n, n * 7), timeout=15)
            except BaseException as exc:  # noqa: BLE001
                errors[index] = exc

        threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(worker_count)]
        for thread in threads:
            thread.start()
        start.wait(10)
        for thread in threads:
            thread.join(15)

        self.assertEqual(errors, {})
        self.assertEqual(results, {index: (index, index * 7) for index in range(worker_count)})

    def test_call_times_out_when_the_gui_thread_is_busy(self):
        blocker_running = threading.Event()
        release = self._blocker()

        def occupy(_root):
            blocker_running.set()
            release.wait(10)

        # Occupy the single GUI thread, then a call with a short timeout must
        # raise rather than block the caller forever.
        self.gui.submit(occupy)
        self.assertTrue(blocker_running.wait(10), "blocker never started")

        with self.assertRaises(TimeoutError):
            self.gui.call(lambda _root: "unreachable", timeout=0.2)

        # Freeing the pump lets it recover and serve the next call.
        release.set()
        self.assertEqual(self.gui.call(lambda _root: "recovered", timeout=10), "recovered")

    # -- submit -----------------------------------------------------------
    def test_submit_does_not_block_and_runs(self):
        done = threading.Event()
        self.gui.submit(lambda _root: done.set())
        self.assertTrue(done.wait(10))

    def test_submit_error_does_not_kill_the_pump(self):
        def boom(_root):
            raise RuntimeError("swallowed")

        self.gui.submit(boom)
        # A second submit and a call must both still run after the failure.
        done = threading.Event()
        self.gui.submit(lambda _root: done.set())
        self.assertTrue(done.wait(10), "pump stopped serving submit after a raising task")
        self.assertEqual(self.gui.call(lambda _root: "alive", timeout=10), "alive")

    def test_submit_error_is_logged_when_a_logger_is_present(self):
        logger = mock.Mock()
        gui = GuiThread(logger=logger, main_thread=False)
        gui.ensure_started()
        self.addCleanup(gui.stop)

        def boom(_root):
            raise RuntimeError("logged-please")

        gui.submit(boom)
        # Serialize behind the failing task: once this returns the submit ran.
        self.assertEqual(gui.call(lambda _root: "sync", timeout=10), "sync")
        logger.error.assert_called()

    # -- reentrancy -------------------------------------------------------
    def test_call_from_the_gui_thread_runs_inline_without_deadlock(self):
        def outer(_root):
            return self.gui.call(lambda _inner: "inner-value", timeout=10)

        thread, holder = run_in_daemon(lambda: self.gui.call(outer, timeout=10))
        self.assertFalse(thread.is_alive(), "reentrant call deadlocked")
        self.assertEqual(holder.get("result"), "inner-value")

    def test_submit_from_the_gui_thread_runs_inline(self):
        marker = threading.Event()

        def outer(_root):
            self.gui.submit(lambda _inner: marker.set())
            # submit from the GUI thread runs synchronously, so the marker is
            # already set by the time outer returns.
            return marker.is_set()

        self.assertTrue(self.gui.call(outer, timeout=10))


@unittest.skipUnless(TK_AVAILABLE, TK_SKIP_REASON)
class GuiThreadShutdownTests(unittest.TestCase):
    def setUp(self):
        self.gui = GuiThread(main_thread=False)
        self.gui.ensure_started()

    def tearDown(self):
        self.gui.stop()

    def test_stop_joins_and_marks_not_running(self):
        self.gui.stop()
        self.assertFalse(self.gui.running)

    def test_stop_wakes_a_caller_whose_work_never_ran(self):
        """A call still queued when the loop exits must fail its caller instead
        of leaving it blocked forever on ``done``."""
        self.gui.stop()

        box = {}
        done = threading.Event()
        self.gui._queue.put((lambda _root: "never runs", box, done))

        self.gui.stop()  # second stop drains and fails the stranded item
        self.assertTrue(done.is_set(), "stranded caller was never woken")
        self.assertIsInstance(box.get("error"), RuntimeError)

    def test_abnormal_loop_exit_fails_stranded_callers(self):
        """A GUI loop that dies without stop() must also wake queued callers: a
        stranded one blocks forever in ``call(timeout=None)`` while holding the
        dialog lock, refusing every later dialog."""
        gui = GuiThread(logger=mock.Mock(), main_thread=False)
        box = {}
        done = threading.Event()
        gui._queue.put((lambda _root: "never runs", box, done))

        with mock.patch.object(gui, "_pump", side_effect=RuntimeError("boom")):
            gui.ensure_started()
            gui._thread.join(10)

        self.assertFalse(gui._thread.is_alive())
        self.assertTrue(done.is_set(), "stranded caller was never woken")
        self.assertIsInstance(box.get("error"), RuntimeError)

    def test_call_after_stop_refuses_instead_of_deadlocking(self):
        self.gui.stop()
        thread, holder = run_in_daemon(lambda: self.gui.call(lambda _root: "x", timeout=10))
        self.assertFalse(thread.is_alive(), "call after stop deadlocked")
        self.assertIsInstance(holder.get("error"), RuntimeError)

    def test_submit_after_stop_refuses_instead_of_deadlocking(self):
        self.gui.stop()
        thread, holder = run_in_daemon(lambda: self.gui.submit(lambda _root: None))
        self.assertFalse(thread.is_alive(), "submit after stop deadlocked")
        self.assertIsInstance(holder.get("error"), RuntimeError)


class GuiThreadHeadlessTests(unittest.TestCase):
    """Paths that never need a real display, so they run even on headless CI."""

    def test_start_error_is_reraised_and_thread_dies(self):
        gui = GuiThread(main_thread=False)
        self.addCleanup(gui.stop)
        with mock.patch.object(gt.tk, "Tk", side_effect=RuntimeError("no display")):
            with self.assertRaises(RuntimeError) as ctx:
                gui.ensure_started()
        self.assertIn("no display", str(ctx.exception))
        # A failed start must not leave a live thread pretending to be a root.
        if gui._thread is not None:
            gui._thread.join(5)
            self.assertFalse(gui._thread.is_alive())
        self.assertFalse(gui.running)

    def test_calls_are_refused_once_shutting_down_without_a_root(self):
        """stop() before any start sets the shutdown flag; later call/submit must
        refuse promptly (no Tk ever created), not hang."""
        gui = GuiThread(main_thread=False)
        gui.stop()  # no thread yet: just flips _stopping and drains

        call_thread, call_holder = run_in_daemon(lambda: gui.call(lambda _root: "x", timeout=5))
        submit_thread, submit_holder = run_in_daemon(lambda: gui.submit(lambda _root: None))

        self.assertFalse(call_thread.is_alive(), "call after shutdown deadlocked")
        self.assertFalse(submit_thread.is_alive(), "submit after shutdown deadlocked")
        self.assertIsInstance(call_holder.get("error"), RuntimeError)
        self.assertIsInstance(submit_holder.get("error"), RuntimeError)
        self.assertFalse(gui.running)

    def test_stop_with_no_thread_wakes_a_manually_queued_caller(self):
        gui = GuiThread(main_thread=False)
        box = {}
        done = threading.Event()
        gui._queue.put((lambda _root: "never", box, done))
        gui.stop()
        self.assertTrue(done.is_set())
        self.assertIsInstance(box.get("error"), RuntimeError)


class MainThreadModeHeadlessTests(unittest.TestCase):
    """Main-thread mode guard rails, asserted without ever creating a root."""

    def test_mode_follows_the_platform_by_default(self):
        with mock.patch.object(gt, "tk_runs_on_main_thread", return_value=True):
            self.assertTrue(GuiThread()._main_thread)
        with mock.patch.object(gt, "tk_runs_on_main_thread", return_value=False):
            self.assertFalse(GuiThread()._main_thread)

    def test_worker_ensure_started_refuses_instead_of_spawning_a_thread(self):
        """The whole point of the mode: a worker must never conjure a root.

        Spawning one here would put Tk off the main thread on macOS, which is
        the configuration AppKit aborts the process over.
        """
        gui = GuiThread(main_thread=True)
        thread, holder = run_in_daemon(gui.ensure_started)

        self.assertFalse(thread.is_alive(), "ensure_started deadlocked")
        self.assertIsInstance(holder.get("error"), RuntimeError)
        self.assertIn("main thread", str(holder["error"]))
        self.assertIsNone(gui._thread, "a GUI thread was spawned in main-thread mode")
        self.assertIsNone(gui.root)

    def test_call_from_a_worker_refuses_before_the_root_is_adopted(self):
        gui = GuiThread(main_thread=True)
        thread, holder = run_in_daemon(lambda: gui.call(lambda _root: "x", timeout=5))
        self.assertFalse(thread.is_alive(), "call deadlocked")
        self.assertIsInstance(holder.get("error"), RuntimeError)

    def test_adopt_from_a_worker_thread_is_refused(self):
        gui = GuiThread(main_thread=True)
        thread, holder = run_in_daemon(gui.adopt_main_thread)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(holder.get("error"), RuntimeError)

    def test_adopt_and_run_mainloop_require_the_mode(self):
        gui = GuiThread(main_thread=False)
        with self.assertRaises(RuntimeError):
            gui.adopt_main_thread()
        with self.assertRaises(RuntimeError):
            gui.run_mainloop()

    def test_run_mainloop_refuses_without_an_adopted_root(self):
        gui = GuiThread(main_thread=True)
        with self.assertRaises(RuntimeError):
            gui.run_mainloop()

    def test_stop_from_the_gui_thread_never_schedules_a_tcl_timer(self):
        """Issue #53: ``stop()`` runs in a Cocoa frame (the tray's *Sair*).

        ``root.after`` there is a Tcl call from outside any ENTER_PYTHON frame,
        which clears ``_tkinter``'s ``tcl_tstate`` and aborts the mainloop on its
        next timer. The teardown must go on the queue instead.
        """
        gui = GuiThread(main_thread=True)
        root = mock.Mock()
        with mock.patch.object(gt.tk, "Tk", return_value=root):
            gui.adopt_main_thread()

        gui.stop()

        root.after.assert_not_called()
        queued = [item[0] for item in list(gui._queue.queue)]
        self.assertIn(gui._teardown, queued)

    def test_failed_adopt_leaves_no_thread_designated(self):
        """A designated thread with no root would make call() run inline on None."""
        gui = GuiThread(main_thread=True)
        with mock.patch.object(gt.tk, "Tk", side_effect=RuntimeError("no display")):
            with self.assertRaises(RuntimeError):
                gui.adopt_main_thread()
        self.assertIsNone(gui._thread)
        self.assertFalse(gui.running)


@unittest.skipUnless(
    MAIN_TK_AVAILABLE and (not IS_MAC or IN_MAIN_TK_CHILD),
    "real macOS Tk contracts run in an isolated subprocess",
)
class MainThreadModeTkTests(unittest.TestCase):
    """Main-thread mode against a real root — the macOS model, portable to run.

    Every test drives the loop the way the app does: adopt on the main thread,
    let a worker marshal in, and enter ``run_mainloop`` (which the worker ends).
    A watchdog ``after`` closes the root regardless, so a broken pump fails the
    test instead of hanging the runner.
    """

    def setUp(self):
        self.gui = GuiThread(main_thread=True)
        self.gui.adopt_main_thread()
        self.gui.root.after(8000, self.gui.root.destroy)  # watchdog

    def tearDown(self):
        if self.gui.root is not None:
            try:
                self.gui.root.destroy()
            except Exception:
                pass
            self.gui.root = None

    def _drive(self, worker):
        """Run ``worker`` in a daemon thread while the main thread pumps."""
        holder = {}

        def runner():
            try:
                holder["result"] = worker()
            except BaseException as exc:  # noqa: BLE001 - record what a caller sees
                holder["error"] = exc
            finally:
                self.gui.stop()

        thread = threading.Thread(target=runner, daemon=True)
        thread.start()
        self.gui.run_mainloop()
        thread.join(5)
        self.assertFalse(thread.is_alive(), "worker never finished")
        return holder

    def test_adopts_the_main_thread_as_the_gui_thread(self):
        self.assertIs(self.gui._thread, threading.main_thread())
        self.assertTrue(self.gui.running)

    def test_worker_call_runs_on_the_main_thread_and_returns_the_value(self):
        holder = self._drive(
            lambda: self.gui.call(
                lambda root: (threading.current_thread(), root.winfo_class()), timeout=10
            )
        )
        self.assertNotIn("error", holder)
        ran_on, widget_class = holder["result"]
        self.assertIs(ran_on, threading.main_thread())
        self.assertEqual(widget_class, "Tk")

    def test_worker_call_reraises_in_the_caller(self):
        def worker():
            with self.assertRaises(ValueError):
                self.gui.call(lambda _root: _raise(ValueError("boom")), timeout=10)
            return "raised"

        self.assertEqual(self._drive(worker)["result"], "raised")

    def test_submit_is_fire_and_forget_and_a_raiser_does_not_kill_the_pump(self):
        seen = []
        done = threading.Event()

        def worker():
            self.gui.submit(lambda _root: _raise(RuntimeError("submit boom")))
            self.gui.submit(lambda _root: (seen.append(threading.current_thread()), done.set()))
            self.assertTrue(done.wait(5), "pump died on the raising submit")
            return "ok"

        self.gui._logger = mock.Mock()
        self.assertEqual(self._drive(worker)["result"], "ok")
        self.assertEqual(seen, [threading.main_thread()])

    def test_call_from_the_gui_thread_itself_runs_inline(self):
        """Tray callbacks land on the main thread on macOS; they must not queue
        onto a pump they are themselves blocking."""
        self.assertEqual(self.gui.call(lambda _root: "inline", timeout=5), "inline")

    def test_submit_from_the_gui_thread_queues_instead_of_running_inline(self):
        """The macOS rule (#53): a tray callback is a *Cocoa* frame, and any Tcl
        call made there clears ``tcl_tstate`` for the running mainloop, aborting
        the process on its next timer. Only Python state may be touched, so the
        work has to leave the Cocoa frame and run from the pump."""
        order = []

        def from_the_gui_thread():
            self.gui.submit(lambda _root: order.append("queued work"))
            order.append("submit returned")

        self.gui.root.after(0, from_the_gui_thread)
        self.gui.root.after(600, self.gui.stop)
        self.gui.run_mainloop()
        self.assertEqual(order, ["submit returned", "queued work"])

    def test_stop_from_a_worker_tears_the_root_down_and_ends_the_loop(self):
        holder = self._drive(lambda: "worker done")
        self.assertEqual(holder["result"], "worker done")
        self.assertIsNone(self.gui.root)
        self.assertFalse(self.gui.running, "main thread liveness reported a dead root as running")

    def test_stop_from_the_gui_thread_defers_the_teardown_to_the_next_tick(self):
        """quit_app() runs inside the loop on macOS: destroying there would tear
        the interpreter down mid-dispatch."""
        self.gui.root.after(0, self.gui.stop)
        self.gui.run_mainloop()
        self.assertIsNone(self.gui.root)

    def test_stop_wakes_callers_whose_queued_work_will_never_run(self):
        early_box, early_done = {}, threading.Event()
        late_box, late_done = {}, threading.Event()

        def shutdown():
            # Queued from inside the loop: the pump drains synchronously on
            # entry, so an item put before run_mainloop would simply have run.
            self.gui._queue.put((lambda _root: "in time", early_box, early_done))
            self.gui.stop()
            self.gui._queue.put((lambda _root: "never", late_box, late_done))

        self.gui.root.after(0, shutdown)
        self.gui.run_mainloop()
        # The teardown is queued, so work already in line still runs; only what
        # lands behind it is stranded. Either way no caller is left blocked.
        self.assertTrue(early_done.is_set(), "queued caller was never woken")
        self.assertEqual(early_box.get("value"), "in time")
        self.assertTrue(late_done.is_set(), "stranded caller was never woken")
        self.assertIsInstance(late_box.get("error"), RuntimeError)


@unittest.skipUnless(
    IS_MAC and MAIN_TK_AVAILABLE and not IN_MAIN_TK_CHILD,
    "needs macOS Tk in the parent test process",
)
class MainThreadModeMacIsolationTests(unittest.TestCase):
    def test_real_tk_contracts_run_in_an_isolated_process(self):
        """Keep Aqua interpreter teardown out of the full-suite process.

        Python 3.14 can defer Tcl async-handler cleanup until a later test. If
        that test uses a worker thread, Aqua aborts with ``Tcl_AsyncDelete``
        even though every real-Tk contract already passed. The application has
        one root for one process lifetime; this boundary mirrors that lifecycle.
        """
        env = os.environ.copy()
        env[MAIN_TK_CHILD_ENV] = "1"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "unittest",
                "tests.test_gui_thread.MainThreadModeTkTests",
                "-v",
            ],
            cwd=os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}",
        )


def _raise(exc):
    raise exc


if __name__ == "__main__":
    unittest.main()
