from datetime import datetime, timedelta, timezone
import unittest
from unittest import mock

import i18n
from meeting_text import (
    TIMESTAMPED,
    format_expiry,
    format_local_timestamp,
    format_storage_size,
    format_summary,
    format_transcript,
    iter_formatted_transcript,
    preview_transcript,
    seconds_until,
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

    def test_summary_preserves_all_builtin_sections_in_template_order(self):
        result = format_summary({
            "generated": {
                "follow_up_email": {"subject": "Próximos passos", "body": "Obrigado pela conversa."},
                "risks": ["Prazo apertado."],
                "summary": {"text": "A conversa alinhou o plano."},
                "key_points": ["Cliente prioriza simplicidade."],
                "open_questions": ["Quem aprova o orçamento?"],
                "feedback": ["A demonstração foi clara."],
                "objections": ["O prazo parece curto."],
                "decisions": [{"text": "Fazer um piloto."}],
                "action_items": [{"text": "Enviar proposta", "owner": "Ana", "deadline": "sexta"}],
            },
        })
        self.assertEqual(result, "\n\n".join((
            "A conversa alinhou o plano.",
            "Pontos principais:\n• Cliente prioriza simplicidade.",
            "Decisões:\n• Fazer um piloto.",
            "Feedback:\n• A demonstração foi clara.",
            "Objeções:\n• O prazo parece curto.",
            "Riscos:\n• Prazo apertado.",
            "Questões em aberto:\n• Quem aprova o orçamento?",
            "Ações:\n• Enviar proposta (responsável: Ana; prazo: sexta)",
            "E-mail de acompanhamento:\nAssunto: Próximos passos\nObrigado pela conversa.",
        )))

    def test_summary_accepts_reviewed_sections_and_omits_empty_sections(self):
        self.assertEqual(format_summary({
            "sections": {
                "summary": "Revisado.", "feedback": "Bom retorno.",
                "risks": [], "action_items": [],
            },
        }), "Revisado.\n\nFeedback:\n• Bom retorno.")

    def test_summary_library_envelope_applies_reviewed_section_overrides(self):
        self.assertEqual(format_summary({
            "generated": {"summary": {"text": "Gerado."}, "risks": ["Risco original"]},
            "reviewed_artifact": {"sections": {"summary": "Revisado.", "risks": "Risco confirmado."}},
        }), "Revisado.\n\nRiscos:\n• Risco confirmado.")

    def test_summary_renders_detailed_notes_in_chronological_order(self):
        result = format_summary({
            "summary": "Visão geral.",
            "detailed_notes": [
                {"start": 3661, "end": 3663, "text": "Último", "segment_ids": ["s3"]},
                {"start": 0, "end": 2, "text": "Primeiro\nlinha dois", "segment_ids": ["s1"]},
                {"start": 61, "end": 64, "text": "Meio", "segment_ids": ["s2"]},
            ],
        })
        self.assertIn("Notas detalhadas:", result)
        self.assertLess(result.index("00:00:00–00:00:02"), result.index("00:01:01–00:01:04"))
        self.assertIn("01:01:01–01:01:03", result)
        self.assertIn("Primeiro\nlinha dois", result)
        self.assertIn("citações: s2", result)

    def test_reviewed_detailed_notes_override_generated_notes(self):
        result = format_summary({
            "generated": {"summary": "Gerado.", "detailed_notes": "Original."},
            "reviewed_artifact": {"sections": {"detailed_notes": "Revisado."}},
        })
        self.assertIn("Revisado.", result)
        self.assertNotIn("Original.", result)


# Wednesday 2026-10-21 14:30 in UTC-3: every fixture below is read in this zone.
BRT = timezone(timedelta(hours=-3))
NOW = datetime(2026, 10, 21, 14, 30, tzinfo=BRT)


class LocalTimestampTests(unittest.TestCase):
    def tearDown(self):
        i18n.set_language(i18n.DEFAULT_LANGUAGE)

    def test_store_utc_with_z_is_shown_in_local_time(self):
        # 12:02Z is 09:02 in UTC-3, the same calendar day.
        self.assertEqual(format_local_timestamp("2026-10-21T12:02:00Z", now=NOW), "Hoje 09:02")
        self.assertEqual(format_local_timestamp("2026-10-21T12:02:00+00:00", now=NOW), "Hoje 09:02")
        # An offset-less value is the store's UTC, never local time.
        self.assertEqual(format_local_timestamp("2026-10-21T12:02:00", now=NOW), "Hoje 09:02")

    def test_utc_midnight_crossing_lands_on_the_local_day(self):
        # 02:15Z on the 21st is still the evening of the 20th in UTC-3.
        self.assertEqual(format_local_timestamp("2026-10-21T02:15:00Z", now=NOW), "Ontem 23:15")

    def test_yesterday_last_week_this_year_and_older(self):
        self.assertEqual(format_local_timestamp("2026-10-20T12:10:00Z", now=NOW), "Ontem 09:10")
        self.assertEqual(format_local_timestamp("2026-10-19T17:05:00Z", now=NOW), "seg. 19/10 14:05")
        self.assertEqual(format_local_timestamp("2026-10-15T12:00:00Z", now=NOW), "qui. 15/10 09:00")
        self.assertEqual(format_local_timestamp("2026-10-14T12:00:00Z", now=NOW), "14/10 09:00")
        self.assertEqual(format_local_timestamp("2026-01-02T12:00:00Z", now=NOW), "02/01 09:00")
        self.assertEqual(format_local_timestamp("2025-12-30T12:00:00Z", now=NOW), "30/12/2025")

    def test_english_uses_month_names_and_its_own_order(self):
        i18n.set_language("en-US")
        self.assertEqual(format_local_timestamp("2026-10-21T12:02:00Z", now=NOW), "Today 09:02")
        self.assertEqual(format_local_timestamp("2026-10-20T12:10:00Z", now=NOW), "Yesterday 09:10")
        self.assertEqual(format_local_timestamp("2026-10-19T17:05:00Z", now=NOW), "Mon Oct 19 14:05")
        self.assertEqual(format_local_timestamp("2026-10-14T12:00:00Z", now=NOW), "Oct 14 09:00")
        self.assertEqual(format_local_timestamp("2025-12-30T12:00:00Z", now=NOW), "Dec 30, 2025")

    def test_missing_or_invalid_input_is_undated_never_a_made_up_time(self):
        for value in (None, "", "   ", "yesterday", "2026-13-40T99:00:00Z", 1760000000):
            with self.subTest(value=value):
                self.assertEqual(format_local_timestamp(value, now=NOW), "Sem data")

    def test_an_instant_the_platform_cannot_convert_is_undated_not_an_error(self):
        # Year 1 minus a negative offset is year 0: the conversion overflows on every platform.
        self.assertEqual(format_local_timestamp("0001-01-01T00:00:00Z", now=NOW), "Sem data")
        # Windows refuses the system-zone conversion of instants such as 1960 (OSError).
        refusing = mock.Mock()
        refusing.astimezone.side_effect = OSError(22, "Invalid argument")
        with mock.patch("meeting_text._parse_timestamp", return_value=refusing):
            self.assertEqual(format_local_timestamp("1960-01-01T00:00:00Z"), "Sem data")
        refusing.astimezone.assert_called_once_with()

    def test_a_naive_clock_is_rejected(self):
        with self.assertRaises(ValueError):
            format_local_timestamp("2026-10-21T12:02:00Z", now=datetime(2026, 10, 21, 14, 30))
        # Rejecting the clock is not hidden behind the unconvertible-instant fallback.
        with self.assertRaises(ValueError):
            format_local_timestamp("0001-01-01T00:00:00Z", now=datetime(2026, 10, 21, 14, 30))

    def test_default_clock_reads_the_system_zone(self):
        stamp = datetime.now(timezone.utc).isoformat()
        self.assertTrue(format_local_timestamp(stamp).startswith("Hoje "))


class ExpiryAndSizeTests(unittest.TestCase):
    def tearDown(self):
        i18n.set_language(i18n.DEFAULT_LANGUAGE)

    def test_expiry_counts_down_in_whole_units_until_it_passes(self):
        now = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(format_expiry("2026-11-13T18:00:00Z", now=now), "em 23 d")
        self.assertEqual(format_expiry("2026-10-21T15:59:00Z", now=now), "em 3 h")
        self.assertEqual(format_expiry("2026-10-21T12:04:30Z", now=now), "em 4 min")
        self.assertEqual(format_expiry("2026-10-21T12:00:20Z", now=now), "em 1 min")
        self.assertEqual(format_expiry("2026-10-21T12:00:00Z", now=now), "Expirada")
        self.assertEqual(format_expiry("2026-09-01T00:00:00Z", now=now), "Expirada")
        self.assertEqual(format_expiry("", now=now), "Sem data")
        i18n.set_language("en-US")
        self.assertEqual(format_expiry("2026-11-13T18:00:00Z", now=now), "in 23 d")
        self.assertEqual(format_expiry("2026-09-01T00:00:00Z", now=now), "Expired")

    def test_seconds_until_is_signed_and_none_when_unknown(self):
        now = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(seconds_until("2026-10-21T12:01:00Z", now=now), 60)
        self.assertEqual(seconds_until("2026-10-21T11:59:00Z", now=now), -60)
        self.assertIsNone(seconds_until("not a date", now=now))

    def test_sizes_use_decimal_units_and_the_language_decimal_mark(self):
        self.assertEqual(format_storage_size(0), "0 B")
        self.assertEqual(format_storage_size(512), "512 B")
        self.assertEqual(format_storage_size(12_300), "12,3 kB")
        self.assertEqual(format_storage_size(46_213_377), "46,2 MB")
        self.assertEqual(format_storage_size(462_000_000), "462 MB")
        self.assertEqual(format_storage_size(1_288_490_188), "1,3 GB")
        i18n.set_language("en-US")
        self.assertEqual(format_storage_size(1_288_490_188), "1.3 GB")

    def test_rounding_at_a_unit_boundary_keeps_three_digits_and_the_right_unit(self):
        for size, text in (
            (99_949_999, "99,9 MB"), (99_960_000, "100 MB"), (99_960, "100 kB"),
            (999_960, "1,0 MB"), (999_950, "1,0 MB"), (999_499, "999 kB"),
            (999_960_000, "1,0 GB"), (999_999_999, "1,0 GB"), (999.9, "999 B"),
        ):
            with self.subTest(size=size):
                self.assertEqual(format_storage_size(size), text)
        # GB is the largest unit, so it is the only one allowed to show four digits.
        self.assertEqual(format_storage_size(1_500_000_000_000), "1500 GB")

    def test_invalid_sizes_fail_loudly(self):
        for value in (-1, float("nan"), float("inf"), True, "12", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                format_storage_size(value)


if __name__ == "__main__":
    unittest.main()
