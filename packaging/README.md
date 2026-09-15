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
