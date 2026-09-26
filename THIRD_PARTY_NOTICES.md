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
| `PyAV` | Python bindings for compressed audio-file encoding and decoding | BSD-3-Clause; the exact license text is in `THIRD_PARTY_LICENSES/FFmpeg/PyAV-LICENSE.txt` |
| `LAME` 3.100 | LGPL MP3 encoder linked into the approved FFmpeg runtime | LGPL-2.0-or-later; the exact source archive, hash, build flags, and license text are in `THIRD_PARTY_LICENSES/FFmpeg/` |
| Custom FFmpeg audio runtime | Shared native encoding of MP3 and decoding of MP3, AAC/M4A, FLAC, Ogg/Vorbis, Opus, WAV and related audio formats | LGPL-2.1-or-later; source, configuration, hashes, build evidence, and the exact license text are in `THIRD_PARTY_LICENSES/FFmpeg/` |
| `transcribe-cpp` / `transcribe-cpp-native` | Optional local transcription runtime | [transcribe.cpp](https://github.com/handy-computer/transcribe.cpp) |
| `llama-cpp-python` / `llama.cpp` | Built-in local summary inference | [llama-cpp-python](https://github.com/abetlen/llama-cpp-python), [llama.cpp](https://github.com/ggml-org/llama.cpp) |

The PyAV release wheel is built from hash-pinned source against Snipvoice's
minimal shared FFmpeg build. The release gate rejects GPL, nonfree, version-3,
external codec, network, and static-library configurations. The exact versions
and build recipe are recorded in `packaging/clean_audio_runtime.py`; the bundled
runtime manifest records the resulting configuration. The optional voice
catalog can download third-party model artifacts;
the model metadata and its applicable attribution requirements are documented
in [`source/docs/voice-input-plan.md`](source/docs/voice-input-plan.md). Do not
describe a model as bundled unless the release actually contains it. Summary
model sources, hashes, sizes, and license gates are documented in
[`source/docs/summary-model-selection.md`](source/docs/summary-model-selection.md).
The optional LiquidAI LFM2.5-2.6B download is governed by the
[LFM Open License v1.0](https://huggingface.co/LiquidAI/LFM2.5-2.6B-GGUF/blob/84022ce711b28455e8c4fc364ce68c00cf995875/LICENSE),
including its redistribution conditions and commercial-use threshold.

This file is an attribution index, not a replacement for the upstream license
texts. Review the upstream notices for the exact versions and artifacts in the
release being distributed.
