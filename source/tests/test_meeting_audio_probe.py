import contextlib
import io
from pathlib import Path
import unittest
from unittest import mock

import meeting_audio_probe


class MeetingAudioProbeTests(unittest.TestCase):
    def test_entrypoint_dispatches_before_desktop_imports(self):
        text = (Path(__file__).resolve().parents[1] / "snipvoice.pyw").read_text(encoding="utf-8")
        self.assertLess(text.index("run_meeting_audio_probe_if_requested()"),
                        text.index("import platform_support"))

    def test_failure_is_one_safe_line(self):
        output = io.StringIO()
        with mock.patch.object(meeting_audio_probe, "run_probe", side_effect=RuntimeError("private detail")), \
                contextlib.redirect_stdout(output):
            self.assertEqual(meeting_audio_probe.main(), 1)
        self.assertEqual(output.getvalue(), "MEETING_AUDIO_PROBE FAIL\n")
        self.assertNotIn("private detail", output.getvalue())

    @unittest.skipUnless(
        __import__("importlib").util.find_spec("av") is not None,
        "PyAV is required for the installed-codec probe",
    )
    def test_real_codec_probe_compacts_and_transcribes_final_audio(self):
        frames, duration = meeting_audio_probe.run_probe()
        self.assertGreater(frames, 0)
        self.assertGreater(duration, 0.9)
        self.assertLess(duration, 1.1)


if __name__ == "__main__":
    unittest.main()
