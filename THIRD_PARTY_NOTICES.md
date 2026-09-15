# Third-party notices

Snipvoice is distributed under the MIT License; see [LICENSE](LICENSE).
The application depends on the following separately licensed projects:

| Component | Use | License/source |
| --- | --- | --- |
| `pynput` | Global keyboard input | [PyPI](https://pypi.org/project/pynput/) |
| `pystray` | System-tray integration | [PyPI](https://pypi.org/project/pystray/) |
| `Pillow` | Image and icon handling | [Pillow license](https://github.com/python-pillow/Pillow/blob/main/LICENSE) |
| `sounddevice` / PortAudio | Optional voice capture | [sounddevice](https://github.com/spatialaudio/python-sounddevice), [PortAudio](https://github.com/PortAudio/portaudio) |
| `soxr` / libsoxr / PFFFT | Optional voice sample-rate conversion | [python-soxr](https://github.com/dofuuz/python-soxr), [LGPLv2.1+ license](https://github.com/dofuuz/python-soxr/blob/main/LICENSE.txt), [libsoxr](https://sourceforge.net/projects/soxr/) |
| `transcribe-cpp` / `transcribe-cpp-native` | Optional local transcription runtime | [transcribe.cpp](https://github.com/handy-computer/transcribe.cpp) |
| `llama-cpp-python` / `llama.cpp` | Built-in local summary inference | [llama-cpp-python](https://github.com/abetlen/llama-cpp-python), [llama.cpp](https://github.com/ggml-org/llama.cpp) |

The exact versions used by a build are recorded in
`source/requirements*.txt`. Before publishing a packaged build, include the
license and notice files shipped by each resolved dependency and native
library. The optional voice catalog can download third-party model artifacts;
the model metadata and its applicable attribution requirements are documented
in [`source/docs/voice-input-plan.md`](source/docs/voice-input-plan.md). Do not
describe a model as bundled unless the release actually contains it. Summary
model sources, hashes, sizes, and license gates are documented in
[`source/docs/summary-model-selection.md`](source/docs/summary-model-selection.md).

This file is an attribution index, not a replacement for the upstream license
texts. Review the upstream notices for the exact versions and artifacts in the
release being distributed.
