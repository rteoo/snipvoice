"""Packaged MP3-first meeting audio diagnostic without user data or models."""

import math
import struct
import tempfile
from pathlib import Path


def _provider():
    class FakeProvider:
        def __init__(self):
            self.samples = 0

        def prepare(self, profile, language, cancel_event=None, allow_download=False):
            if allow_download:
                raise RuntimeError("probe attempted a model download")

        def transcribe(self, samples, cancel_event=None):
            self.samples += len(samples)
            return ""

        def cancel(self):
            return None

        def unload(self):
            return None

    return FakeProvider()


def run_probe():
    from meeting_mixdown import export_mixdown
    from meeting_storage import compact_capture, verify_mp3_file
    from meeting_store import MeetingStore
    from meeting_transcription import MeetingTranscriber

    with tempfile.TemporaryDirectory(prefix="snipvoice-audio-probe-") as directory:
        root = Path(directory)
        store = MeetingStore(root / "meetings")
        session = store.begin({"sources": "system"}, "synthetic probe")
        rate, channels, frames = 48000, 2, 48000
        payload = struct.pack("<" + "f" * (frames * channels), *(0.1 for _ in range(frames * channels)))
        store.append_audio(session, {
            "type": "audio", "generation": 0, "track": "system", "rate": rate,
            "channels": channels, "frames": frames, "timestamp": 0.0, "sequence": 0,
        }, payload)
        store.finish(session)
        destination = root / "final.mp3"
        evidence = {}

        def validate(path, duration):
            evidence.update(verify_mp3_file(path, expected_duration=duration))

        output = export_mixdown(store, session, destination, tracks=("system",), validate_output=validate)
        evidence["path"] = output
        store.save_final_audio(session, output, verification=evidence)
        compact_capture(store, session, evidence)
        provider = _provider()
        transcriber = MeetingTranscriber(store, provider)
        transcriber.transcribe(session, "probe", "en-US")
        if provider.samples <= 0:
            raise RuntimeError("final MP3 was not transcribed")
        metadata = store.get(session, include_events=False)
        if metadata.get("capture_storage", {}).get("state") != "compacted":
            raise RuntimeError("compaction did not commit")
        if metadata.get("tracks", {}).get("system", {}).get("available") is not False:
            raise RuntimeError("raw track remained available")
        return evidence["frames"], evidence["duration"]


def main():
    try:
        frames, duration = run_probe()
        if frames <= 0 or not math.isfinite(duration) or duration <= 0:
            raise RuntimeError("invalid MP3 result")
    except Exception:
        print("MEETING_AUDIO_PROBE FAIL")
        return 1
    print(f"MEETING_AUDIO_PROBE PASS frames={frames} duration={duration:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
