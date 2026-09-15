"""Bounded meeting waveform model and Canvas tests."""

import tkinter as tk
import unittest

from meeting_waveform import (
    AmplitudeRingBuffer, MeetingWaveform, MeetingWaveformModel, TRACKS,
    envelope_coordinates,
)


class AmplitudeRingBufferTests(unittest.TestCase):
    def test_capacity_discards_oldest_samples(self):
        buffer = AmplitudeRingBuffer(3)
        self.assertEqual(buffer.append_block([0.1, 0.2, 0.3, 0.4]), 4)
        self.assertEqual(buffer.values(), (0.2, 0.3, 0.4))
        self.assertEqual(len(buffer), 3)

    def test_samples_are_bounded_and_invalid_values_do_not_break_capture(self):
        buffer = AmplitudeRingBuffer(5)
        self.assertEqual(buffer.append_block([-4, 0.5, 4, float("nan"), "bad"]), 3)
        self.assertEqual(buffer.values(), (-1.0, 0.5, 1.0))

    def test_block_input_is_bounded_before_storage(self):
        buffer = AmplitudeRingBuffer(8)
        values = (value / 100 for value in range(10000))
        self.assertEqual(buffer.append_block(values), 4096)
        self.assertEqual(len(buffer), 8)
        self.assertEqual(buffer.values()[-1], 1.0)

    def test_geometry_is_bounded_to_pixels_and_lane(self):
        buffer = AmplitudeRingBuffer(10)
        buffer.append_block([0, 0.5, 1.0, -0.25])
        geometry = buffer.geometry(3, 20, x=10, y=5)
        self.assertEqual(len(geometry), 3)
        self.assertEqual(geometry[0][0], 10.5)
        for _center, top, bottom in geometry:
            self.assertGreaterEqual(top, 5)
            self.assertLessEqual(bottom, 25)

    def test_invalid_geometry_dimensions_are_empty(self):
        buffer = AmplitudeRingBuffer(4)
        buffer.append_block([1])
        self.assertEqual(buffer.geometry(0, 20), ())
        self.assertEqual(buffer.geometry(10, 0), ())
        self.assertEqual(buffer.geometry("bad", 20), ())

    def test_envelope_polygon_keeps_upper_and_lower_edges_separate(self):
        points = ((1, 2, 8), (2, 3, 7), (3, 4, 6))
        self.assertEqual(
            envelope_coordinates(points),
            (1, 2, 2, 3, 3, 4, 3, 6, 2, 7, 1, 8),
        )

    def test_single_column_is_a_valid_four_corner_polygon(self):
        self.assertEqual(
            envelope_coordinates(((4, 2, 8),)),
            (3.5, 2, 4.5, 2, 4.5, 8, 3.5, 8),
        )


class MeetingWaveformModelTests(unittest.TestCase):
    def test_model_keeps_microphone_and_system_independent(self):
        model = MeetingWaveformModel(capacity=4)
        self.assertEqual(TRACKS, ("microphone", "system"))
        model.append_block("microphone", [1, 0.5])
        model.append_block("system", [0.25])
        self.assertEqual(model.sample_count("microphone"), 2)
        self.assertEqual(model.sample_count("system"), 1)
        model.clear("microphone")
        self.assertEqual(model.sample_count("microphone"), 0)
        self.assertEqual(model.sample_count("system"), 1)

    def test_unknown_track_is_rejected(self):
        model = MeetingWaveformModel()
        with self.assertRaises(ValueError):
            model.append_block("speakers", [1])


class MeetingWaveformWidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
            cls.root.withdraw()
        except tk.TclError as exc:
            raise unittest.SkipTest(f"Tk initialization unavailable: {exc}") from exc

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def test_enqueue_is_bounded_and_drained_on_tk_path(self):
        waveform = MeetingWaveform(self.root, poll_ms=1000)
        try:
            for value in range(100):
                self.assertTrue(waveform.enqueue_block("microphone", [value / 100]))
            self.assertLessEqual(waveform._pending.qsize(), 64)
            waveform._drain_pending()
            self.assertGreater(waveform.model.sample_count("microphone"), 0)
            self.assertLessEqual(waveform.model.sample_count("microphone"), 4096)
        finally:
            waveform.destroy()
            self.root.update_idletasks()

    def test_state_and_status_are_visible_and_validated(self):
        waveform = MeetingWaveform(self.root, poll_ms=1000)
        try:
            self.assertEqual(waveform.state, "idle")
            self.assertEqual(waveform.status_text, "Estado: Pronto")
            waveform.set_state("recording")
            self.assertEqual(waveform.status_text, "Estado: Gravando")
            waveform.set_state("paused")
            self.assertEqual(waveform.status_text, "Estado: Pausado")
            with self.assertRaises(ValueError):
                waveform.set_state("stopped")
        finally:
            waveform.destroy()
            self.root.update_idletasks()


if __name__ == "__main__":
    unittest.main()
