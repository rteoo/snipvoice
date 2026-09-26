# Development

Snipvoice source is `1.1.0` on the `stable` channel (`v1.1.0`).
Runtime dependencies and native voice dependencies remain in separate manifests.
Use an existing interpreter; do not implicitly update host tooling or packages.

Run all tests from `source` with `python -m unittest discover -s tests -v`.
Run focused correctness lint from root with `python -m ruff check source`.
CI uses three consolidated lanes: Windows and macOS run the release Python
3.12 runtime, while Linux runs Python 3.14 plus focused Ruff checks. Every lane
installs the pinned native voice dependencies, requires their imports, rejects
skipped runtime/resampler tests, builds the applicable capture helper, and runs
the complete suite. This preserves all supported operating-system and Python
version boundaries without a Cartesian matrix.

Desktop bundle jobs remain the release proof, but run on pull requests only
when packaging, native-helper, dependency, icon, installer, or workflow inputs
change. They always run for version tags and manual dispatch. Pull-request
bundle artifacts are not uploaded; tag/manual artifacts expire after 14 days
and should be moved to a GitHub Release when they are intended for distribution.
Branch protection should require only `tests (ubuntu-latest, py3.14)`,
`tests (windows-latest, py3.12)`, and `tests (macos-latest, py3.12)`; lint and
native voice are enforced inside those jobs rather than as separate contexts.

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
`meeting_mixdown.py` derives one bounded, atomic MP3 from the timestamped raw
tracks after a normal stop; PCM16 WAV is an explicit export option. The bundled
PyAV/LAME encoder streams the mix without a permanent WAV intermediate.
It linearly adapts a lower native rate to the
higher source clock and never rewrites the recoverable track segments. The
optional microphone volume adjustment measures the raw track in bounded memory,
then applies capped gain and a limiter only to the derived playback file. It preserves
quiet speech instead of hard-gating it; do not describe it as spectral denoising
or echo cancellation. Transcription continues to consume the original raw
tracks, so MP3 compression does not alter model input. Existing final WAVs stay
playable and are never automatically converted or removed. Raw storage remains
available for recovery and retranscription under the existing retention policy.
New recordings and imports automatically use installed transcription and
summary models, sequentially under the existing local inference reservation.
Unavailable models are skipped without downloads. The library opens full text
by default, offers a timestamped presentation, and copies/exports the selected
format without rerunning inference. Summaries are saved automatically and can
be regenerated. Manual organization and filter controls are removed from the
interface; stored organization metadata remains intact.
See [implementation validation and open hardware gates](offline-meeting-validation.md).

Local meeting-memory development uses the canonical-bundle/disposable-index
boundary described in
[`local-meeting-memory-operations.md`](local-meeting-memory-operations.md).
Run storage, index, report, annotation, clip, and retention tests only against
copied fixtures under `source/tests/tmp`; never point them at a live
`SNIPVOICE_HOME`. Deleting `library.sqlite*` is an index-rebuild test, not a
data-deletion test. The SQLite/FTS runtime probe, repair/rebuild UI, report and
Q&A controls, consent settings, retention preview, trash,
restore, and purge routes are implemented. On 2026-09-17,
`python -m unittest discover -s tests -q` ran 1,195 tests successfully with 53
environment/platform skips. Ruff is unavailable in this validation context and
must not be described as passed until it is run with the pinned development
tool. Physical Tk/tray interaction, native capture, packaged startup, and
signing remain separate acceptance work.
