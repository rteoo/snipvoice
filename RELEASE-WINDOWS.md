# Windows release preparation

## Current candidate: v1.4.0-beta.3

Use the canonical **Desktop bundles** GitHub Actions workflow for this beta;
it builds Windows with Python 3.12 and the approved native dependencies.
The local helper below accepts stable releases only.

Before publication, merge the reviewed metadata, build the exact candidate
revision, verify installer/ZIP checksums and bundled probes, then scan and
smoke-test the candidate installer on Windows. Verify macOS bundle checks
separately. Previous beta.2 package validation does not certify beta.3 assets.
Keep the prerelease flag and include the current changelog's validation limits.
Preparing these files does not create a tag or publish a release.

## Stable release helper

Set the clean FFmpeg evidence directory first:

```powershell
$env:SNIPVOICE_FFMPEG_COMPLIANCE_DIR = 'C:\path\to\compliance'
.\prepare-release-windows.ps1 -Tag v1.2.1 -DryRun
.\prepare-release-windows.ps1 -Tag v1.2.1
```

The helper requires matching source/runtime, installer, and bundle-workflow
version/channel metadata, then clean `main` at `origin/main`. It runs unittest,
preserves the compliance manifest/native capture/runtime probes in the existing
builders, and hashes the Windows installer. macOS capture, signing, notarization,
and release publication remain external gates.
