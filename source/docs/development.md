# Development

Snipvoice source is `3.0.0` on the `stable` channel.
Runtime dependencies and native voice dependencies remain in separate manifests.
Use an existing interpreter; do not implicitly update host tooling or packages.

Run all tests from `source` with `python -m unittest discover -s tests -v`.
Run focused correctness lint from root with `python -m ruff check source`.
The test/native CI matrices cover Windows/macOS/Linux, Python 3.12 and 3.14.
Core-only tests may skip optional native imports; the native CI lane requires
the pinned imports and rejects skipped runtime/resampler tests.

Windows: `build_release.bat`, then `build_installer.bat` (Inno Setup 6 required).
macOS: `./build_release_macos.sh`, optionally with an existing `PYTHON` interpreter
or `CODESIGN_IDENTITY`. Both builds stage first and probe the complete native
runtime before promoting. Installer identity, shortcuts, mutex, bundle identifier,
logs, user data, and default cache belong to Snipvoice independently of Sniptype.
PyInstaller is pinned separately in `requirements-build.txt`; source-only installs
do not need the packaging tool.

Live
microphone-to-paste, cancellation, device-loss, final-word resampling, denied
permissions, stale target, upgrade/uninstall, and macOS TCC/signing behavior need
physical desktop proof. Hosted bundles are unsigned or ad-hoc signed and must not
be represented as notarized or publisher-signed.

Offline meetings add owned Windows/macOS capture helpers built by
`source/native/build_windows_capture.bat` and
`source/native/build_macos_capture.sh`. Existing native toolchains are required;
the package scripts compile/bundle these helpers and run the non-recording
`--meeting-capture-probe` before promotion. The meeting workspace shares the
existing GUI root and keeps new device/disk/inference work on workers.
`meeting_mixdown.py` derives one bounded, atomic PCM16 WAV from the timestamped
raw tracks after a normal stop. It linearly adapts a lower native rate to the
higher source clock and never rewrites the recoverable track segments. The
optional microphone cleanup is a deterministic low-level gate, bounded gain,
and limiter; do not describe it as spectral denoising or echo cancellation.
Automatic transcription and summary remain opt-in and run sequentially under
the existing local inference reservation.
See [implementation validation and open hardware gates](offline-meeting-validation.md).
