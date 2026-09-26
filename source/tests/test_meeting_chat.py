"""Focused interaction and layout checks for the standalone meeting chat."""

import gc
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
        cls.root = None
        gc.collect()

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
        self.chat = self.window = None
        # Tk variables must be finalized on their creating thread, before a
        # later controller worker can trigger collection of widget cycles.
        gc.collect()

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
        messages = [item.cget("text") for item in descendants(self.chat) if isinstance(item, tk.Message)]
        self.assertIn("Pensando…", messages)
        self.assertIn("Falhou", messages)
        self.assertIn("Tente novamente.", messages)

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
        self.chat.set_controls(busy=True, can_save=True, can_send=True)
        self.assertEqual(self._buttons("Copiar")[0].cget("state"), "normal")
        self.assertEqual(self._buttons("Salvar")[0].cget("state"), "disabled")

    def test_long_turn_wraps_and_composer_stays_visible_at_small_height(self):
        self.chat.set_height(320)
        self.chat.render([{
            "id": 1, "question": "Q" * 500, "answer": "A" * 5000,
            "status": "complete", "citations": ["source"],
        }])
        self.chat.status.set("Uma mensagem de status suficientemente longa para testar a quebra responsiva no rodapé.")
        self.root.update()
        self.assertGreaterEqual(self.chat.winfo_height(), 240)
        self.assertTrue(self.chat.composer.winfo_ismapped())
        self.assertTrue(self.chat.send_button.winfo_ismapped())
        self.assertGreater(self.chat.composer.winfo_width(), 0)
        self.assertGreater(self.chat._message_widgets[0][0].winfo_height(), 0)
        self.assertGreater(self.chat._message_widgets[1][0].winfo_height(), 0)
        self.assertLessEqual(self.chat._message_widgets[1][0].cget("width"), self.chat.canvas.winfo_width())
        for message, _inset in self.chat._message_widgets[:2]:
            self.assertLessEqual(
                int(message.cget("width")) + 2 * self.theme.space_sm,
                message.winfo_width(),
            )
        self.assertLessEqual(
            self.chat.send_button.winfo_rootx() + self.chat.send_button.winfo_width(),
            self.chat.winfo_rootx() + self.chat.winfo_width(),
        )
        self.assertLessEqual(
            self.chat.send_button.winfo_rooty() + self.chat.send_button.winfo_height(),
            self.chat.winfo_rooty() + self.chat.winfo_height(),
        )
        self.assertLessEqual(
            self.chat.composer.winfo_rooty() + self.chat.composer.winfo_height(),
            self.chat.winfo_rooty() + self.chat.winfo_height(),
        )
        self.window.geometry("720x550")
        self.root.update()
        self.assertTrue(self.chat.composer.winfo_ismapped())
        self.assertGreater(self.chat.composer.winfo_width(), 300)

    def test_empty_populated_switch_tolerates_late_resize_during_child_teardown(self):
        self.chat.render([])
        self.chat.render([{"id": 1, "question": "Q", "answer": "A", "status": "complete",
                           "uncertainty": "medium"}])
        self.root.update()
        self.chat.status.set("Atualizando a resposta…")
        self.chat._empty_title.destroy()
        # A queued Configure can arrive after a hidden empty-state child has
        # been destroyed while the chat window is being rebuilt or closed.
        callback_errors = []
        previous_reporter = self.root.report_callback_exception
        self.root.report_callback_exception = lambda *args: callback_errors.append(args)
        try:
            self.chat._composer.event_generate("<Configure>")
            self.root.update()
        finally:
            self.root.report_callback_exception = previous_reporter
        self.assertEqual(callback_errors, [])
        messages = [item.cget("text") for item in descendants(self.chat) if isinstance(item, tk.Message)]
        self.assertIn("Incerteza média", messages)


if __name__ == "__main__":
    unittest.main()
