"""Packaging of the Tauri manager window: release scripts, notices and the packaged probe.

The probe runs against a fake window process over real OS pipes; reads wait
with timeouts, never bare sleeps, and every pipe is closed.
"""

import importlib.util
import json
import os
import sys
import threading
import unittest
from unittest import mock

SOURCE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ROOT = os.path.dirname(SOURCE_DIR)
sys.path.insert(0, SOURCE_DIR)

import manager_window_probe

WAIT = 5.0


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


def _notices_module():
    spec = importlib.util.spec_from_file_location("manager_notices", os.path.join(ROOT, "packaging", "manager_notices.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReleaseScriptTests(unittest.TestCase):
    def test_windows_release_builds_bundles_and_probes_the_window(self):
        text = _read("build_release.bat")
        build = text.index('call "%REPO_DIR%\\desktop\\build_manager.bat"')
        self.assertLess(build, text.index("python -m PyInstaller"))
        self.assertIn('--add-binary "%REPO_DIR%\\desktop\\src-tauri\\target\\release\\snipvoice-manager.exe;."', text)
        self.assertIn('--add-data "%REPO_DIR%\\build\\manager-notices.txt;THIRD_PARTY_LICENSES/manager-window"', text)
        probe = text.index('"%STAGING_DIR%\\Snipvoice.exe" --manager-window-probe')
        self.assertLess(text.index("python -m PyInstaller"), probe)
        self.assertLess(probe, text.index("call :promote_staged_release"))

    def test_window_build_installs_only_the_lockfile_and_writes_notices(self):
        text = _read("desktop", "build_manager.bat")
        self.assertIn("npm.cmd ci", text)
        self.assertNotIn("npm.cmd install", text)
        self.assertIn("npm.cmd run tauri build", text)
        self.assertIn("packaging\\manager_notices.py", text)

    def test_bundles_run_for_window_changes_and_record_toolchains(self):
        text = _read(".github", "workflows", "bundles.yml")
        for path in ('"desktop/**"', '"source/manager_bridge.py"', '"source/manager_window_probe.py"',
                     '"source/tests/test_manager_packaging.py"'):
            self.assertIn(path, text)
        self.assertIn("cargo --version", text)

    def test_the_probe_runs_before_desktop_imports(self):
        text = _read("source", "snipvoice.pyw")
        self.assertLess(text.index("run_manager_window_probe_if_requested()\n"), text.index("import platform_support"))


class NoticeTests(unittest.TestCase):
    def setUp(self):
        self.notices = _notices_module()

    def test_license_expressions(self):
        cases = {
            "MIT OR Apache-2.0": True, "Apache-2.0/MIT": True, "(MIT OR Apache-2.0) AND Unicode-3.0": True,
            "MPL-2.0": True, "LGPL-2.1-or-later OR MIT": True, "GPL-3.0-only": False,
            "MIT AND GPL-2.0-only": False, "Apache-2.0 WITH LLVM-exception": True,
            "GPL-2.0-only WITH Classpath-exception-2.0": False, "MIT WITH Unknown-exception": False,
        }
        for expression, allowed in cases.items():
            with self.subTest(expression=expression):
                self.assertIs(self.notices.license_allowed(expression), allowed)
        for broken in ("(MIT OR Apache-2.0", "MIT OR", "MIT Apache-2.0"):
            with self.subTest(expression=broken), self.assertRaises(self.notices.NoticeError):
                self.notices.license_allowed(broken)

    def test_undeclared_or_refused_licenses_fail_the_build(self):
        with self.assertRaises(self.notices.NoticeError) as caught:
            self.notices.check([{"name": "a", "version": "1", "license": ""},
                                {"name": "b", "version": "2", "license": "GPL-3.0-only"},
                                {"name": "c", "version": "3", "license": "MIT"}])
        self.assertIn("a 1", str(caught.exception))
        self.assertIn("b 2", str(caught.exception))
        self.assertNotIn("c 3", str(caught.exception))

    def test_entries_without_license_files_say_where_the_text_is(self):
        text = self.notices.render([{"name": "crate", "version": "1.0", "license": "MIT", "source": "https://example.invalid/crate", "texts": []}], [])
        self.assertIn("--- crate 1.0", text)
        self.assertIn("ships no license file", text)


class FakeWindow:
    """A manager process over real pipes; it greets the engine when told to."""

    def __init__(self, greet):
        to_engine_read, self._to_engine = os.pipe()
        self._from_engine, from_engine_write = os.pipe()
        self.stdout = os.fdopen(to_engine_read, "rb", buffering=0)
        self.stdin = os.fdopen(from_engine_write, "wb", buffering=0)
        self.exited = threading.Event()
        self.reader = threading.Thread(target=self._run, args=(greet,), daemon=True)
        self.reader.start()

    def _run(self, greet):
        out = os.fdopen(self._to_engine, "wb", buffering=0)
        incoming = os.fdopen(self._from_engine, "rb", buffering=0)
        with out, incoming:
            if greet:
                out.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "app.hello", "params": {}}).encode() + b"\n")
            # The window exits when the engine closes its input.
            while incoming.readline():
                pass
        self.exited.set()

    def wait(self, timeout=None):
        if not self.exited.wait(timeout):
            raise manager_window_probe.subprocess.TimeoutExpired("fake", timeout)
        return 0

    def kill(self):
        self.exited.set()


class ProbeTests(unittest.TestCase):
    def run_probe(self, window, timeout=WAIT):
        with mock.patch.object(manager_window_probe.subprocess, "Popen", return_value=window), \
                mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            return manager_window_probe.main("snipvoice-manager.exe", timeout=timeout)

    def test_a_window_that_greets_passes_and_exits(self):
        window = FakeWindow(greet=True)
        self.assertEqual(self.run_probe(window), 0)
        self.assertTrue(window.exited.is_set())

    def test_a_silent_window_fails(self):
        window = FakeWindow(greet=False)
        self.assertEqual(self.run_probe(window, timeout=0.2), 1)
        self.assertTrue(window.exited.wait(WAIT))

    def test_a_missing_window_fails(self):
        with mock.patch.object(manager_window_probe, "manager_executable", return_value=None), \
                mock.patch("sys.stderr"):
            self.assertEqual(manager_window_probe.main(), 1)


if __name__ == "__main__":
    unittest.main()
