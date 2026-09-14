# Development

Snipvoice source is `0.1.0` on the `beta` channel. It has no published release.
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

No installer is produced or installed merely by creating the fork. Live
microphone-to-paste, cancellation, device-loss, final-word resampling, denied
permissions, stale target, upgrade/uninstall, and macOS TCC/signing behavior need
physical desktop proof. Do not promote beta based only on offline tests or CI.
