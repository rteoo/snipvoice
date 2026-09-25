"""Pack the Windows PyInstaller build into an MSIX for Microsoft Store submission.

Run after ``build_release.bat`` has produced ``dist\\Snipvoice``. The package
identity comes from Partner Center (Product identity page) and is passed in
explicitly; the Store re-signs the upload, so no code-signing certificate is
involved. ``makeappx.exe`` ships with the Windows SDK.
"""

import argparse
import glob
import os
import re
import shutil
import subprocess
import sys
from xml.sax.saxutils import escape, quoteattr

from PIL import Image

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SOURCE_ENTRY = os.path.join(ROOT, "source", "snipvoice.pyw")
ICON_SOURCE = os.path.join(ROOT, "source", "snipvoice-icon.png")
EXECUTABLE = "Snipvoice.exe"
STARTUP_TASK_ID = "SnipvoiceStartup"
DESCRIPTION = "Ditado por voz local e gravador de reuniões."

# Store-required tile and logo assets, at their 100% scale sizes.
ASSETS = {
    "StoreLogo.png": (50, 50),
    "Square44x44Logo.png": (44, 44),
    "Square150x150Logo.png": (150, 150),
}

IDENTITY_NAME_PATTERN = re.compile(r"^[A-Za-z0-9.-]{3,50}$")

MANIFEST_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<Package
  xmlns="http://schemas.microsoft.com/appx/manifest/foundation/windows10"
  xmlns:uap="http://schemas.microsoft.com/appx/manifest/uap/windows10"
  xmlns:desktop="http://schemas.microsoft.com/appx/manifest/desktop/windows10"
  xmlns:rescap="http://schemas.microsoft.com/appx/manifest/foundation/windows10/restrictedcapabilities"
  IgnorableNamespaces="uap desktop rescap">
  <Identity Name={name} Publisher={publisher} Version={version} ProcessorArchitecture="x64" />
  <Properties>
    <DisplayName>SnipVoice</DisplayName>
    <PublisherDisplayName>{publisher_display_name}</PublisherDisplayName>
    <Logo>Assets\\StoreLogo.png</Logo>
  </Properties>
  <Dependencies>
    <TargetDeviceFamily Name="Windows.Desktop" MinVersion="10.0.17763.0" MaxVersionTested="10.0.26100.0" />
  </Dependencies>
  <Resources>
    <Resource Language="pt-BR" />
  </Resources>
  <Applications>
    <Application Id="Snipvoice" Executable="{executable}" EntryPoint="Windows.FullTrustApplication">
      <uap:VisualElements
        DisplayName="SnipVoice"
        Description={description}
        BackgroundColor="transparent"
        Square150x150Logo="Assets\\Square150x150Logo.png"
        Square44x44Logo="Assets\\Square44x44Logo.png" />
      <Extensions>
        <desktop:Extension Category="windows.startupTask" Executable="{executable}" EntryPoint="Windows.FullTrustApplication">
          <desktop:StartupTask TaskId="{startup_task_id}" Enabled="false" DisplayName="SnipVoice" />
        </desktop:Extension>
      </Extensions>
    </Application>
  </Applications>
  <Capabilities>
    <rescap:Capability Name="runFullTrust" />
    <DeviceCapability Name="microphone" />
  </Capabilities>
</Package>
"""


def source_version():
    with open(SOURCE_ENTRY, encoding="utf-8") as handle:
        match = re.search(r'^APP_VERSION = "(\d+)\.(\d+)\.(\d+)"$', handle.read(), re.M)
    if not match:
        raise ValueError("APP_VERSION not found in source/snipvoice.pyw")
    # The Store reserves the fourth field; submissions must leave it at 0.
    return ".".join(match.groups()) + ".0"


def render_manifest(identity_name, publisher, publisher_display_name, version):
    if not IDENTITY_NAME_PATTERN.match(identity_name):
        raise ValueError(f"Invalid package identity name: {identity_name!r}")
    if not publisher.startswith("CN="):
        raise ValueError("Publisher must be the Partner Center 'CN=...' value")
    if not publisher_display_name.strip():
        raise ValueError("Publisher display name is required")
    return MANIFEST_TEMPLATE.format(
        name=quoteattr(identity_name),
        publisher=quoteattr(publisher),
        version=quoteattr(version),
        publisher_display_name=escape(publisher_display_name),
        description=quoteattr(DESCRIPTION),
        executable=EXECUTABLE,
        startup_task_id=STARTUP_TASK_ID,
    )


def write_assets(assets_dir):
    os.makedirs(assets_dir, exist_ok=True)
    with Image.open(ICON_SOURCE) as icon:
        icon = icon.convert("RGBA")
        for filename, size in ASSETS.items():
            icon.resize(size, Image.LANCZOS).save(os.path.join(assets_dir, filename))


def stage_layout(dist_dir, layout_dir, manifest):
    if not os.path.isfile(os.path.join(dist_dir, EXECUTABLE)):
        raise FileNotFoundError(f"{EXECUTABLE} not found in {dist_dir}; run build_release.bat first")
    if os.path.exists(layout_dir):
        raise FileExistsError(f"Layout directory already exists: {layout_dir}")
    shutil.copytree(dist_dir, layout_dir)
    write_assets(os.path.join(layout_dir, "Assets"))
    with open(os.path.join(layout_dir, "AppxManifest.xml"), "w", encoding="utf-8") as handle:
        handle.write(manifest)


def find_makeappx():
    explicit = os.environ.get("SNIPVOICE_MAKEAPPX")
    if explicit:
        return explicit
    on_path = shutil.which("makeappx.exe")
    if on_path:
        return on_path
    kits = os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                        "Windows Kits", "10", "bin")
    candidates = sorted(glob.glob(os.path.join(kits, "10.*", "x64", "makeappx.exe")))
    if not candidates:
        raise FileNotFoundError("makeappx.exe not found; install the Windows SDK or set SNIPVOICE_MAKEAPPX")
    return candidates[-1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--identity-name", default=os.environ.get("SNIPVOICE_MSIX_IDENTITY_NAME"))
    parser.add_argument("--publisher", default=os.environ.get("SNIPVOICE_MSIX_PUBLISHER"))
    parser.add_argument("--publisher-display-name",
                        default=os.environ.get("SNIPVOICE_MSIX_PUBLISHER_DISPLAY_NAME"))
    parser.add_argument("--dist-dir", default=os.path.join(ROOT, "dist", "Snipvoice"))
    parser.add_argument("--work-dir", default=os.path.join(ROOT, "build", "msix"))
    parser.add_argument("--output-dir", default=os.path.join(ROOT, "dist", "msix"))
    args = parser.parse_args(argv)
    for name in ("identity_name", "publisher", "publisher_display_name"):
        if not getattr(args, name):
            parser.error(f"--{name.replace('_', '-')} is required (Partner Center > Product identity)")

    version = source_version()
    manifest = render_manifest(args.identity_name, args.publisher, args.publisher_display_name, version)
    layout_dir = os.path.join(args.work_dir, "layout")
    stage_layout(args.dist_dir, layout_dir, manifest)

    os.makedirs(args.output_dir, exist_ok=True)
    package = os.path.join(args.output_dir, f"Snipvoice-{version}-x64.msix")
    subprocess.run([find_makeappx(), "pack", "/d", layout_dir, "/p", package, "/o"], check=True)
    print(package)
    return 0


if __name__ == "__main__":
    sys.exit(main())
