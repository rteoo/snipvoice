import time
import unittest

from meeting_titles import initial_recording_title, refine_recording_title


class MeetingTitleTests(unittest.TestCase):
    def test_empty_title_gets_local_timestamp_and_three_word_placeholder(self):
        stamp = time.mktime((2026, 9, 15, 14, 7, 0, 0, 0, -1))
        self.assertEqual(
            initial_recording_title("  ", stamp),
            "2026-09-15-14-07-Gravacao-sem-transcricao",
        )

    def test_supplied_title_is_trimmed_and_never_replaced(self):
        self.assertEqual(initial_recording_title("  Quarterly Review  "), "Quarterly Review")
        self.assertEqual(
            refine_recording_title(
                "Quarterly Review",
                "20260915-140700-abc",
                [{"text": "planejamento do orçamento anual"}],
            ),
            "Quarterly Review",
        )

    def test_transcript_refines_placeholder_to_three_to_five_content_words(self):
        title = refine_recording_title(
            "2026-09-15-14-07-Gravacao-sem-transcricao",
            "20260915-140700-abc",
            [{"text": "Bom dia pessoal, hoje vamos discutir o planejamento do orçamento anual."}],
        )
        self.assertEqual(title, "2026-09-15-14-07-Discutir-Planejamento-Orçamento-Anual")
        self.assertGreaterEqual(len(title.split("-")[5:]), 3)
        self.assertLessEqual(len(title.split("-")[5:]), 5)

    def test_sparse_transcript_keeps_placeholder_instead_of_inventing_words(self):
        self.assertEqual(
            refine_recording_title(
                "2026-09-15-14-07-Gravacao-sem-transcricao",
                "20260915-140700-abc",
                [{"text": "Teste"}],
            ),
            "2026-09-15-14-07-Gravacao-sem-transcricao",
        )

    def test_empty_transcript_keeps_existing_placeholder(self):
        title = "2026-09-15-14-07-Gravacao-sem-transcricao"
        self.assertEqual(refine_recording_title(title, "20260915-140700-abc", []), title)


if __name__ == "__main__":
    unittest.main()
