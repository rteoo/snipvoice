# Clean audio runtime

Official Snipvoice releases do not install PyAV's prebuilt wheel. The desktop
bundle workflow runs `clean_audio_runtime.py`, which builds PyAV 18.1.0 from
hash-pinned source against a hash-pinned FFmpeg 8.1.2 build.

The FFmpeg build uses shared libraries and native audio decoders only. It
disables GPL, nonfree, version-3, network, auto-detected external libraries,
programs, encoders, and video decoders. The repaired wheel is rejected unless
all seven FFmpeg libraries required by PyAV are dynamic and no known GPL codec
library is present.

Each platform bundle contains `THIRD_PARTY_LICENSES/FFmpeg` with:

- the exact FFmpeg source archive and SHA-256;
- the complete configure invocation, config header, and config log;
- the unmodified-source patch (empty by design) and build recipe;
- PyAV and FFmpeg license texts; and
- hashes for the PyAV wheel and FFmpeg shared libraries.

Snipvoice source remains MIT. PyAV is distributed under BSD-3-Clause and the
custom FFmpeg shared libraries under LGPL-2.1-or-later. These components keep
their own licenses; the combined bundle must not be described as MIT-only.

The GitHub Actions workflow is the supported release builder. For local
diagnostics, install the platform toolchain shown in that workflow, install
`packaging/requirements-build.txt` plus `delvewheel==1.13.0` on Windows or
`delocate==0.13.0` on macOS, and run:

```text
python packaging/clean_audio_runtime.py --work-dir build/clean-audio --wheel-dir build/clean-audio-wheel --compliance-dir build/clean-audio-compliance
python -m pip install --no-index build/clean-audio-wheel/av-*.whl
```

Then install `source/requirements-voice.txt`. Do not replace this with
`pip install av`; that resolves the upstream binary wheel and bypasses the
release license gate.

## Windows PowerShell

The Windows recipe can be launched from PowerShell. It discovers the default
MSYS2 installation at `C:\msys64` and adds its MINGW64 and MSYS binaries to
the child-process environment. Git Bash alone is insufficient because it does
not provide `gcc`.

Install MSYS2 with `base-devel`, `mingw-w64-x86_64-gcc`,
`mingw-w64-x86_64-nasm`, and `mingw-w64-x86_64-pkgconf`. If it is installed
elsewhere, set `SNIPVOICE_MSYS2_ROOT` before running the same PowerShell
commands:

```powershell
$env:SNIPVOICE_MSYS2_ROOT = 'D:\tools\msys64'
python -m pip install --require-hashes -r packaging\requirements-build.lock
python packaging\clean_audio_runtime.py `
  --work-dir build\clean-audio `
  --wheel-dir build\clean-audio-wheel `
  --compliance-dir build\clean-audio-compliance
```

## Microsoft Store (MSIX)

`build_msix.py` packs `dist\Snipvoice` into an unsigned MSIX for Partner
Center; the Store signs it on ingestion, so no code-signing certificate is
needed. The package identity comes from Partner Center > Product identity and
is set as the repository variables `SNIPVOICE_MSIX_IDENTITY_NAME`,
`SNIPVOICE_MSIX_PUBLISHER` (`CN=...`) and
`SNIPVOICE_MSIX_PUBLISHER_DISPLAY_NAME`; the Windows bundle job packs the MSIX
only once they exist. Locally, pass the same values as flags after
`build_release.bat`; `makeappx.exe` comes from the Windows SDK.

The package declares `runFullTrust` (restricted; Partner Center asks for a
justification), the microphone, and an opt-in startup task that the user
toggles in Windows Settings instead of a Startup-folder shortcut. Store
versions must strictly increase, so a beta and its stable release cannot share
`APP_VERSION`.
