# Capture timing and local speech detection

## Failure mechanisms

Continuous WASAPI sample positions can accompany a step in the packet QPC
timestamp. Treating both as independent audio boundaries can insert a gap or
overlap in the final mix. The Windows helper now derives continuous timestamps
from an integer device-position anchor. Real discontinuities, invalid timestamps,
pauses and endpoint reopens remain explicit; no captured samples are removed.
See [capture-health.md](capture-health.md) for the clock and recovery policy.

Whisper can return plausible words for silence and stationary background noise.
The tested native no-speech scores did not separate those cases reliably, so
changing their threshold was insufficient. A local Silero VAD 6.2.2 model now
selects speech before ASR for both dictation and meeting transcription. This is
audio classification rather than a blacklist of phrases.

## Runtime contract

- ONNX Runtime uses the CPU provider with one intra-operation and one
  inter-operation thread. Model loading is lazy and requires no network access.
- The model and its MIT license are fetched during builds from an immutable
  upstream commit and verified against SHA-256 pins. Frozen applications only
  resolve the bundled asset; missing or tampered assets fail visibly.
- At 16 kHz, each 512-sample window includes the preceding 64 samples, with a
  zero context at the beginning of each independent chunk. Recurrent state
  advances within a chunk. Omitting this context can incorrectly reject speech.
- Speech needs two consecutive windows above probability 0.35. Selected ranges
  receive 200 ms context padding; overlapping ranges merge before ASR. Long
  non-speech intervals between ranges are omitted from the inference input.
  The saved MP3 remains unchanged and transcript timestamps retain their original
  chunk boundaries; they do not claim word-level alignment.
- A gated provider uses bounded non-streaming inference. Streaming remains
  disabled for that provider until equivalent speech-range gating is supported.
- Quiet meeting chunks reach the detector without the previous energy cutoff.
  Empty results still create completed chunk entries for resumable processing.
  Cancellation during detection preserves a cancelled revision.

## Verification and limits

Regression tests cover QPC steps with continuous samples, actual device-position
jumps, pause/reopen resets, a three-hour sample-clock simulation, native flag
transport, speech context/state, padding, non-speech rejection, cancellation and
the provider/packaging boundaries. CI and the frozen voice-runtime probe also
load the actual ONNX model and reject a zero-signal control.

A live one-minute Windows loopback test of the new helper exported an MP3 and
passed the existing 0.95 correlation threshold for every 100 ms reference block.
Every reference sample was scored through EOF with one fixed initial alignment;
native flags remained journaled. This short run is separate from a live
three-hour acceptance test.

With the verified Whisper Turbo model, silence and a 30-second stationary-noise
window from a synthetic reference both produced false words without VAD and
empty results through the gate. A genuine synthetic English "Thank you" remained
recognizable at gains 1.0, 0.1 and 0.01. Longer normal and quiet synthetic speech
also reached ASR. These controls demonstrate the targeted correction; they are
not a general word-error benchmark or a guarantee against every hallucination.
Very quiet, unusual or heavily distorted speech still needs representative
accuracy testing. Physical USB/Bluetooth reconnects remain a separate hardware
validation from injected recovery faults.

Private logs and synthetic diagnostic audio remain outside the public repository.
