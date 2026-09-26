"""Focused interaction and layout checks for the standalone meeting chat."""

import tkinter as tk
import unittest
from types import SimpleNamespace

from meeting_chat import MeetingChat
import ui_theme


def descendants(widget):
    result = []
    for child in widget.winfo_children():
        result.append(child)
        result.extend(descendants(child))
    return result


class MeetingChatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
            cls.root.withdraw()
        except tk.TclError as exc:
            raise unittest.SkipTest(f"Tk initialization unavailable: {exc}") from exc
        cls.theme = ui_theme.build_theme("light", system="windows")

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def setUp(self):
        self.events = []
        self.window = tk.Toplevel(self.root)
        self.window.geometry("360x320")
        self.chat = MeetingChat(
            self.window, self.theme,
            on_send=lambda value: self.events.append(("send", value)),
            on_new=lambda: self.events.append(("new",)),
            on_save=lambda value: self.events.append(("save", value)),
            on_copy=lambda value: self.events.append(("copy", value)),
            on_source=lambda turn, source: self.events.append(("source", turn, source)),
        )
        self.chat.pack(fill="both", expand=True)
        self.root.update()

    def tearDown(self):
        self.window.destroy()
        self.root.update()

    def _buttons(self, text):
        return [item for item in descendants(self.chat)
                if isinstance(item, tk.Button) and item.cget("text") == text]

    def test_empty_state_suggestion_and_keyboard_composer(self):
        self.assertTrue(self.chat._empty.winfo_ismapped())
        self.assertEqual(len(self._buttons("Resuma os pontos principais")), 1)
        self._buttons("Resuma os pontos principais")[0].invoke()
        self.assertEqual(self.chat.get_question(), "Resuma os pontos principais")

        self.chat.set_question("uma pergunta")
        self.chat._composer_return(SimpleNamespace(state=0))
        self.assertEqual(self.events, [("send", "uma pergunta")])

        self.chat.set_question("linha 1")
        self.chat._composer_return(SimpleNamespace(state=1))
        self.assertEqual(self.chat.get_question(), "linha 1\n")
        self.assertEqual(self.events, [("send", "uma pergunta")])

    def test_render_turn_states_and_answer_actions_keep_ids(self):
        self.chat.render([
            {"id": 11, "question": "Q1", "answer": "A1", "status": "complete",
             "citations": ["c-1", "c-2"], "saved": False, "saving": False},
            {"id": 12, "question": "Q2", "answer": "", "status": "pending",
             "citations": [], "saved": False, "saving": False},
            {"id": 13, "question": "Q3", "answer": "", "status": "error",
             "error": "Falhou", "uncertainty": "Tente novamente.", "citations": [],
             "saved": False, "saving": False},
        ])
        self.root.update()
        self.assertFalse(self.chat._empty.winfo_ismapped())
        self.assertEqual(len(self._buttons("Fonte 1")), 1)
        self.assertEqual(len(self._buttons("Fonte 2")), 1)
        self._buttons("Fonte 2")[0].invoke()
        self._buttons("Copiar")[0].invoke()
        self._buttons("Salvar")[0].invoke()
        self.assertIn(("source", 11, "c-2"), self.events)
        self.assertIn(("copy", 11), self.events)
        self.assertIn(("save", 11), self.events)
        labels = [item.cget("text") for item in descendants(self.chat) if isinstance(item, tk.Label)]
        self.assertIn("Pensando…", labels)
        self.assertIn("Falhou", labels)
        self.assertIn("Tente novamente.", labels)

    def test_controls_disable_send_during_busy_and_save_when_not_allowed(self):
        self.chat.set_question("pergunta")
        self.chat.set_controls(busy=True, can_save=False, can_send=True)
        self.assertEqual(self.chat.send_button.cget("state"), "disabled")
        self.assertEqual(self.chat.new_button.cget("state"), "disabled")
        self.chat._send()
        self.assertEqual(self.events, [])

        self.chat.set_controls(busy=False, can_save=False, can_send=True)
        self.chat.render([{"id": 3, "question": "q", "answer": "a", "status": "complete"}])
        self.root.update()
        self.assertEqual(self.chat.send_button.cget("state"), "normal")
        self.assertEqual(self._buttons("Salvar")[0].cget("state"), "disabled")

    def test_long_turn_wraps_and_composer_stays_visible_at_small_height(self):
        self.chat.set_height(320)
        self.chat.render([{
            "id": 1, "question": "Q" * 500, "answer": "A" * 5000,
            "status": "complete", "citations": ["source"],
        }])
        self.root.update()
        self.assertGreaterEqual(self.chat.winfo_height(), 240)
        self.assertTrue(self.chat.composer.winfo_ismapped())
        self.assertGreater(self.chat.composer.winfo_width(), 0)
        self.window.geometry("720x550")
        self.root.update()
        self.assertTrue(self.chat.composer.winfo_ismapped())
        self.assertGreater(self.chat.composer.winfo_width(), 300)


if __name__ == "__main__":
    unittest.main()
