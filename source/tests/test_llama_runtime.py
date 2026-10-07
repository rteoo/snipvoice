"""Custom native wheel provenance, inventory, and packaging failure contracts."""

import base64
import csv
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock
import zipfile

import embedding_runtime_probe
import llama_runtime


TMP_ROOT = Path(__file__).parent / "tmp"
REPO = Path(__file__).resolve().parents[2]
specification = importlib.util.spec_from_file_location("llama_wheel_recipe", REPO / "packaging/llama_runtime.py")
recipe = importlib.util.module_from_spec(specification)
specification.loader.exec_module(recipe)


class LlamaRuntimeTests(unittest.TestCase):
    def setUp(self):
        TMP_ROOT.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=TMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.package = self.root / "llama_cpp"
        (self.package / "lib").mkdir(parents=True)
        self.library = self.package / "lib/llama.dll"
        self.library.write_bytes(b"synthetic native library")
        self.manifest = {"schema": 1, "wrapper_version": llama_runtime.WRAPPER_VERSION,
                         "wrapper_revision": llama_runtime.WRAPPER_REVISION,
                         "native_revision": llama_runtime.NATIVE_REVISION,
                         "libraries": {"lib/llama.dll": hashlib.sha256(self.library.read_bytes()).hexdigest()}}
        self.write_manifest()

    def write_manifest(self):
        (self.package / llama_runtime.MANIFEST_NAME).write_text(json.dumps(self.manifest), encoding="utf-8")

    def test_approved_identity_and_native_hash_pass(self):
        self.assertEqual(llama_runtime.verify_manifest(self.package), self.manifest)
        self.assertEqual(recipe.NATIVE_REVISION, llama_runtime.NATIVE_REVISION)
        self.assertEqual(recipe.WRAPPER_REVISION, llama_runtime.WRAPPER_REVISION)
        self.assertEqual(recipe.WRAPPER_VERSION, llama_runtime.WRAPPER_VERSION)

    def test_unapproved_wrapper_native_and_schema_fail(self):
        for key, value in (("wrapper_version", "upstream"), ("native_revision", "old-native"),
                           ("wrapper_revision", "old-wrapper"), ("schema", 2)):
            with self.subTest(key=key):
                original = self.manifest[key]
                self.manifest[key] = value
                self.write_manifest()
                with self.assertRaises(RuntimeError):
                    llama_runtime.verify_manifest(self.package)
                self.manifest[key] = original

    def test_inventory_rejects_escapes_absolute_paths_and_bad_hashes(self):
        digest = "0" * 64
        for libraries in ({}, {"../outside.dll": digest}, {"/outside.dll": digest},
                          {"lib\\outside.dll": digest}, {"lib/llama.dll": "invalid"}):
            with self.subTest(libraries=libraries):
                self.manifest["libraries"] = libraries
                self.write_manifest()
                with self.assertRaises(RuntimeError):
                    llama_runtime.verify_manifest(self.package)

    def test_source_preflight_rejects_modified_library_and_frozen_inventory_remains_required(self):
        self.library.write_bytes(b"changed synthetic native library")
        with self.assertRaises(RuntimeError):
            llama_runtime.verify_manifest(self.package)
        llama_runtime.verify_manifest(self.package, check_hashes=False)
        with mock.patch.object(Path, "is_file", return_value=False):
            with self.assertRaises(RuntimeError):
                llama_runtime.verify_manifest(self.package, check_hashes=False)

    def test_macos_frozen_manifest_uses_sealed_resources_and_framework_libraries(self):
        contents = self.root / "Synthetic.app/Contents"
        executable = contents / "MacOS/Synthetic"
        executable.parent.mkdir(parents=True)
        executable.touch()
        framework = contents / "Frameworks/llama_cpp"
        resources = contents / "Resources/llama_cpp"
        (framework / "lib").mkdir(parents=True)
        resources.mkdir(parents=True)
        (framework / "lib/llama.dll").write_bytes(self.library.read_bytes())
        (resources / llama_runtime.MANIFEST_NAME).write_text(json.dumps(self.manifest), encoding="utf-8")
        with mock.patch.object(llama_runtime.sys, "frozen", True, create=True), \
             mock.patch.object(llama_runtime.sys, "platform", "darwin"), \
             mock.patch.object(llama_runtime.sys, "executable", str(executable)):
            manifest_root = llama_runtime._macos_manifest_root(framework)
            self.assertEqual(manifest_root, resources)
            self.assertEqual(llama_runtime.verify_manifest(framework, check_hashes=False,
                                                           manifest_root=manifest_root), self.manifest)
            with self.assertRaisesRegex(RuntimeError, "bundle layout"):
                llama_runtime._macos_manifest_root(self.package)
        with self.assertRaises(FileNotFoundError):
            llama_runtime.verify_manifest(framework)

    def test_source_and_windows_frozen_runtime_keep_package_manifest(self):
        for frozen, platform in ((False, "darwin"), (True, "win32")):
            with mock.patch.object(llama_runtime.sys, "frozen", frozen, create=True), \
                 mock.patch.object(llama_runtime.sys, "platform", platform):
                self.assertIsNone(llama_runtime._macos_manifest_root(self.package))
        with mock.patch.object(Path, "is_symlink", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "Invalid custom llama.cpp runtime manifest"):
                llama_runtime.verify_manifest(self.package, check_hashes=False)

    def test_source_cache_hash_is_verified_on_every_build(self):
        archive = self.root / "source.tar.gz"
        archive.write_bytes(b"synthetic archive")
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        with mock.patch.object(recipe.urllib.request, "urlopen") as network:
            recipe.acquire("https://example.invalid/source", archive, digest)
            network.assert_not_called()
            with self.assertRaises(RuntimeError):
                recipe.acquire("https://example.invalid/source", archive, "0" * 64)

    def test_source_archive_cannot_escape_build_directory(self):
        archive = self.root / "source.tar.gz"
        with tarfile.open(archive, "w:gz") as handle:
            item = tarfile.TarInfo("source/../../outside")
            item.size = 1
            handle.addfile(item, io.BytesIO(b"x"))
        with self.assertRaises(tarfile.FilterError):
            recipe.extract(archive, self.root / "extract")
        self.assertFalse((self.root / "outside").exists())

    def test_failed_source_download_leaves_no_cache_or_partial(self):
        destination = self.root / "new-source.tar.gz"
        with mock.patch.object(recipe.urllib.request, "urlopen", return_value=io.BytesIO(b"synthetic wrong source")):
            with self.assertRaises(RuntimeError):
                recipe.acquire("https://example.invalid/source", destination, "0" * 64)
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.root.iterdir()), [self.package])

    def windows_toolchain(self, version, *, bundled=False, generators=None):
        vswhere = self.root / "Microsoft Visual Studio/Installer/vswhere.exe"
        vswhere.parent.mkdir(parents=True, exist_ok=True)
        vswhere.touch()
        installation = self.root / "Visual Studio"
        cmake = installation / "Common7/IDE/CommonExtensions/Microsoft/CMake/CMake/bin/cmake.exe"
        if bundled:
            cmake.parent.mkdir(parents=True, exist_ok=True)
            cmake.touch()
        installed = [{"installationPath": str(installation), "installationVersion": version}]
        capabilities = {"generators": [{"name": name} for name in (generators or [
            "Visual Studio 17 2022", "Visual Studio 18 2026", "Ninja"])]}
        calls = [json.dumps(installed), json.dumps(capabilities)]
        lookup = [None, str(cmake)] if bundled else ["cmake.exe", "cmake.exe"]
        with mock.patch.dict(recipe.os.environ, {"ProgramFiles(x86)": str(self.root), "PATH": "synthetic-path",
                                                "CMAKE_GENERATOR": "Visual Studio 17 2022",
                                                "CMAKE_GENERATOR_INSTANCE": "stale-instance"}, clear=True), \
             mock.patch.object(recipe.platform, "system", return_value="Windows"), \
             mock.patch.object(recipe.shutil, "which", side_effect=lookup), \
             mock.patch.object(recipe.subprocess, "check_output", side_effect=calls) as output:
            environment = recipe.build_environment()
        self.assertIn("Microsoft.VisualStudio.Component.VC.Tools.x86.x64", output.call_args_list[0].args[0])
        self.assertEqual(output.call_args_list[1].args[0][-2:], ["-E", "capabilities"])
        self.assertEqual(environment["CMAKE_GENERATOR_INSTANCE"], str(installation))
        self.assertEqual(environment["CMAKE_ARGS"], recipe.CMAKE_ARGS)
        if bundled:
            self.assertEqual(environment["PATH"], str(cmake.parent) + recipe.os.pathsep + "synthetic-path")
        else:
            self.assertEqual(environment["PATH"], "synthetic-path")
        return environment

    def test_windows_2026_compiler_matches_generator_with_cmake_on_path(self):
        self.assertEqual(self.windows_toolchain("18.1.0")["CMAKE_GENERATOR"], "Visual Studio 18 2026")

    def test_windows_2022_compiler_uses_existing_bundled_cmake(self):
        self.assertEqual(self.windows_toolchain("17.14.0", bundled=True)["CMAKE_GENERATOR"],
                         "Visual Studio 17 2022")

    def test_windows_compiler_requires_matching_cmake_generator(self):
        with self.assertRaisesRegex(RuntimeError, "does not support installed Visual Studio major 18"):
            self.windows_toolchain("18.1.0", generators=["Visual Studio 17 2022", "Ninja"])

    def test_windows_missing_cpp_toolchain_fails_before_build(self):
        vswhere = self.root / "Microsoft Visual Studio/Installer/vswhere.exe"
        vswhere.parent.mkdir(parents=True)
        vswhere.touch()
        with mock.patch.dict(recipe.os.environ, {"ProgramFiles(x86)": str(self.root)}, clear=True), \
             mock.patch.object(recipe.platform, "system", return_value="Windows"), \
             mock.patch.object(recipe.subprocess, "check_output", return_value="[]"):
            with self.assertRaisesRegex(RuntimeError, "with MSVC C\\+\\+ tools is required"):
                recipe.build_environment()

    def test_non_windows_keeps_native_generator_and_requires_existing_cmake(self):
        with mock.patch.dict(recipe.os.environ, {"PATH": "synthetic-path"}, clear=True), \
             mock.patch.object(recipe.platform, "system", return_value="Darwin"), \
             mock.patch.object(recipe.shutil, "which", return_value="cmake"), \
             mock.patch.object(recipe.subprocess, "check_output") as output:
            self.assertNotIn("CMAKE_GENERATOR", recipe.build_environment())
            output.assert_not_called()
        with mock.patch.object(recipe.platform, "system", return_value="Linux"), \
             mock.patch.object(recipe.shutil, "which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "CMake installation is required"):
                recipe.build_environment()

    def test_sealed_wheel_record_covers_provenance_license_and_native_files(self):
        wheel = self.root / "llama_cpp_python.whl"
        license_path = self.root / "LICENSE"
        license_path.write_text("Synthetic license fixture", encoding="ascii")
        record = "llama_cpp_python-0.3.36.dist-info/RECORD"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("llama_cpp/lib/llama.dll", b"synthetic native library")
            archive.writestr("llama_cpp/__init__.py", b"synthetic")
            archive.writestr(record, b"")
        manifest = recipe.seal_wheel(wheel, license_path)
        with zipfile.ZipFile(wheel) as archive:
            archive.extractall(self.root / "installed")
            rows = list(csv.reader(io.StringIO(archive.read(record).decode())))
            self.assertEqual({row[0] for row in rows}, set(archive.namelist()))
            for name, digest, size in rows:
                if name == record:
                    self.assertEqual((digest, size), ("", ""))
                    continue
                value = archive.read(name)
                expected = base64.urlsafe_b64encode(hashlib.sha256(value).digest()).rstrip(b"=").decode()
                self.assertEqual(digest, "sha256=" + expected)
                self.assertEqual(int(size), len(value))
        self.assertEqual(llama_runtime.verify_manifest(self.root / "installed/llama_cpp"), manifest)

    def test_wheel_without_native_library_is_rejected(self):
        wheel = self.root / "invalid.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("llama_cpp_python.dist-info/RECORD", b"")
        with self.assertRaises(RuntimeError):
            recipe.seal_wheel(wheel, self.root / "not-read")

    def test_probe_reports_failure_without_native_details(self):
        output = io.StringIO()
        with mock.patch.object(embedding_runtime_probe, "probe_embedding_runtime", side_effect=RuntimeError("synthetic-private-detail")), \
             mock.patch("sys.stdout", output):
            self.assertEqual(embedding_runtime_probe.main(), 1)
        self.assertNotIn("synthetic-private-detail", output.getvalue())

    def test_release_and_ci_use_recipe_and_probes_before_promotion(self):
        bundles = (REPO / ".github/workflows/bundles.yml").read_text("utf-8")
        self.assertEqual(bundles.count("python packaging/llama_runtime.py --work-dir"), 2)
        ci = (REPO / ".github/workflows/ci.yml").read_text("utf-8")
        self.assertIn("python packaging/llama_runtime.py --work-dir", ci)
        for name, promotion in (("build_release.bat", "call :promote_staged_release"),
                                ("build_release_macos.sh", "# --- Promote")):
            text = (REPO / name).read_text("utf-8")
            self.assertLess(text.index("--embedding-runtime-probe"), text.index(promotion))
        entrypoint = (REPO / "source/snipvoice.pyw").read_text("utf-8")
        self.assertLess(entrypoint.index('if "--embedding-runtime-probe"'), entrypoint.index("import platform_support"))
