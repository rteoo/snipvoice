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
