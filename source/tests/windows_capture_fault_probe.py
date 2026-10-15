"""Opt-in real WASAPI lifecycle tests using a separate fault-test build.

Run explicitly with --run and --output under the private logs directory.
Default unittest discovery never runs this probe or records a microphone.
"""

import argparse
from collections import Counter
import json
import math
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from meeting_audio import NativeCapture
from meeting_settings import EndpointSelection, MeetingSettings


def tone(path):
    samples = [round(600 * math.sin(2 * math.pi * 997 * index / 48000)) for index in range(48000)]
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(2)
        writer.setsampwidth(2)
        writer.setframerate(48000)
        writer.writeframes(b"".join(struct.pack("<hh", value, value) for value in samples))


def scenario(helper, endpoint, name, signal):
    import winsound
    fault = None if name in {"quiet", "control"} else name
    duration = 30 if name == "invalidate-persistent" else 12 if name == "zero-once" else 9
    def spawn(arguments, **options):
        if fault:
            arguments = [*arguments, "--fault-script", fault]
        return subprocess.Popen(arguments, **options)
    capture = NativeCapture(helper, popen=spawn)
    counters = Counter()
    health = Counter()
    reasons = Counter()
    endpoints = set()
    frames = 0
    previous_sequence = None
    sequence_breaks = 0
    packets_after_recovery = 0
    max_amplitude = 0.0
    stopped = False
    stop_sent = False
    started = time.monotonic()
    try:
        if name != "quiet":
            winsound.PlaySound(str(signal), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_LOOP)
        capture.start(MeetingSettings(sources="system", system=EndpointSelection("manual", endpoint)), 1)
        while time.monotonic() - started < duration + 5:
            if time.monotonic() - started >= duration and not stop_sent:
                capture.command("stop")
                stop_sent = True
            item = capture.read_event(.1)
            if item is None:
                continue
            event, payload = item
            counters[event["type"]] += 1
            if event["type"] == "source_changed":
                endpoints.add(event["endpoint_id"])
            elif event["type"] == "capture_health":
                health[event["state"]] += 1
            elif event["type"] == "gap":
                reasons[event["reason"]] += 1
            elif event["type"] == "audio":
                sequence = event["sequence"]
                if previous_sequence is not None and sequence != previous_sequence + 1:
                    sequence_breaks += 1
                previous_sequence = sequence
                frames += event["frames"]
                if health["recovering"] and counters["source_changed"] > health["recovering"]:
                    packets_after_recovery += 1
                values = struct.iter_unpack("<f", payload)
                max_amplitude = max(max_amplitude, max((abs(value[0]) for value in values), default=0))
            elif event["type"] == "stopped":
                stopped = True
                break
    finally:
        try:
            capture.stop(force=not stopped)
        finally:
            if name != "quiet":
                winsound.PlaySound(None, 0)
    result = {"scenario": name, "events": dict(counters), "health": dict(health),
              "gap_reasons": dict(reasons), "endpoint_count": len(endpoints),
              "frames": frames, "sequence_breaks": sequence_breaks,
              "packets_after_recovery": packets_after_recovery,
              "max_amplitude": max_amplitude, "stopped_cleanly": stopped,
              "elapsed_seconds": round(time.monotonic() - started, 3)}
    assert stopped and sequence_breaks == 0, "capture did not stop cleanly or sequence broke"
    if name == "invalidate-persistent":
        assert health["recovering"] == 6 and health["failed"] == 1
        assert counters["source_error"] == 1 and frames > 0
    else:
        assert counters["source_error"] == 0 and health["failed"] == 0
        if name in {"invalidate-once", "zero-once"}:
            assert health["recovering"] == 1 and counters["source_changed"] == 2
            assert len(endpoints) == 1 and packets_after_recovery > 0
            if name == "zero-once":
                assert reasons["capture_interruption_zero_frames"] == 1
        else:
            assert health["recovering"] == 0 and counters["source_changed"] == 1
        if name == "quiet":
            assert max_amplitude <= 0.00001, "other system audio prevented a quiet-loopback control"
        else:
            assert frames > 0 and max_amplitude > .001, "synthetic source was not captured"
    result["passed"] = True
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.run or sys.platform != "win32":
        parser.error("Windows and explicit --run are required")
    output = args.output.resolve()
    logs = (Path.home() / ".snipvoice/logs").resolve()
    if not output.is_relative_to(logs) or output == logs:
        parser.error("output must be under the private logs directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    helper = Path(__file__).resolve().parents[1] / "native/bin/snipvoice-capture-fault-test.exe"
    if not helper.is_file():
        parser.error("build native helper with --fault-tests first")
    devices = NativeCapture(helper).list_devices()
    endpoint = next(device["id"] for device in devices if device["kind"] == "system" and device.get("default"))
    results = []
    try:
        temporary_root = Path(__file__).resolve().parent / "tmp"
        temporary_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="capture-fault-", dir=temporary_root) as directory:
            signal = Path(directory) / "synthetic-tone.wav"
            tone(signal)
            for name in ("quiet", "control", "empty-once", "invalidate-once", "zero-once", "invalidate-persistent"):
                result = scenario(helper, endpoint, name, signal)
                results.append(result)
                print(json.dumps(result), flush=True)
    finally:
        output.write_text(json.dumps({"results": results, "passed": len(results) == 6}, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
