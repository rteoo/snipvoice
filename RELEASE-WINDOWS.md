# Windows release preparation

Set the clean FFmpeg evidence directory first:

```powershell
$env:SNIPVOICE_FFMPEG_COMPLIANCE_DIR = 'C:\path\to\compliance'
.\prepare-release-windows.ps1 -Tag v1.2.0 -DryRun
.\prepare-release-windows.ps1 -Tag v1.2.0
```

The helper requires clean `main` at `origin/main`, runs unittest, preserves the
compliance manifest/native capture/runtime probes in the existing builders, and
hashes the Windows installer. macOS capture, signing, notarization, and release
publication remain external gates.
