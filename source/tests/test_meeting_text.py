import unittest

from meeting_text import (
    TIMESTAMPED,
    format_summary,
    format_transcript,
    iter_formatted_transcript,
    preview_transcript,
)


class MeetingTextTests(unittest.TestCase):
    def test_readable_preserves_order_and_groups_pause_and_track(self):
        segments = [
            {"track": "microphone", "start": 0, "end": 1, "text": "Primeiro"},
            {"track": "microphone", "start": 1, "end": 2, "text": "ponto."},
            {"track": "system", "start": 2, "end": 3, "text": "Segundo"},
            {"track": "system", "start": 8, "end": 9, "text": "ponto."},
        ]
        result = format_transcript(segments)
        self.assertEqual(result, "Primeiro ponto.\n\nSegundo\n\nponto.")

    def test_empty_text_is_ignored_without_affecting_order(self):
        segments = [{"text": ""}, {"text": "  "}, {"text": "A"}, {"text": "B"}]
        self.assertEqual(format_transcript(segments), "A B")

    def test_timestamped_format_has_time_and_track_label(self):
        segments = [
            {"track": "microphone", "start": 3661.8, "text": "Olá"},
            {"track": "system", "start": 5, "text": "Mundo"},
        ]
        self.assertEqual(format_transcript(segments, TIMESTAMPED),
                         "01:01:01 Microfone: Olá\n\n00:00:05 Áudio do sistema: Mundo")

    def test_streaming_does_not_drop_long_text(self):
        text = "palavra " * 5000
        result = "\n\n".join(iter_formatted_transcript([{"text": text}], paragraph_chars=20))
        self.assertEqual(result.replace(" ", "").strip(), text.replace(" ", "").strip())

    def test_preview_reports_truncation(self):
        preview = preview_transcript([{"text": "abcdef"}], max_chars=3)
        self.assertEqual((preview.text, preview.truncated), ("abc", True))

    def test_preview_stops_consuming_after_ceiling(self):
        def segments():
            yield {"start": 0, "text": "first"}
            raise AssertionError("preview consumed beyond its display ceiling")

        preview = preview_transcript(segments(), TIMESTAMPED, max_chars=3)
        self.assertEqual(preview.text, "00:")
        self.assertTrue(preview.truncated)

    def test_invalid_style_is_rejected(self):
        with self.assertRaises(ValueError):
            format_transcript([], "json")

    def test_summary_is_readable(self):
        result = format_summary({
            "summary": "A equipe alinhou o próximo passo.",
            "decisions": [{"text": "Publicar na sexta."}],
            "action_items": [{"text": "Preparar anúncio", "owner": "Ana", "deadline": "sexta"}],
        })
        self.assertEqual(result, "A equipe alinhou o próximo passo.\n\nDecisões:\n• Publicar na sexta.\n\nAções:\n• Preparar anúncio (responsável: Ana; prazo: sexta)")

    def test_summary_report_envelope_is_readable(self):
        self.assertEqual(format_summary({"generated": {"summary": {"text": "Gerado."}}}), "Gerado.")


if __name__ == "__main__":
    unittest.main()
