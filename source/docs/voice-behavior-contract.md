> Historical Sniptype record, retained unchanged from the predecessor. Release, path, and issue references describe Sniptype. See [extraction.md](extraction.md) for the current Snipvoice boundary.

# Behavior Contract

## User-Visible Goal
Voice input stays off until enabled, never types a trigger through the
keyboard listener, and failed or empty captures do not paste.

## Target
- Type: CLI probe
- Launch or access: `python voice_probe.py` from `source/`
- Allowed fixtures and credential source: none

## User Tasks
1. Run the probe and read the printed clause results.
2. Confirm every clause is `PASS`.

## Expected Observable Behavior
- Voice defaults to disabled / unavailable.
- Enabling voice keeps it unavailable when the configured capture runtime is
  missing; the model is not loaded until capture is ready.
- Cancellation is checked again after blocking target readiness and immediately
  before each application callback commits a side effect. A callback that has
  already committed may finish and cannot be interrupted or undone. Queued Tk
  form writes recheck their session token when they execute.
- Capture, ASR, target restoration, and application callbacks run without the
  controller state lock. Internal history metadata writes serialize with the
  state transition; slow local disk I/O can therefore delay cancellation and
  status updates during that commit.
- If shutdown reaches its bounded wait while native inference is still running,
  runtime unload is deferred until the worker exits; voice remains unavailable
  during that cleanup.
- A dictation result is inserted as literal text, even if it looks like a trigger.
- A voice-command result expands only an exact trigger and does not insert the spoken word.
- A form-field result updates the form callback and does not insert globally.
- An empty transcript does not insert.
- A second press during a session is ignored.
- Shutdown after press performs no insert.
- Native 44.1/48 kHz mono or stereo input is converted to canonical mono
  16 kHz float32 without resetting the resampler between callback chunks.
- Releasing push-to-talk flushes the resampler tail before inference.
- A capture status, device, queue, normalization, journal, stop, or close
  failure stores the available audio as failed history and never transcribes
  or pastes it as a completed utterance.
- An empty or misaligned history recording is not retryable. A valid retry
  copies its result to the clipboard and never pastes into a later target.
- User term corrections apply once to dictation and form results. Spoken
  commands continue matching the provider transcript unchanged.

## Anti-Cheat Probes
- Change the fake transcript and confirm the inserted/expanded value changes with it.
- A non-matching command prints `no_match` and expands nothing.
- Split a 48 kHz test signal across irregular callback boundaries and compare
  it with one-shot SoXR output; the flushed results must match.
- Configure `Queen` → `Qwen`; confirm dictation changes, the raw transcript is
  retained in history, and command matching is unchanged.

## Evidence Required
- The probe stdout, one `CLAUSE ... PASS|FAIL` line per clause, ending with
  `RESULT pass` or `RESULT fail`.
- The focused voice audio, resampler, controller, history, settings, GUI, and
  packaging tests.

## Out Of Scope
- Physical microphone/device-loss behavior, real model download, packaged
  installer size, Handy composition, and live WER.
