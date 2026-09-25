import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import platform_support as ps


class OsDetectionTests(unittest.TestCase):
    def test_current_os_is_known(self):
        self.assertIn(ps.current_os(), {"windows", "darwin", "linux"})

    def test_flags_are_consistent(self):
        flags = [ps.IS_WINDOWS, ps.IS_MAC, ps.IS_LINUX]
        self.assertEqual(sum(1 for f in flags if f), 1)

    def test_paste_modifier_matches_platform(self):
        self.assertEqual(ps.paste_modifier_is_cmd(), ps.IS_MAC)


class LockfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "app.lock")

    def _write_lock(self, pid=None):
        if ps._fcntl is None:
            os.mkdir(self.path)
            if pid is not None:
                with open(
                    os.path.join(self.path, ps._FALLBACK_OWNER_NAME),
                    "w",
                    encoding="ascii",
                ) as handle:
                    handle.write(str(pid))
            return
        with open(self.path, "w", encoding="utf-8") as handle:
            if pid is not None:
                handle.write(str(pid))

    def _read_lock_pid(self):
        owner_path = self.path
        if os.path.isdir(self.path):
            owner_path = os.path.join(self.path, ps._FALLBACK_OWNER_NAME)
        with open(owner_path, encoding="utf-8") as handle:
            return int(handle.read().strip())

    def test_acquire_then_same_process_reacquires(self):
        self.assertTrue(ps.acquire_lockfile(self.path))
        # Same PID may re-acquire (idempotent for one process).
        self.assertTrue(ps.acquire_lockfile(self.path))

    def test_stale_lock_is_reclaimed(self):
        # A PID that is very unlikely to be running.
        self._write_lock(2147483000)
        self.assertTrue(ps.acquire_lockfile(self.path))
        self.assertEqual(self._read_lock_pid(), os.getpid())

    def test_live_lock_owned_by_another_pid_is_not_reclaimed(self):
        if ps._fcntl is None:
            self._write_lock(1234)
            with mock.patch.object(ps.os, "getpid", return_value=5678), \
                    mock.patch.object(ps, "_pid_is_running", return_value=True):
                self.assertFalse(ps.acquire_lockfile(self.path))
            self.assertEqual(self._read_lock_pid(), 1234)
            return

        owner = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        ps._fcntl.flock(owner, ps._fcntl.LOCK_EX | ps._fcntl.LOCK_NB)
        try:
            self.assertFalse(ps.acquire_lockfile(self.path))
        finally:
            ps._fcntl.flock(owner, ps._fcntl.LOCK_UN)
            os.close(owner)

    def test_fallback_reclaims_old_empty_lock_left_by_crashed_creator(self):
        os.mkdir(self.path)
        old_timestamp = ps.time.time() - ps._EMPTY_LOCK_STALE_SECONDS - 1
        os.utime(self.path, (old_timestamp, old_timestamp))

        with mock.patch.object(ps, "_fcntl", None):
            self.assertTrue(ps.acquire_lockfile(self.path))

        self.assertEqual(self._read_lock_pid(), os.getpid())

    def test_fallback_does_not_reclaim_fresh_empty_lock(self):
        os.mkdir(self.path)

        with mock.patch.object(ps, "_fcntl", None):
            self.assertFalse(ps.acquire_lockfile(self.path))

    def test_concurrent_different_processes_only_one_acquires(self):
        barrier = threading.Barrier(2)
        results = []
        thread_pids = {}

        def fake_pid():
            return thread_pids[threading.get_ident()]

        def worker(pid):
            thread_pids[threading.get_ident()] = pid
            barrier.wait(timeout=2)
            results.append(ps.acquire_lockfile(self.path))

        with mock.patch.object(ps.os, "getpid", side_effect=fake_pid), \
                mock.patch.object(ps, "_pid_is_running", return_value=True):
            threads = [
                threading.Thread(target=worker, args=(1111,)),
                threading.Thread(target=worker, args=(2222,)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertCountEqual(results, [True, False])

    def test_fallback_stale_reclaim_cannot_remove_the_winner(self):
        with mock.patch.object(ps, "_fcntl", None):
            os.mkdir(self.path)
            with open(
                os.path.join(self.path, ps._FALLBACK_OWNER_NAME),
                "w",
                encoding="ascii",
            ) as handle:
                handle.write("9999")

            barrier = threading.Barrier(2)
            results = []
            thread_pids = {}
            synchronized_threads = set()
            synchronization_lock = threading.Lock()

            def fake_pid():
                return thread_pids[threading.get_ident()]

            def stale_owner(_pid):
                thread_id = threading.get_ident()
                with synchronization_lock:
                    first_check = thread_id not in synchronized_threads
                    synchronized_threads.add(thread_id)
                if first_check:
                    barrier.wait(timeout=2)
                return False

            def worker(pid):
                thread_pids[threading.get_ident()] = pid
                results.append(ps.acquire_lockfile(self.path))

            with mock.patch.object(ps.os, "getpid", side_effect=fake_pid), \
                    mock.patch.object(ps, "_pid_is_running", side_effect=stale_owner):
                threads = [
                    threading.Thread(target=worker, args=(1111,)),
                    threading.Thread(target=worker, args=(2222,)),
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

        self.assertCountEqual(results, [True, False])
        self.assertIn(self._read_lock_pid(), {1111, 2222})

    def test_release_removes_own_lock(self):
        ps.acquire_lockfile(self.path)
        ps.release_lockfile(self.path)
        if ps._fcntl is None:
            self.assertFalse(os.path.exists(self.path))
        else:
            # Kernel ownership is gone even though the diagnostic PID file stays.
            self.assertTrue(os.path.exists(self.path))
            self.assertTrue(ps.acquire_lockfile(self.path))
            ps.release_lockfile(self.path)

    def test_release_does_not_remove_another_process_lock(self):
        self._write_lock(1234)
        with mock.patch.object(ps.os, "getpid", return_value=5678):
            ps.release_lockfile(self.path)
        self.assertEqual(self._read_lock_pid(), 1234)


class AutostartTests(unittest.TestCase):
    def test_target_path_has_expected_suffix(self):
        path = ps.autostart_target_path("Snipvoice")
        self.assertTrue(path.endswith(".lnk") or path.endswith(".plist") or path.endswith(".desktop"))

    def test_macos_plist_is_wellformed(self):
        plist = ps.macos_launch_agent("Snipvoice", "/usr/bin/txt")
        self.assertIn("com.snipvoice", plist)
        self.assertIn("<key>RunAtLoad</key><true/>", plist)
        self.assertIn("/usr/bin/txt", plist)

    def test_msix_package_reports_managed_without_reading_a_shortcut(self):
        with mock.patch.object(ps, "is_msix_packaged", return_value=True), \
                mock.patch.object(ps, "read_autostart_command") as read:
            self.assertEqual(ps.autostart_state("Snipvoice"), ps.AUTOSTART_MANAGED)
        read.assert_not_called()

    def test_msix_detection_is_windows_only(self):
        with mock.patch.object(ps.sys, "platform", "darwin"), \
                mock.patch.object(ps.ctypes, "windll", create=True) as windll:
            self.assertFalse(ps.is_msix_packaged())
        windll.kernel32.GetCurrentPackageFullName.assert_not_called()

    @unittest.skipUnless(sys.platform.startswith("win"), "Windows package identity API")
    def test_unpackaged_test_process_has_no_package_identity(self):
        self.assertFalse(ps.is_msix_packaged())

    def test_linux_desktop_entry_is_wellformed(self):
        entry = ps.linux_desktop_entry("Snipvoice", "python snipvoice.pyw")
        self.assertIn("[Desktop Entry]", entry)
        self.assertIn("Exec=python snipvoice.pyw", entry)
        self.assertIn("Name=Snipvoice", entry)


class AutostartRoundTripTests(unittest.TestCase):
    """install/remove round-trip on each mocked OS, writing into a temp dir."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _redirect(self, system, filename):
        path = os.path.join(self.tmp, "autostart", filename)
        patches = [
            mock.patch.object(ps, "current_os", return_value=system),
            mock.patch.object(ps, "autostart_target_path", return_value=path),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return path

    def test_linux_round_trip(self):
        path = self._redirect("linux", "snipvoice.desktop")
        created = ps.install_autostart("Snipvoice", ["/usr/bin/python3", "/opt/snipvoice.pyw"])
        self.assertEqual(created, path)
        self.assertTrue(os.path.exists(path))
        with open(path, encoding="utf-8") as handle:
            content = handle.read()
        self.assertIn("[Desktop Entry]", content)
        self.assertIn("Exec=/usr/bin/python3 /opt/snipvoice.pyw", content)

        self.assertTrue(ps.remove_autostart("Snipvoice"))
        self.assertFalse(os.path.exists(path))
        self.assertFalse(ps.remove_autostart("Snipvoice"))

    def test_macos_round_trip(self):
        path = self._redirect("darwin", "com.snipvoice.plist")
        ps.install_autostart("Snipvoice", ["/usr/bin/python3", "/opt/snipvoice.pyw"])
        with open(path, encoding="utf-8") as handle:
            content = handle.read()
        self.assertIn("<string>/usr/bin/python3</string>", content)
        self.assertIn("<string>/opt/snipvoice.pyw</string>", content)
        self.assertIn("<key>RunAtLoad</key><true/>", content)

        self.assertTrue(ps.remove_autostart("Snipvoice"))
        self.assertFalse(os.path.exists(path))

    def test_windows_round_trip_with_mocked_shortcut(self):
        path = self._redirect("windows", "Snipvoice.lnk")

        def fake_run(argv, **kwargs):
            # Stand in for WScript.Shell: the real call writes the .lnk.
            with open(path, "wb") as handle:
                handle.write(b"L\x00\x00\x00")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(ps.subprocess, "run", side_effect=fake_run) as run:
            created = ps.install_autostart("Snipvoice", [r"C:\App\Snipvoice.exe"])

        self.assertEqual(created, path)
        script = run.call_args[0][0][-1]
        self.assertIn("WScript.Shell", script)
        self.assertIn(r"'C:\App\Snipvoice.exe'", script)
        self.assertIn(r"$sc.WorkingDirectory = 'C:\App'", script)
        self.assertIn("$sc.Arguments = ''", script)
        self.assertTrue(os.path.exists(path))
        self.assertTrue(ps.remove_autostart("Snipvoice"))

    def test_windows_shortcut_from_source_works_in_the_app_dir(self):
        path = self._redirect("windows", "Snipvoice.lnk")
        source_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        launcher = os.path.join(source_dir, "snipvoice.pyw")

        def fake_run(argv, **kwargs):
            open(path, "wb").close()
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(ps.subprocess, "run", side_effect=fake_run) as run:
            ps.install_autostart("Snipvoice", [r"C:\Py\pythonw.exe", launcher])

        # Literal expectations: building these with _ps_quote would let a broken
        # quoter satisfy its own test.
        script = run.call_args[0][0][-1]
        self.assertIn(f"$sc.WorkingDirectory = '{source_dir}'", script)
        self.assertIn(f"$sc.Arguments = '{launcher}'", script)
        self.assertIn(r"$sc.TargetPath = 'C:\Py\pythonw.exe'", script)

    def test_windows_single_quote_in_path_is_escaped(self):
        """A quote in the path must not break out of the PowerShell string literal."""
        path = self._redirect("windows", "Snipvoice.lnk")
        target = r"C:\Users\O'Brien\Snipvoice.exe"

        def fake_run(argv, **kwargs):
            open(path, "wb").close()
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(ps.subprocess, "run", side_effect=fake_run) as run:
            ps.install_autostart("Snipvoice", [target])

        script = run.call_args[0][0][-1]
        self.assertIn(r"$sc.TargetPath = 'C:\Users\O''Brien\Snipvoice.exe'", script)
        # Doubling is the only escape: no lone quote may survive to end the literal.
        self.assertNotIn(r"'C:\Users\O'Brien", script)

    def test_windows_failure_raises_instead_of_claiming_success(self):
        path = self._redirect("windows", "Snipvoice.lnk")
        failure = mock.Mock(returncode=1, stdout="", stderr="access denied")
        with mock.patch.object(ps.subprocess, "run", return_value=failure):
            with self.assertRaises(OSError) as ctx:
                ps.install_autostart("Snipvoice", [r"C:\App\Snipvoice.exe"])
        self.assertIn("access denied", str(ctx.exception))
        self.assertFalse(os.path.exists(path))

    def test_missing_entry_after_write_raises(self):
        self._redirect("windows", "Snipvoice.lnk")
        ok = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(ps.subprocess, "run", return_value=ok):
            with self.assertRaises(OSError):
                ps.install_autostart("Snipvoice", [r"C:\App\Snipvoice.exe"])


class AutostartStateTests(unittest.TestCase):
    """absent/current/stale classification: presence alone must not mean enabled."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # Real files on disk to stand in for the installed exe/interpreter and
        # scripts: classification requires every argv element to still exist.
        self.installed = os.path.join(self.tmp, "current-app.exe")
        open(self.installed, "wb").close()
        self.command = [self.installed, os.path.join(self.tmp, "snipvoice.pyw")]
        open(self.command[1], "wb").close()
        self.missing = [os.path.join(self.tmp, "deleted-dist", "app.exe")]
        self.other_install = [self.installed, os.path.join(self.tmp, "other", "snipvoice.pyw")]
        os.makedirs(os.path.dirname(self.other_install[1]))
        open(self.other_install[1], "wb").close()

    def _redirect(self, system, filename):
        path = os.path.join(self.tmp, "autostart", filename)
        for patch in (
            mock.patch.object(ps, "current_os", return_value=system),
            mock.patch.object(ps, "autostart_target_path", return_value=path),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        return path

    def _write(self, system, argv):
        """Install an entry for ``argv`` with the Windows .lnk write mocked out."""
        if system != "windows":
            ps.install_autostart("Snipvoice", argv)
            return

        path = ps.autostart_target_path("Snipvoice")
        self._lnk = argv

        def fake_run(cmd, **kwargs):
            open(path, "wb").close()
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(ps.subprocess, "run", side_effect=fake_run):
            ps.install_autostart("Snipvoice", argv)

    def _windows_read(self, argv):
        """Mock the WScript.Shell read-back for the argv the .lnk was written with."""
        target = argv[0]
        arguments = subprocess.list2cmdline(argv[1:]) if len(argv) > 1 else ""
        return mock.patch.object(
            ps.subprocess,
            "run",
            return_value=mock.Mock(returncode=0, stdout=f"{target}\n{arguments}\n", stderr=""),
        )

    # -- linux ------------------------------------------------------------
    def test_linux_absent(self):
        self._redirect("linux", "snipvoice.desktop")
        self.assertEqual(ps.autostart_state("Snipvoice", self.command), ps.AUTOSTART_ABSENT)

    def test_linux_current(self):
        self._redirect("linux", "snipvoice.desktop")
        self._write("linux", self.command)
        self.assertEqual(ps.autostart_state("Snipvoice", self.command), ps.AUTOSTART_CURRENT)

    def test_linux_stale_when_target_is_gone(self):
        self._redirect("linux", "snipvoice.desktop")
        self._write("linux", self.missing)
        self.assertEqual(ps.autostart_state("Snipvoice", self.command), ps.AUTOSTART_STALE)

    def test_linux_stale_when_another_install_owns_it(self):
        self._redirect("linux", "snipvoice.desktop")
        self._write("linux", self.other_install)
        self.assertEqual(ps.autostart_state("Snipvoice", self.command), ps.AUTOSTART_STALE)

    def test_stale_when_script_is_gone_even_if_interpreter_survives(self):
        """A surviving interpreter must not make a deleted checkout's entry live."""
        self._redirect("linux", "snipvoice.desktop")
        dead = [self.installed, os.path.join(self.tmp, "deleted-checkout", "snipvoice.pyw")]
        self._write("linux", dead)
        self.assertEqual(ps.autostart_state("Snipvoice", self.command), ps.AUTOSTART_STALE)
        # And it is the dead kind of stale — the one the app may repair.
        self.assertFalse(ps.autostart_target_exists(dead))

    # -- macOS ------------------------------------------------------------
    def test_macos_absent(self):
        self._redirect("darwin", "com.snipvoice.plist")
        self.assertEqual(ps.autostart_state("Snipvoice", self.command), ps.AUTOSTART_ABSENT)

    def test_macos_current(self):
        self._redirect("darwin", "com.snipvoice.plist")
        self._write("darwin", self.command)
        self.assertEqual(ps.autostart_state("Snipvoice", self.command), ps.AUTOSTART_CURRENT)

    def test_macos_stale_when_target_is_gone(self):
        self._redirect("darwin", "com.snipvoice.plist")
        self._write("darwin", self.missing)
        self.assertEqual(ps.autostart_state("Snipvoice", self.command), ps.AUTOSTART_STALE)

    def test_macos_stale_when_another_install_owns_it(self):
        self._redirect("darwin", "com.snipvoice.plist")
        self._write("darwin", self.other_install)
        self.assertEqual(ps.autostart_state("Snipvoice", self.command), ps.AUTOSTART_STALE)

    # -- windows ----------------------------------------------------------
    def test_windows_absent_reads_nothing(self):
        self._redirect("windows", "Snipvoice.lnk")
        with mock.patch.object(ps.subprocess, "run") as run:
            self.assertEqual(ps.autostart_state("Snipvoice", self.command), ps.AUTOSTART_ABSENT)
        run.assert_not_called()

    def test_windows_current(self):
        self._redirect("windows", "Snipvoice.lnk")
        self._write("windows", self.command)
        with self._windows_read(self.command) as run:
            self.assertEqual(ps.autostart_state("Snipvoice", self.command), ps.AUTOSTART_CURRENT)
        # One shortcut read, not one per caller.
        self.assertEqual(run.call_count, 1)

    def test_windows_current_ignores_case_and_separators(self):
        self._redirect("windows", "Snipvoice.lnk")
        self._write("windows", self.command)
        shouty = [arg.upper().replace("\\", "/") for arg in self.command]
        # The subject here is the path *comparison*, not the on-disk check: an
        # uppercased temp path does not exist on a case-sensitive filesystem, so
        # off Windows the entry would classify as stale before ever being
        # compared. test_windows_current covers the unmocked chain.
        with self._windows_read(shouty), \
                mock.patch.object(ps, "autostart_target_exists", return_value=True):
            self.assertEqual(ps.autostart_state("Snipvoice", self.command), ps.AUTOSTART_CURRENT)

    def test_windows_stale_when_target_is_gone(self):
        self._redirect("windows", "Snipvoice.lnk")
        self._write("windows", self.missing)
        with self._windows_read(self.missing):
            self.assertEqual(ps.autostart_state("Snipvoice", self.command), ps.AUTOSTART_STALE)

    def test_windows_stale_when_another_install_owns_it(self):
        self._redirect("windows", "Snipvoice.lnk")
        self._write("windows", self.other_install)
        with self._windows_read(self.other_install):
            self.assertEqual(ps.autostart_state("Snipvoice", self.command), ps.AUTOSTART_STALE)

    def test_windows_unreadable_shortcut_is_stale_not_enabled(self):
        self._redirect("windows", "Snipvoice.lnk")
        self._write("windows", self.command)
        failure = mock.Mock(returncode=1, stdout="", stderr="cannot open")
        with mock.patch.object(ps.subprocess, "run", return_value=failure):
            self.assertEqual(ps.autostart_state("Snipvoice", self.command), ps.AUTOSTART_STALE)

    def test_is_enabled_is_no_longer_presence_only(self):
        """The old predicate trusted the path alone; a dead entry fooled it."""
        path = self._redirect("linux", "snipvoice.desktop")
        self._write("linux", self.missing)
        with mock.patch.object(ps, "default_autostart_command", return_value=self.command):
            self.assertTrue(os.path.exists(path))
            self.assertFalse(ps.is_autostart_enabled("Snipvoice"))

            self._write("linux", self.command)
            self.assertTrue(ps.is_autostart_enabled("Snipvoice"))

    def test_windows_quoted_arguments_round_trip(self):
        """A .lnk stores args as one string; splitting it must recover the argv."""
        self._redirect("windows", "Snipvoice.lnk")
        spaced = [self.installed, os.path.join(self.tmp, "with space", "snipvoice.pyw")]
        os.makedirs(os.path.dirname(spaced[1]))
        open(spaced[1], "wb").close()
        self._write("windows", spaced)
        with self._windows_read(spaced):
            self.assertEqual(ps.autostart_state("Snipvoice", spaced), ps.AUTOSTART_CURRENT)


class ClassifyAutostartTests(unittest.TestCase):
    """Direct unit coverage of the pure classifier the state resolver builds on.

    The round-trip tests above drive classify_autostart through
    autostart_state/install; these pin the argv-comparison rules themselves.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.exe = os.path.join(self.tmp, "app.exe")
        self.script = os.path.join(self.tmp, "snipvoice.pyw")
        for path in (self.exe, self.script):
            open(path, "wb").close()
        self.command = [self.exe, self.script]

    def test_none_is_absent(self):
        self.assertEqual(ps.classify_autostart(None, self.command), ps.AUTOSTART_ABSENT)

    def test_identical_argv_is_current(self):
        self.assertEqual(
            ps.classify_autostart(list(self.command), self.command), ps.AUTOSTART_CURRENT
        )

    def test_differing_length_is_stale(self):
        # A single-element exe entry versus an interpreter+script expected command.
        self.assertEqual(ps.classify_autostart([self.exe], self.command), ps.AUTOSTART_STALE)

    def test_pythonw_versus_python_is_stale(self):
        pythonw = os.path.join(self.tmp, "pythonw.exe")
        open(pythonw, "wb").close()
        entry = [pythonw, self.script]
        expected = [self.exe, self.script]
        self.assertEqual(ps.classify_autostart(entry, expected), ps.AUTOSTART_STALE)

    def test_missing_target_is_stale_even_when_expected_matches(self):
        gone = [os.path.join(self.tmp, "nope", "app.exe"), self.script]
        # Even if the expected command were these exact strings, a path that does
        # not exist is dead at login and must classify stale, not current.
        self.assertEqual(ps.classify_autostart(gone, gone), ps.AUTOSTART_STALE)

    def test_relative_path_entry_is_stale(self):
        # A relative argv element cannot be confirmed on disk, so it is a dead
        # pointer regardless of what the expected command is.
        self.assertEqual(
            ps.classify_autostart(["app.exe", "snipvoice.pyw"], self.command),
            ps.AUTOSTART_STALE,
        )

    def test_windows_comparison_ignores_case_and_separators(self):
        # Only meaningful under Windows path rules; force them and stub the
        # on-disk check, since the shouty variant will not exist on disk.
        shouty = [arg.upper().replace("\\", "/") for arg in self.command]
        with mock.patch.object(ps, "current_os", return_value="windows"), \
                mock.patch.object(ps, "autostart_target_exists", return_value=True):
            self.assertEqual(
                ps.classify_autostart(shouty, self.command), ps.AUTOSTART_CURRENT
            )

    def test_defaults_to_default_autostart_command_when_expected_is_none(self):
        with mock.patch.object(ps, "default_autostart_command", return_value=self.command):
            self.assertEqual(ps.classify_autostart(list(self.command)), ps.AUTOSTART_CURRENT)


class ReadShortcutFailureTests(unittest.TestCase):
    """The Windows .lnk reader is a PowerShell round-trip; garbage and failures
    must degrade to a safe classification, never a crash or a false 'enabled'."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.lnk = os.path.join(self.tmp, "Snipvoice.lnk")
        open(self.lnk, "wb").close()

    def _windows(self, run_result):
        return (
            mock.patch.object(ps, "current_os", return_value="windows"),
            mock.patch.object(ps, "autostart_target_path", return_value=self.lnk),
            mock.patch.object(ps.subprocess, "run", return_value=run_result),
        )

    def test_nonzero_exit_raises_oserror_with_detail(self):
        result = mock.Mock(returncode=1, stdout="", stderr="access is denied")
        patches = self._windows(result)
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        with self.assertRaises(OSError) as ctx:
            ps.read_autostart_command("Snipvoice")
        self.assertIn("access is denied", str(ctx.exception))

    def test_empty_output_yields_no_command(self):
        result = mock.Mock(returncode=0, stdout="\n\n", stderr="")
        patches = self._windows(result)
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.assertEqual(ps.read_autostart_command("Snipvoice"), [])

    def test_target_only_output_has_no_arguments(self):
        result = mock.Mock(returncode=0, stdout=r"C:\App\Snipvoice.exe" + "\n", stderr="")
        patches = self._windows(result)
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.assertEqual(ps.read_autostart_command("Snipvoice"), [r"C:\App\Snipvoice.exe"])

    def test_garbage_output_classifies_stale_not_enabled(self):
        # Empty stdout -> [] from the reader -> classify_autostart([]) is stale,
        # so the tray box shows unchecked instead of claiming a working entry.
        result = mock.Mock(returncode=0, stdout="", stderr="")
        patches = self._windows(result)
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.assertEqual(ps.autostart_state("Snipvoice"), ps.AUTOSTART_STALE)

    def test_nonzero_exit_is_caught_as_stale_by_autostart_state(self):
        result = mock.Mock(returncode=1, stdout="", stderr="cannot open")
        patches = self._windows(result)
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.assertEqual(ps.autostart_state("Snipvoice"), ps.AUTOSTART_STALE)


class TrayBackendTests(unittest.TestCase):
    """The win32 pin makes ``import pystray`` fail off Windows (issue #23)."""

    def test_windows_pins_win32(self):
        env = {}
        with mock.patch.object(ps, "current_os", return_value="windows"):
            self.assertEqual(ps.pin_tray_backend(env), "win32")
        self.assertEqual(env, {"PYSTRAY_BACKEND": "win32"})

    def test_windows_respects_an_explicit_override(self):
        env = {"PYSTRAY_BACKEND": "xorg"}
        with mock.patch.object(ps, "current_os", return_value="windows"):
            self.assertEqual(ps.pin_tray_backend(env), "xorg")
        self.assertEqual(env, {"PYSTRAY_BACKEND": "xorg"})

    def test_non_windows_leaves_the_backend_unset(self):
        for system in ("darwin", "linux"):
            with self.subTest(system=system):
                env = {}
                with mock.patch.object(ps, "current_os", return_value=system):
                    self.assertIsNone(ps.pin_tray_backend(env))
                self.assertEqual(env, {})

    def test_defaults_to_the_process_environment(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PYSTRAY_BACKEND", None)
            with mock.patch.object(ps, "current_os", return_value="linux"):
                ps.pin_tray_backend()
            self.assertNotIn("PYSTRAY_BACKEND", os.environ)
            with mock.patch.object(ps, "current_os", return_value="windows"):
                ps.pin_tray_backend()
            self.assertEqual(os.environ.get("PYSTRAY_BACKEND"), "win32")


class LauncherPinTests(unittest.TestCase):
    """The pin must still be applied before ``import pystray`` in the launcher."""

    def test_launcher_pins_before_importing_pystray(self):
        launcher = os.path.join(os.path.dirname(__file__), "..", "snipvoice.pyw")
        with open(launcher, encoding="utf-8") as handle:
            lines = [line.strip() for line in handle]
        pin = next((i for i, line in enumerate(lines) if line.startswith("platform_support.pin_tray_backend(")), None)
        pystray_import = next((i for i, line in enumerate(lines) if line == "import pystray"), None)
        self.assertIsNotNone(pin, "launcher no longer calls pin_tray_backend()")
        self.assertIsNotNone(pystray_import, "launcher no longer imports pystray")
        self.assertLess(pin, pystray_import)
        # No code line may set the variable itself: the platform guard inside
        # pin_tray_backend is what keeps the pin off macOS/Linux.
        code = [line for line in lines if not line.startswith("#")]
        self.assertEqual([line for line in code if "PYSTRAY_BACKEND" in line], [])


class AutostartCommandTests(unittest.TestCase):
    def test_frozen_build_points_at_the_executable(self):
        with mock.patch.object(ps.sys, "frozen", True, create=True), \
                mock.patch.object(ps.sys, "executable", r"C:\App\Snipvoice.exe"):
            self.assertEqual(ps.default_autostart_command(), [r"C:\App\Snipvoice.exe"])

    def test_macos_bundle_points_inside_the_app(self):
        """The LaunchAgent runs the bundle's binary, not ``open -a``.

        Verified against a real PyInstaller ``.app``: ``sys.executable`` is
        ``…/Snipvoice.app/Contents/MacOS/Snipvoice``, launchd starts it,
        and the process still resolves as the bundle (Info.plist honored, so
        ``LSUIElement`` applies and TCC attributes the grants to the bundle).
        ``open -a`` would hand launchd a wrapper that exits immediately.
        """
        binary = "/Applications/Snipvoice.app/Contents/MacOS/Snipvoice"
        with mock.patch.object(ps.sys, "frozen", True, create=True), \
                mock.patch.object(ps.sys, "executable", binary):
            self.assertEqual(ps.default_autostart_command(), [binary])

    def test_source_checkout_points_at_the_launcher(self):
        with mock.patch.object(ps.sys, "frozen", False, create=True):
            argv = ps.default_autostart_command()
        self.assertEqual(len(argv), 2)
        self.assertTrue(argv[1].endswith("snipvoice.pyw"))
        self.assertTrue(os.path.exists(argv[1]))


class DockIconTests(unittest.TestCase):
    """LSUIElement alone does not keep the packaged .app out of the Dock."""

    def test_non_macos_is_a_no_op(self):
        with mock.patch.object(ps, "IS_MAC", False):
            self.assertFalse(ps.hide_dock_icon())

    def test_macos_sets_the_accessory_activation_policy(self):
        appkit = mock.Mock()
        appkit.NSApplicationActivationPolicyAccessory = 1
        shared = appkit.NSApplication.sharedApplication.return_value
        shared.setActivationPolicy_.return_value = True
        with mock.patch.object(ps, "IS_MAC", True), \
                mock.patch.dict(sys.modules, {"AppKit": appkit}):
            self.assertTrue(ps.hide_dock_icon())
        # Aqua Tk set the policy to Regular while creating the root; accessory
        # is what puts it back, and it must be applied to the same shared
        # NSApplication the tray attaches to.
        shared.setActivationPolicy_.assert_called_once_with(1)

    def test_a_refused_policy_is_reported_not_raised(self):
        appkit = mock.Mock()
        appkit.NSApplication.sharedApplication.return_value.setActivationPolicy_.return_value = False
        with mock.patch.object(ps, "IS_MAC", True), \
                mock.patch.dict(sys.modules, {"AppKit": appkit}):
            self.assertFalse(ps.hide_dock_icon())

    def test_an_appkit_failure_never_escapes(self):
        """A Dock icon is cosmetic; it must not take the tray down."""
        appkit = mock.Mock()
        appkit.NSApplication.sharedApplication.side_effect = RuntimeError("no AppKit")
        with mock.patch.object(ps, "IS_MAC", True), \
                mock.patch.dict(sys.modules, {"AppKit": appkit}):
            self.assertFalse(ps.hide_dock_icon())


class FrontmostApplicationTests(unittest.TestCase):
    """Expansion dialogs must return Cmd+V to the app that triggered them."""

    def test_non_macos_capture_and_restore_are_no_ops(self):
        target = mock.Mock()
        with mock.patch.object(ps, "IS_MAC", False):
            self.assertIsNone(ps.capture_frontmost_application())
            self.assertFalse(ps.restore_frontmost_application(target))

    def test_capture_returns_an_external_frontmost_application(self):
        appkit = mock.Mock()
        target = appkit.NSWorkspace.sharedWorkspace.return_value.frontmostApplication.return_value
        target.processIdentifier.return_value = os.getpid() + 1
        with mock.patch.object(ps, "IS_MAC", True), \
                mock.patch.dict(sys.modules, {"AppKit": appkit}):
            self.assertIs(target, ps.capture_frontmost_application())

    def test_capture_ignores_snipvoice_itself(self):
        appkit = mock.Mock()
        target = appkit.NSWorkspace.sharedWorkspace.return_value.frontmostApplication.return_value
        target.processIdentifier.return_value = os.getpid()
        with mock.patch.object(ps, "IS_MAC", True), \
                mock.patch.dict(sys.modules, {"AppKit": appkit}):
            self.assertIsNone(ps.capture_frontmost_application())

    def test_restore_activates_the_captured_application(self):
        appkit = mock.Mock()
        appkit.NSApplicationActivateIgnoringOtherApps = 2
        target = mock.Mock()
        target.activateWithOptions_.return_value = True
        with mock.patch.object(ps, "IS_MAC", True), \
                mock.patch.dict(sys.modules, {"AppKit": appkit}):
            self.assertTrue(ps.restore_frontmost_application(target))
        target.activateWithOptions_.assert_called_once_with(2)

    def test_appkit_failures_never_escape(self):
        appkit = mock.Mock()
        appkit.NSWorkspace.sharedWorkspace.side_effect = RuntimeError("no workspace")
        target = mock.Mock()
        target.activateWithOptions_.side_effect = RuntimeError("not running")
        with mock.patch.object(ps, "IS_MAC", True), \
                mock.patch.dict(sys.modules, {"AppKit": appkit}):
            self.assertIsNone(ps.capture_frontmost_application())
            self.assertFalse(ps.restore_frontmost_application(target))


class TextTargetTests(unittest.TestCase):
    def test_windows_capture_skips_this_process(self):
        user32 = mock.Mock()
        user32.GetForegroundWindow.return_value = 42

        def get_pid(hwnd, out):
            out._obj.value = os.getpid()
            return 0

        user32.GetWindowThreadProcessId.side_effect = get_pid
        with mock.patch.object(ps, "IS_WINDOWS", True), \
                mock.patch.object(ps, "IS_MAC", False), \
                mock.patch.object(ps, "_win32_user32", return_value=user32):
            self.assertIsNone(ps.capture_text_target())

    def test_windows_restore_refuses_a_dead_window(self):
        user32 = mock.Mock()
        user32.IsWindow.return_value = 0
        with mock.patch.object(ps, "IS_WINDOWS", True), \
                mock.patch.object(ps, "IS_MAC", False), \
                mock.patch.object(ps, "_win32_user32", return_value=user32):
            self.assertFalse(ps.restore_text_target(("hwnd", 99)))

    def test_missing_target_is_not_restored(self):
        self.assertFalse(ps.restore_text_target(None))
        self.assertFalse(ps.text_target_is_alive(None))

    def test_macos_restore_text_target_waits_for_workspace_activation(self):
        target = mock.Mock()
        with mock.patch.object(ps, "IS_MAC", True), \
                mock.patch.object(ps, "IS_WINDOWS", False), \
                mock.patch.object(ps, "restore_application_when_ready") as ready:
            def complete(app, on_active, on_failed):
                on_active()
                return lambda: None

            ready.side_effect = complete
            self.assertTrue(ps.restore_text_target(target))
        ready.assert_called_once()
        self.assertIs(ready.call_args.args[0], target)

    def test_macos_restore_text_target_fails_closed_on_timeout(self):
        target = mock.Mock()
        with mock.patch.object(ps, "IS_MAC", True), \
                mock.patch.object(ps, "IS_WINDOWS", False), \
                mock.patch.object(ps, "restore_application_when_ready") as ready:
            ready.return_value = lambda: None
            self.assertFalse(
                ps.wait_for_restored_application(target, timeout_seconds=0.01)
            )


class ApplicationActivationBarrierTests(unittest.TestCase):
    """Accessory dialogs wait for native activation before revealing Tk."""

    def test_non_macos_completes_immediately(self):
        on_active = mock.Mock()
        on_failed = mock.Mock()
        with mock.patch.object(ps, "IS_MAC", False):
            cancel = ps.activate_application_when_ready(on_active, on_failed)
        on_active.assert_called_once_with()
        on_failed.assert_not_called()
        cancel()

    def test_already_active_macos_completes_without_an_observer(self):
        appkit = mock.Mock()
        appkit.NSApplication.sharedApplication.return_value.isActive.return_value = True
        on_active = mock.Mock()
        on_failed = mock.Mock()
        with mock.patch.object(ps, "IS_MAC", True), \
                mock.patch.dict(sys.modules, {"AppKit": appkit}):
            cancel = ps.activate_application_when_ready(on_active, on_failed)
        on_active.assert_called_once_with()
        on_failed.assert_not_called()
        appkit.NSNotificationCenter.defaultCenter.assert_not_called()
        cancel()

    def test_inactive_macos_completes_from_the_activation_notification(self):
        appkit = mock.Mock()
        appkit.NSApplicationActivateAllWindows = 1
        appkit.NSApplicationActivateIgnoringOtherApps = 2
        appkit.NSApplicationDidBecomeActiveNotification = "active"
        app = appkit.NSApplication.sharedApplication.return_value
        app.isActive.return_value = False
        center = appkit.NSNotificationCenter.defaultCenter.return_value
        token = center.addObserverForName_object_queue_usingBlock_.return_value
        current = appkit.NSRunningApplication.currentApplication.return_value
        current.activateWithOptions_.return_value = True
        on_active = mock.Mock()
        on_failed = mock.Mock()
        with mock.patch.object(ps, "IS_MAC", True), \
                mock.patch.dict(sys.modules, {"AppKit": appkit}):
            cancel = ps.activate_application_when_ready(on_active, on_failed)

        on_active.assert_not_called()
        on_failed.assert_not_called()
        current.activateWithOptions_.assert_called_once_with(3)
        callback = center.addObserverForName_object_queue_usingBlock_.call_args.args[3]
        callback(mock.Mock())
        on_active.assert_called_once_with()
        center.removeObserver_.assert_called_once_with(token)
        cancel()

    def test_cancelled_observer_cannot_complete_later(self):
        appkit = mock.Mock()
        appkit.NSApplicationActivateAllWindows = 1
        appkit.NSApplicationActivateIgnoringOtherApps = 2
        app = appkit.NSApplication.sharedApplication.return_value
        app.isActive.return_value = False
        center = appkit.NSNotificationCenter.defaultCenter.return_value
        current = appkit.NSRunningApplication.currentApplication.return_value
        current.activateWithOptions_.return_value = True
        on_active = mock.Mock()
        on_failed = mock.Mock()
        with mock.patch.object(ps, "IS_MAC", True), \
                mock.patch.dict(sys.modules, {"AppKit": appkit}):
            cancel = ps.activate_application_when_ready(on_active, on_failed)
        callback = center.addObserverForName_object_queue_usingBlock_.call_args.args[3]
        cancel()
        callback(mock.Mock())
        on_active.assert_not_called()
        on_failed.assert_not_called()
        center.removeObserver_.assert_called_once()

    def test_refused_activation_fails_instead_of_revealing_unfocused_dialog(self):
        appkit = mock.Mock()
        appkit.NSApplicationActivateAllWindows = 1
        appkit.NSApplicationActivateIgnoringOtherApps = 2
        app = appkit.NSApplication.sharedApplication.return_value
        app.isActive.return_value = False
        center = appkit.NSNotificationCenter.defaultCenter.return_value
        current = appkit.NSRunningApplication.currentApplication.return_value
        current.activateWithOptions_.return_value = False
        on_active = mock.Mock()
        on_failed = mock.Mock()
        with mock.patch.object(ps, "IS_MAC", True), \
                mock.patch.dict(sys.modules, {"AppKit": appkit}):
            cancel = ps.activate_application_when_ready(on_active, on_failed)
        on_active.assert_not_called()
        on_failed.assert_called_once_with("macOS refused to activate Snipvoice")
        center.removeObserver_.assert_called_once()
        cancel()

    def test_appkit_failure_fails_instead_of_bypassing_the_barrier(self):
        appkit = mock.Mock()
        appkit.NSApplication.sharedApplication.side_effect = RuntimeError("no app")
        on_active = mock.Mock()
        on_failed = mock.Mock()
        with mock.patch.object(ps, "IS_MAC", True), \
                mock.patch.dict(sys.modules, {"AppKit": appkit}):
            cancel = ps.activate_application_when_ready(on_active, on_failed)
        on_active.assert_not_called()
        on_failed.assert_called_once_with("Could not activate Snipvoice: no app")
        cancel()

    def test_timeout_fails_without_revealing_the_dialog(self):
        appkit = mock.Mock()
        appkit.NSApplicationActivateAllWindows = 1
        appkit.NSApplicationActivateIgnoringOtherApps = 2
        app = appkit.NSApplication.sharedApplication.return_value
        app.isActive.return_value = False
        current = appkit.NSRunningApplication.currentApplication.return_value
        current.activateWithOptions_.return_value = True
        timer = mock.Mock()
        on_active = mock.Mock()
        on_failed = mock.Mock()
        with mock.patch.object(ps, "IS_MAC", True), \
                mock.patch.dict(sys.modules, {"AppKit": appkit}), \
                mock.patch.object(
                    ps.threading,
                    "Timer",
                    return_value=timer,
                ) as timer_factory:
            cancel = ps.activate_application_when_ready(on_active, on_failed)
        timeout_callback = timer_factory.call_args.args[1]
        timeout_args = timer_factory.call_args.kwargs["args"]
        timeout_callback(*timeout_args)
        on_active.assert_not_called()
        on_failed.assert_called_once_with(timeout_args[0])
        cancel()


class ApplicationRestoreBarrierTests(unittest.TestCase):
    def test_non_macos_completes_immediately(self):
        on_active = mock.Mock()
        on_failed = mock.Mock()
        with mock.patch.object(ps, "IS_MAC", False):
            cancel = ps.restore_application_when_ready(
                mock.Mock(),
                on_active,
                on_failed,
            )
        on_active.assert_called_once_with()
        on_failed.assert_not_called()
        cancel()

    def test_external_app_completes_only_after_workspace_activation(self):
        appkit = mock.Mock()
        appkit.NSApplicationActivateIgnoringOtherApps = 2
        appkit.NSWorkspaceDidActivateApplicationNotification = "activated"
        appkit.NSWorkspaceApplicationKey = "app"
        target = mock.Mock()
        target.processIdentifier.return_value = 42
        target.activateWithOptions_.return_value = True
        workspace = appkit.NSWorkspace.sharedWorkspace.return_value
        frontmost = mock.Mock()
        frontmost.processIdentifier.return_value = 7
        workspace.frontmostApplication.return_value = frontmost
        center = workspace.notificationCenter.return_value
        token = center.addObserverForName_object_queue_usingBlock_.return_value
        on_active = mock.Mock()
        on_failed = mock.Mock()
        with mock.patch.object(ps, "IS_MAC", True), \
                mock.patch.dict(sys.modules, {"AppKit": appkit}):
            cancel = ps.restore_application_when_ready(
                target,
                on_active,
                on_failed,
            )

        on_active.assert_not_called()
        callback = center.addObserverForName_object_queue_usingBlock_.call_args.args[3]
        notification = mock.Mock()
        notification.userInfo.return_value = {"app": target}
        callback(notification)
        on_active.assert_called_once_with()
        on_failed.assert_not_called()
        center.removeObserver_.assert_called_once_with(token)
        cancel()

    def test_refused_restore_reports_failure(self):
        appkit = mock.Mock()
        appkit.NSApplicationActivateIgnoringOtherApps = 2
        target = mock.Mock()
        target.processIdentifier.return_value = 42
        target.activateWithOptions_.return_value = False
        workspace = appkit.NSWorkspace.sharedWorkspace.return_value
        frontmost = mock.Mock()
        frontmost.processIdentifier.return_value = 7
        workspace.frontmostApplication.return_value = frontmost
        on_active = mock.Mock()
        on_failed = mock.Mock()
        with mock.patch.object(ps, "IS_MAC", True), \
                mock.patch.dict(sys.modules, {"AppKit": appkit}):
            cancel = ps.restore_application_when_ready(
                target,
                on_active,
                on_failed,
            )
        on_active.assert_not_called()
        on_failed.assert_called_once_with(
            "macOS refused to return focus to the previous application"
        )
        cancel()


class TkWindowKeyBarrierTests(unittest.TestCase):
    def test_non_macos_focuses_immediately(self):
        dialog = mock.Mock()
        target = mock.Mock()
        on_key = mock.Mock()
        on_failed = mock.Mock()
        with mock.patch.object(ps, "IS_MAC", False):
            cancel = ps.focus_tk_window_when_ready(
                dialog,
                target,
                on_key,
                on_failed,
            )
        target.focus_force.assert_called_once_with()
        on_key.assert_called_once_with()
        on_failed.assert_not_called()
        cancel()

    def test_macos_waits_for_the_exact_native_window_to_become_key(self):
        appkit = mock.Mock()
        appkit.NSWindowDidBecomeKeyNotification = "key"
        objc = mock.Mock()
        native_window = objc.objc_object.return_value
        native_window.canBecomeKeyWindow.return_value = True
        native_window.isKeyWindow.return_value = False
        center = appkit.NSNotificationCenter.defaultCenter.return_value
        token = center.addObserverForName_object_queue_usingBlock_.return_value
        get_nswindow = mock.Mock(return_value=123)
        library = mock.Mock()
        library.Tk_MacOSXGetNSWindowForDrawable = get_nswindow
        dialog = mock.Mock()
        dialog.winfo_id.return_value = 456
        target = mock.Mock()
        on_key = mock.Mock()
        on_failed = mock.Mock()

        with mock.patch.object(ps, "IS_MAC", True), \
                mock.patch.dict(
                    sys.modules,
                    {"AppKit": appkit, "objc": objc},
                ), mock.patch.object(ps.ctypes, "CDLL", return_value=library):
            cancel = ps.focus_tk_window_when_ready(
                dialog,
                target,
                on_key,
                on_failed,
            )

        target.focus_force.assert_called_once_with()
        on_key.assert_not_called()
        on_failed.assert_not_called()
        observer_call = (
            center.addObserverForName_object_queue_usingBlock_.call_args
        )
        self.assertIs(native_window, observer_call.args[1])
        observer_call.args[3](mock.Mock())
        on_key.assert_called_once_with()
        center.removeObserver_.assert_called_once_with(token)
        cancel()

    def test_already_key_window_completes_without_refocusing(self):
        appkit = mock.Mock()
        objc = mock.Mock()
        native_window = objc.objc_object.return_value
        native_window.canBecomeKeyWindow.return_value = True
        native_window.isKeyWindow.return_value = True
        library = mock.Mock()
        library.Tk_MacOSXGetNSWindowForDrawable.return_value = 123
        dialog = mock.Mock()
        target = mock.Mock()
        on_key = mock.Mock()
        on_failed = mock.Mock()

        with mock.patch.object(ps, "IS_MAC", True), \
                mock.patch.dict(
                    sys.modules,
                    {"AppKit": appkit, "objc": objc},
                ), mock.patch.object(ps.ctypes, "CDLL", return_value=library):
            cancel = ps.focus_tk_window_when_ready(
                dialog,
                target,
                on_key,
                on_failed,
            )

        target.focus_force.assert_not_called()
        on_key.assert_called_once_with()
        on_failed.assert_not_called()
        cancel()

    def test_missing_native_window_fails_without_focusing(self):
        appkit = mock.Mock()
        objc = mock.Mock()
        library = mock.Mock()
        library.Tk_MacOSXGetNSWindowForDrawable.return_value = 0
        target = mock.Mock()
        on_key = mock.Mock()
        on_failed = mock.Mock()

        with mock.patch.object(ps, "IS_MAC", True), \
                mock.patch.dict(
                    sys.modules,
                    {"AppKit": appkit, "objc": objc},
                ), mock.patch.object(ps.ctypes, "CDLL", return_value=library):
            cancel = ps.focus_tk_window_when_ready(
                mock.Mock(),
                target,
                on_key,
                on_failed,
            )

        target.focus_force.assert_not_called()
        on_key.assert_not_called()
        on_failed.assert_called_once_with(
            "Tk did not expose a native window for the input dialog"
        )
        cancel()

    def test_key_window_timeout_fails_and_removes_the_observer(self):
        appkit = mock.Mock()
        objc = mock.Mock()
        native_window = objc.objc_object.return_value
        native_window.canBecomeKeyWindow.return_value = True
        native_window.isKeyWindow.return_value = False
        center = appkit.NSNotificationCenter.defaultCenter.return_value
        token = center.addObserverForName_object_queue_usingBlock_.return_value
        library = mock.Mock()
        library.Tk_MacOSXGetNSWindowForDrawable.return_value = 123
        timer = mock.Mock()
        on_key = mock.Mock()
        on_failed = mock.Mock()

        with mock.patch.object(ps, "IS_MAC", True), \
                mock.patch.dict(
                    sys.modules,
                    {"AppKit": appkit, "objc": objc},
                ), mock.patch.object(
                    ps.ctypes,
                    "CDLL",
                    return_value=library,
                ), mock.patch.object(
                    ps.threading,
                    "Timer",
                    return_value=timer,
                ) as timer_factory:
            cancel = ps.focus_tk_window_when_ready(
                mock.Mock(),
                mock.Mock(),
                on_key,
                on_failed,
            )

        timeout_callback = timer_factory.call_args.args[1]
        timeout_args = timer_factory.call_args.kwargs["args"]
        timeout_callback(*timeout_args)
        on_key.assert_not_called()
        on_failed.assert_called_once_with(timeout_args[0])
        center.removeObserver_.assert_called_once_with(token)
        cancel()

    def test_cancel_blocks_a_late_key_window_notification(self):
        appkit = mock.Mock()
        objc = mock.Mock()
        native_window = objc.objc_object.return_value
        native_window.canBecomeKeyWindow.return_value = True
        native_window.isKeyWindow.return_value = False
        center = appkit.NSNotificationCenter.defaultCenter.return_value
        library = mock.Mock()
        library.Tk_MacOSXGetNSWindowForDrawable.return_value = 123
        timer = mock.Mock()
        on_key = mock.Mock()
        on_failed = mock.Mock()

        with mock.patch.object(ps, "IS_MAC", True), \
                mock.patch.dict(
                    sys.modules,
                    {"AppKit": appkit, "objc": objc},
                ), mock.patch.object(
                    ps.ctypes,
                    "CDLL",
                    return_value=library,
                ), mock.patch.object(ps.threading, "Timer", return_value=timer):
            cancel = ps.focus_tk_window_when_ready(
                mock.Mock(),
                mock.Mock(),
                on_key,
                on_failed,
            )

        callback = center.addObserverForName_object_queue_usingBlock_.call_args.args[3]
        cancel()
        callback(mock.Mock())
        on_key.assert_not_called()
        on_failed.assert_not_called()
        center.removeObserver_.assert_called_once()
        timer.cancel.assert_called_once_with()


class TkMainThreadSeamTests(unittest.TestCase):
    """The macOS tray + Tk threading seam (issue #24)."""

    def test_only_macos_moves_tk_to_the_main_thread(self):
        self.assertEqual(ps.tk_runs_on_main_thread(), ps.IS_MAC)
        with mock.patch.object(ps, "IS_MAC", True):
            self.assertTrue(ps.tk_runs_on_main_thread())
        with mock.patch.object(ps, "IS_MAC", False):
            self.assertFalse(ps.tk_runs_on_main_thread())


class InsertionTimingTests(unittest.TestCase):
    """Per-OS paste/erase delays with settings.json overrides (issue #27)."""

    def test_every_platform_defines_every_key(self):
        for system in ("windows", "darwin", "linux"):
            timings = ps.default_insertion_timings(system)
            self.assertEqual(set(timings), set(ps.INSERTION_TIMING_KEYS), system)
            for key, value in timings.items():
                self.assertIsInstance(value, float, f"{system}.{key}")

    def test_windows_timings_are_the_historical_constants(self):
        # These are the values the paste path shipped with; a macOS retune must
        # not move them.
        self.assertEqual(
            {
                "clipboard_settle_delay": 0.05,
                "paste_restore_delay": 0.12,
                "erase_key_delay": 0.01,
            },
            ps.default_insertion_timings("windows"),
        )

    def test_macos_settles_faster_than_windows(self):
        # pbcopy/osascript exit only once NSPasteboard holds the payload, so the
        # Windows settle margin is latency macOS does not need.
        self.assertLess(
            ps.default_insertion_timings("darwin")["clipboard_settle_delay"],
            ps.default_insertion_timings("windows")["clipboard_settle_delay"],
        )

    def test_unknown_platform_falls_back_instead_of_raising(self):
        self.assertEqual(
            ps.default_insertion_timings("haiku"), ps.default_insertion_timings("linux")
        )

    def test_settings_override_a_single_key_and_leave_the_rest(self):
        timings = ps.insertion_timings({"paste_restore_delay": 0.3}, system="darwin")
        self.assertEqual(0.3, timings["paste_restore_delay"])
        self.assertEqual(
            ps.default_insertion_timings("darwin")["erase_key_delay"],
            timings["erase_key_delay"],
        )

    def test_unrelated_settings_keys_are_ignored(self):
        self.assertEqual(
            ps.default_insertion_timings("windows"),
            ps.insertion_timings({"terminator_mode": True, "bcb_timeout": 3}, system="windows"),
        )

    def test_bad_overrides_fall_back_to_the_platform_default(self):
        # A delay written in milliseconds, a negative, a string and a bool would
        # each freeze or break the listener thread if they reached it.
        for bad in (500, -0.1, "0.2", True, None, [0.2]):
            with self.subTest(bad=bad):
                timings = ps.insertion_timings({"erase_key_delay": bad}, system="windows")
                self.assertEqual(0.01, timings["erase_key_delay"])
                self.assertEqual(["erase_key_delay"], ps.invalid_timing_overrides({"erase_key_delay": bad}))

    def test_zero_is_a_valid_delay(self):
        timings = ps.insertion_timings({"erase_key_delay": 0}, system="windows")
        self.assertEqual(0.0, timings["erase_key_delay"])
        self.assertEqual([], ps.invalid_timing_overrides({"erase_key_delay": 0}))

    def test_no_settings_reports_no_invalid_overrides(self):
        self.assertEqual([], ps.invalid_timing_overrides(None))
        self.assertEqual([], ps.invalid_timing_overrides({}))


class TrayIconOptionTests(unittest.TestCase):
    """The pystray/NSApplication handoff on macOS (issue #24)."""

    def test_non_macos_adds_no_icon_options(self):
        """Windows must keep constructing the icon exactly as it always has."""
        with mock.patch.object(ps, "IS_MAC", False):
            self.assertEqual(ps.tray_icon_options(), {})

    def test_macos_hands_pystray_the_shared_nsapplication(self):
        appkit = mock.Mock()
        shared = object()
        appkit.NSApplication.sharedApplication.return_value = shared
        with mock.patch.object(ps, "IS_MAC", True), \
                mock.patch.dict(sys.modules, {"AppKit": appkit}):
            options = ps.tray_icon_options()
        # The singleton, not a fresh instance: Tk already created the one the
        # process is allowed to have, and pystray has to attach to that loop.
        self.assertEqual(options, {"darwin_nsapplication": shared})
        appkit.NSApplication.sharedApplication.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
