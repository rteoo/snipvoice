import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from voice_history import (
    STATUS_COMPLETED,
    STATUS_INTERRUPTED,
    STATUS_PENDING,
    VoiceHistoryStore,
)


class VoiceHistoryTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.store = VoiceHistoryStore(self.root)

    def _recording(self):
        return self.store.begin(
            mode="dictation",
            provider="local",
            profile="balanced",
            language="pt-BR",
            target_kind="window",
        )

    def test_capture_is_append_only_and_roundtrips_samples(self):
        recording = self._recording()
        recording.write_chunk([0.25, -0.5])
        recording.write_chunk([0.75])
        recording.finish_capture()

        entry = self.store.get(recording.record_id)
        self.assertEqual(entry["status"], STATUS_PENDING)
        self.assertEqual(self.store.load_samples(recording.record_id), [0.25, -0.5, 0.75])
        self.assertFalse(
            os.path.exists(
                os.path.join(self.root, recording.record_id, "metadata.json.tmp")
            )
        )

    def test_finish_capture_appends_only_missing_journal_tail(self):
        recording = self._recording()
        recording.write_chunk([0.25])
        recording.finish_capture([0.25, -0.5, 0.75])

        self.assertEqual(
            self.store.load_samples(recording.record_id),
            [0.25, -0.5, 0.75],
        )

    def test_short_writes_are_counted_exactly(self):
        class ShortWriter:
            def __init__(self):
                self.payload = bytearray()

            def write(self, payload):
                written = min(3, len(payload))
                self.payload.extend(payload[:written])
                return written

            def flush(self):
                pass

            def fileno(self):
                return 1

        recording = self._recording()
        recording._handle.close()
        writer = ShortWriter()
        recording._handle = writer

        with mock.patch("voice_history.os.fsync"):
            recording.write_chunk([0.25, -0.5])

        self.assertEqual(recording._bytes_written, 8)
        self.assertEqual(len(writer.payload), 8)

    def test_zero_length_write_fails_without_inventing_progress(self):
        handle = mock.Mock()
        handle.write.return_value = 0
        recording = self._recording()
        recording._handle.close()
        recording._handle = handle

        with self.assertRaisesRegex(OSError, "não avançou"):
            recording.write_chunk([0.25])

        self.assertEqual(recording._bytes_written, 0)

    def test_startup_recovers_an_interrupted_recording(self):
        recording = self._recording()
        recording.write_chunk([0.1])
        recording._handle.close()
        recording._closed = True

        recovered = VoiceHistoryStore(self.root)

        entry = recovered.get(recording.record_id)
        self.assertEqual(entry["status"], STATUS_INTERRUPTED)
        self.assertTrue(recovered.is_retryable(recording.record_id))

    def test_empty_failed_recording_is_not_retryable(self):
        recording = self._recording()
        recording.finish_capture()
        self.store.fail(recording.record_id, "offline")

        self.assertFalse(self.store.is_retryable(recording.record_id))

    def test_misaligned_failed_recording_is_not_retryable(self):
        recording = self._recording()
        recording.finish_capture([0.1])
        self.store.fail(recording.record_id, "offline")
        with open(self.store._audio_path(recording.record_id), "ab") as handle:
            handle.write(b"x")

        self.assertFalse(self.store.is_retryable(recording.record_id))
        with self.assertRaisesRegex(ValueError, "corrompido"):
            self.store.load_samples(recording.record_id)

    def test_completed_transcript_remains_available(self):
        recording = self._recording()
        recording.finish_capture([0.1, 0.2])
        self.store.complete(recording.record_id, "texto", "inserted")

        entry = self.store.get(recording.record_id)
        self.assertEqual(entry["status"], STATUS_COMPLETED)
        self.assertEqual(entry["transcript"], "texto")
        self.assertFalse(self.store.is_retryable(recording.record_id))

    def test_capture_and_raw_transcript_metadata_are_additive(self):
        recording = self._recording()
        recording.finish_capture(
            [0.1],
            {
                "source_sample_rate_hz": 48000,
                "source_channels": 2,
                "capture_duration_seconds": 0.25,
            },
        )
        self.store.mark_transcribed(
            recording.record_id,
            "Qwen",
            raw_transcript="Queen",
            inference_duration_seconds=0.1,
        )

        entry = self.store.get(recording.record_id)
        self.assertEqual(entry["source_sample_rate_hz"], 48000)
        self.assertEqual(entry["source_channels"], 2)
        self.assertEqual(entry["raw_transcript"], "Queen")
        self.assertEqual(entry["transcript"], "Qwen")

    def test_corrupt_metadata_is_ignored_without_overwriting_it(self):
        item = os.path.join(self.root, "broken")
        os.makedirs(item)
        path = os.path.join(item, "metadata.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{broken")

        self.assertEqual(self.store.list_entries(), [])
        with open(path, "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "{broken")

    def test_metadata_cannot_redirect_recovery_outside_history(self):
        recording = self._recording()
        recording.finish_capture([0.1])
        path = os.path.join(self.root, recording.record_id, "metadata.json")
        with open(path, "r", encoding="utf-8") as handle:
            metadata = json.load(handle)
        metadata["id"] = "../../outside"
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(metadata, handle)

        entry = self.store.list_entries()[0]

        self.assertEqual(entry["id"], recording.record_id)
        self.assertIsNone(self.store.get("../../outside"))


if __name__ == "__main__":
    unittest.main()
