# Windows capture health and recovery

Capture remains native WASAPI shared mode. This policy does not change the OS
default device, invent PCM for quiet intervals, suppress discontinuity flags,
or erase earlier interruption evidence after a successful reopen.

## Health policy

- Accepted packets advance per-source packet/frame counters. Health events are
  journaled on state changes and at most once every five seconds otherwise.
- Endpoint state, stream-clock availability and endpoint peak observations are
  sampled every 500 ms, after releasing native packet buffers.
- Microphone starvation requires three seconds without usable packets plus
  three successful stream-clock observations. A readable clock with no captured
  packets is evidence that the client is not delivering its expected stream.
- Loopback can remain quiet without packets indefinitely. Missing loopback
  packets trigger recovery only with a readable stream clock and at least one
  second / three observations of active endpoint peaks above 0.00001.
- A race between advertised packet size and `AUDCLNT_S_BUFFER_EMPTY` is normal.
  Repeated advertised-positive/zero-frame reads spanning three seconds trigger
  recovery. A later no-pending observation clears that suspicion.
- Pause resets watchdog age and observations. A paused source does not spend
  its reconnect budget or undergo endpoint-health polling.

`capture_health` events carry the track, state, reason, cumulative packet/frame
counts, last-packet age and recovery attempt. Counters describe native accepted
packets, not independently verified durable disk writes. The controller journals
the observations before publishing them to the UI. An active recovery adds
“Reconnecting audio” to recording feedback without exposing native error text.

## Recovery

A source that follows the OS default switches to the new default as soon as
it changes, whether it is polled while running or found after the old default
was removed. The switch records `default_endpoint_changed`, spends no retry and
is not audio loss. Each new default gets one immediate open; if that fails,
the normal backoff applies.

Device/resource invalidation, an unavailable selected device, temporary audio
service unavailability, and watchdog-confirmed starvation reopen the selected
endpoint. A manual endpoint remains pinned to its original ID. While it is
absent (or no default exists at all), the source reports `disconnected`, the
controller alerts that the recording is receiving no audio from that source,
and the source keeps waiting for the endpoint to return. One outage records one
`capture_interruption_reopening` gap, so the session ends `partial`.

Retry delays are 250, 500, 1000, 2000 and then 4000 ms until the source
returns or the user stops; the reported attempt saturates at six. Stop
interrupts these waits. Thirty seconds of continuous healthy packet timing and
captured sample duration restore the short delays; a single packet cannot. A
recording never ends because a source is missing. Permission, format and other
permanent errors are not retried and remain source errors.

Valid buffered tails are flushed before abandoning a client. Failed clients
are not drained again. Sequence numbering continues across successful reopens.
Existing capture gaps and source-error metadata remain available for diagnosis;
successful recovery is not a claim that absent samples returned.

Native COM calls retain their platform blocking behavior. The existing helper
process shutdown bounds remain the final containment for an unresponsive driver.

## Sample timing

Each opened endpoint anchors its first valid packet to QPC. While WASAPI's
integer device position advances by exactly the preceding packet's frame count,
subsequent timestamps come from that anchor and sample position. A QPC step with
continuous samples must not make the exporter insert or overlap audio. The
calculation uses the original integer anchor rather than repeatedly adding a
floating-point packet duration, including over a three-hour session.

Device-position jumps, invalid timestamps, pauses and reopened endpoints break
that continuity. Their QPC boundaries remain visible. This does not remove PCM,
compact silent packets, or claim that missing driver samples were recovered.
Native discontinuity diagnostics include both QPC and selected sample timestamps;
audio blocks also retain the OR of their native packet flags. Watchdog packet age
uses wall time independently of the selected audio timeline.

Different physical endpoints keep separate clock anchors and drift against
QPC at their own oscillator rates. A smoothed QPC error beyond 20 ms slews the
anchor by at most 0.05% of each packet's duration until the error falls below
5 ms, so per-packet jitter never moves samples while drift between the tracks
stays bounded near 20 ms. A counter that keeps advancing more than 0.5 s away
from real time is a discontinuity and re-anchors to QPC.

## Verification

From `source`, with the existing MSVC toolchain:

```powershell
cmd /c native\build_windows_capture.bat
python -m unittest tests.test_windows_capture tests.test_meeting_audio tests.test_meeting_support tests.test_meeting_gui -v
```

The native self-test runs deterministic quiet-loopback, empty-race, zero-frame,
starvation, same-device reopen, capped persistent retry, missing-endpoint
outage, default switching, stable-budget reset, permanent-error, clock drift,
tail/sequence preservation and cancellation scenarios without opening devices.

The explicit live fault probe opens real WASAPI loopback clients and uses a
generated tone. Close other system-audio playback for its quiet control:

```powershell
cmd /c native\build_windows_capture.bat --fault-tests
python tests/windows_capture_fault_probe.py --run --output "$HOME\.snipvoice\logs\capture-health\native-fault-probe.json"
```

The fault build is a separate executable. Its parser and injection code are
compiled only with `SNIPVOICE_CAPTURE_FAULT_TESTS`. Official bundles explicitly
include only `snipvoice-capture.exe`; normal builds reject fault arguments.
The probe discards PCM, saves aggregate evidence privately and cleans playback
and its temporary WAV on failure too. It tests real client teardown/reopen with
controlled injected faults, not physical USB/Bluetooth hotplug. Long installed
app recording and waveform/reference checks are separate acceptance evidence.

Microsoft contracts: [GetBuffer](https://learn.microsoft.com/en-us/windows/win32/api/audioclient/nf-audioclient-iaudiocaptureclient-getbuffer),
[stream clock](https://learn.microsoft.com/en-us/windows/win32/api/audioclient/nf-audioclient-iaudioclock-getposition),
and [endpoint meter](https://learn.microsoft.com/en-us/windows/win32/api/endpointvolume/nn-endpointvolume-iaudiometerinformation).
