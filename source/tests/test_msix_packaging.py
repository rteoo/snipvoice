import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET

from PIL import Image


ROOT = Path(__file__).resolve().parents[2]
TMP = Path(__file__).resolve().parent / "tmp"
TMP.mkdir(exist_ok=True)
FOUNDATION = "{http://schemas.microsoft.com/appx/manifest/foundation/windows10}"
DESKTOP = "{http://schemas.microsoft.com/appx/manifest/desktop/windows10}"

spec = importlib.util.spec_from_file_location("build_msix", ROOT / "packaging" / "build_msix.py")
build_msix = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_msix)


def render():
    return build_msix.render_manifest(
        "Example.Snipvoice", "CN=00000000-0000-0000-0000-000000000000", "Example & Co", "3.4.0.0",
    )


class MsixManifestTests(unittest.TestCase):
    def test_version_follows_source_with_store_reserved_zero(self):
        self.assertRegex(build_msix.source_version(), r"^\d+\.\d+\.\d+\.0$")

    def test_manifest_is_wellformed_and_escaped(self):
        root = ET.fromstring(render())
        identity = root.find(f"{FOUNDATION}Identity")
        self.assertEqual(identity.get("Name"), "Example.Snipvoice")
        self.assertEqual(identity.get("Version"), "3.4.0.0")
        self.assertEqual(root.find(f"{FOUNDATION}Properties/{FOUNDATION}PublisherDisplayName").text,
                         "Example & Co")

    def test_display_name_matches_the_store_reservation(self):
        # Partner Center rejects a package whose DisplayName differs from the reserved name.
        root = ET.fromstring(render())
        self.assertEqual(root.find(f"{FOUNDATION}Properties/{FOUNDATION}DisplayName").text, "SnipVoice")
        names = {element.get("DisplayName") for element in root.iter() if element.get("DisplayName")}
        self.assertEqual(names, {"SnipVoice"})

    def test_manifest_declares_microphone_and_opt_in_startup_task(self):
        root = ET.fromstring(render())
        devices = [cap.get("Name") for cap in root.iter(f"{FOUNDATION}DeviceCapability")]
        self.assertEqual(devices, ["microphone"])
        task = next(root.iter(f"{DESKTOP}StartupTask"))
        self.assertEqual(task.get("Enabled"), "false")
        application = next(root.iter(f"{FOUNDATION}Application"))
        self.assertEqual(application.get("Executable"), "Snipvoice.exe")

    def test_manifest_rejects_invalid_identity(self):
        with self.assertRaises(ValueError):
            build_msix.render_manifest("bad name", "CN=x", "Example", "1.0.0.0")
        with self.assertRaises(ValueError):
            build_msix.render_manifest("Example.Snipvoice", "O=x", "Example", "1.0.0.0")


class MsixLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TMP)
        self.addCleanup(self.temp.cleanup)
        self.dist = os.path.join(self.temp.name, "dist")
        os.makedirs(self.dist)

    def test_layout_requires_the_built_executable(self):
        with self.assertRaises(FileNotFoundError):
            build_msix.stage_layout(self.dist, os.path.join(self.temp.name, "layout"), render())

    def test_layout_holds_app_manifest_and_store_assets(self):
        Path(self.dist, "Snipvoice.exe").write_bytes(b"")
        layout = os.path.join(self.temp.name, "layout")
        build_msix.stage_layout(self.dist, layout, render())

        self.assertTrue(os.path.isfile(os.path.join(layout, "Snipvoice.exe")))
        self.assertTrue(os.path.isfile(os.path.join(layout, "AppxManifest.xml")))
        for filename, size in build_msix.ASSETS.items():
            with Image.open(os.path.join(layout, "Assets", filename)) as image:
                self.assertEqual(image.size, size)
        with self.assertRaises(FileExistsError):
            build_msix.stage_layout(self.dist, layout, render())


if __name__ == "__main__":
    unittest.main()
