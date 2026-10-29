"""Library actions for the Tauri manager (``manager_library``).

The controller is a fake that records calls, so every refusal and the
single-use trash plan are checked without a library on disk. The cancellation
test waits on events with timeouts, never bare sleeps.
"""

import os
import sys
import threading
import types
import unittest

SOURCE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, SOURCE_DIR)

import i18n
from manager_bridge import ManagerApi, RpcFailure
from manager_library import CANCELLED, LibraryActions

WAIT = 5.0
PROFILES = [
    {"id": "interview", "name": "Entrevista", "builtin": True},
    {"id": "meeting_notes", "name": "Notas de reunião", "builtin": True},
    {"id": "custom", "name": "Meu formato", "version": 2, "disabled": True},
]


class FakeLibrary:
    def __init__(self):
        self.calls = []
        self.sessions = {"s1": {"id": "s1", "title": "Weekly  planning", "duration": 754.0,
                                "revisions": [{"id": "r1"}], "summary": None}}
        self.reports = []
        self.report_bodies = {}
        self.qa = "explicit_save"
        self.inventory = {"final": 2048, "final_format": "MP3", "microphone": 10, "system": None}
        self.plan = {"session_id": "s1", "operation": "whole_meeting", "eligible": True,
                     "byte_estimate": 4096, "targets": [], "policy": {"purge_after_days": 30}}
        self.trash_entries = []
        self.generate_started = threading.Event()
        self.block_generate = False
        self.refusal = None

    def get_session(self, session_id):
        return self.sessions.get(session_id)

    def list_report_profiles(self, language=None):
        return [dict(item) for item in PROFILES]

    def list_reports(self, session_id, limit=None):
        return list(self.reports)

    def get_report(self, session_id, report_id):
        return self.report_bodies[report_id]

    def qa_mode(self):
        return self.qa

    def generate_report(self, session_id, model, *, profile=None, revision=None, focus=None, cancel_event=None):
        self.calls.append(("generate_report", session_id, model, profile["id"], revision, focus))
        self.generate_started.set()
        if self.block_generate:
            cancel_event.wait(WAIT)
            raise RuntimeError("A operação foi cancelada; o áudio salvo e os arquivos anteriores foram preservados.")
        self.reports.append({"id": "rep2", "kind": "report"})
        self.report_bodies["rep2"] = {"profile_id": profile["id"], "generated": {"summary": "Novo resumo"}}
        return {"report_id": "rep2"}

    def ask_this_meeting(self, session_id, question, model, *, revision=None, history=None, cancel_event=None):
        self.calls.append(("ask", session_id, question, history))
        if self.refusal:
            raise self.refusal
        return {"answer": "Decidimos manter a data.", "citations": ["seg1", "seg2"], "uncertainty": "low",
                "revision": revision}

    def save_answer(self, session_id, answer, model, *, question="", revision=None):
        self.calls.append(("save_answer", session_id, answer["answer"], question, revision))
        return {"report_id": "qa1"}

    def audio_inventory(self, session_id):
        return dict(self.inventory)

    def export(self, session_id, path, format="markdown"):
        self.calls.append(("export", session_id, path, format))
        return path

    def export_final_audio(self, session_id, path):
        self.calls.append(("export_final_audio", session_id, path))
        return path

    def recover_audio(self, session_id, settings=None):
        return True

    def import_audio(self, path, settings):
        self.calls.append(("import_audio", path))
        return "s9"

    def retention_plan(self, session_id, policy=None):
        self.calls.append(("retention_plan", session_id, policy))
        return dict(self.plan)

    def apply_retention(self, plan, *, confirm=False):
        self.calls.append(("apply_retention", plan["session_id"], confirm))
        return True

    def restore_session(self, session_id):
        self.calls.append(("restore_session", session_id))
        return True

    def list_trash(self):
        return list(self.trash_entries)

    def purge_session(self, session_id, *, confirm=False):
        self.calls.append(("purge_session", session_id, confirm))
        return True

    def empty_trash(self, *, confirm=False):
        self.calls.append(("empty_trash", confirm))
        return True


def make_actions(library=None, *, ready=True, installed=True, path="", language="pt-BR"):
    library = library or FakeLibrary()
    dialogs = []

    def choose_path(kind, title, filename, filetypes):
        dialogs.append((kind, title, filename, filetypes))
        return path

    actions = LibraryActions(
        meetings=library,
        meeting_settings=lambda: types.SimpleNamespace(summary_model="qwen", language=language),
        summary_model_installed=lambda model: installed,
        choose_path=choose_path,
        startup_status=lambda: (ready, "" if ready else "A recuperação local exige reconciliação manual."),
    )
    return actions, library, dialogs


class SummaryTests(unittest.TestCase):
    def tearDown(self):
        i18n.set_language("pt-BR")

    def test_the_newest_report_is_shown_with_its_reviewed_sections(self):
        actions, library, _dialogs = make_actions()
        library.reports = [{"id": "rep1", "kind": "report"}, {"id": "qa1", "kind": "qa"}]
        library.report_bodies["rep1"] = {"profile_id": "interview", "generated": {"summary": "Gerado"},
                                         "reviewed_artifact": {"sections": {"summary": "Revisado"}}}
        view = actions.summary({"id": "s1"})
        self.assertIn("Revisado", view["text"])
        self.assertNotIn("Gerado", view["text"])
        self.assertEqual(view["name"], "Entrevista")
        self.assertEqual([item["id"] for item in view["profiles"]], ["meeting_notes", "interview", "custom"])
        self.assertEqual(view["profileId"], "meeting_notes")
        self.assertEqual(view["profiles"][2]["name"], "Meu formato · v2")
        self.assertTrue(view["profiles"][2]["disabled"])
        self.assertTrue(view["modelReady"])
        self.assertTrue(view["canSaveAnswers"])

    def test_without_reports_the_session_summary_and_a_setup_hint_show(self):
        actions, library, _dialogs = make_actions(installed=False)
        library.qa = "memory_only"
        view = actions.summary({"id": "s1"})
        self.assertEqual(view["text"], "")
        self.assertFalse(view["canSaveAnswers"])
        self.assertIn("Configurações → Modelos", view["hint"])

    def test_generation_is_refused_before_the_engine_runs(self):
        cases = [
            ({"profileId": "missing"}, {}, "Escolha um formato"),
            ({"profileId": "custom"}, {}, "Ative este formato"),
            ({"profileId": "meeting_notes", "focus": "x" * 601}, {}, "600 caracteres"),
            ({"profileId": "meeting_notes"}, {"installed": False}, "Instale um modelo"),
        ]
        for params, options, words in cases:
            with self.subTest(words=words):
                actions, library, _dialogs = make_actions(**options)
                with self.assertRaises(RpcFailure) as caught:
                    actions.generate_summary({"id": "s1", **params})
                self.assertIn(words, caught.exception.message)
                self.assertEqual(library.calls, [])

    def test_an_untranscribed_recording_cannot_be_summarized(self):
        actions, library, _dialogs = make_actions()
        library.sessions["s1"]["revisions"] = []
        with self.assertRaises(RpcFailure) as caught:
            actions.generate_summary({"id": "s1", "profileId": "meeting_notes"})
        self.assertIn("Transcreva", caught.exception.message)

    def test_generation_uses_the_engine_profile_and_returns_the_new_version(self):
        actions, library, _dialogs = make_actions()
        view = actions.generate_summary({"id": "s1", "profileId": "meeting_notes", "focus": "  decisões  "})
        self.assertEqual(library.calls, [("generate_report", "s1", "qwen", "meeting_notes", "r1", "decisões")])
        self.assertIn("Novo resumo", view["text"])

    def test_a_cancelled_generation_answers_cancelled_and_frees_the_slot(self):
        actions, library, _dialogs = make_actions()
        library.block_generate = True
        outcome = {}

        def run():
            try:
                actions.generate_summary({"id": "s1", "profileId": "meeting_notes"})
            except RpcFailure as failure:
                outcome["failure"] = failure

        worker = threading.Thread(target=run)
        worker.start()
        self.assertTrue(library.generate_started.wait(WAIT))
        with self.assertRaises(RpcFailure):
            actions.generate_summary({"id": "s1", "profileId": "meeting_notes"})
        self.assertEqual(actions.cancel_job({"kind": "summary"}), {"cancelled": True})
        worker.join(WAIT)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome["failure"].code, CANCELLED)
        self.assertEqual(actions.cancel_job({"kind": "summary"}), {"cancelled": False})


class ChatTests(unittest.TestCase):
    def test_only_complete_recent_turns_reach_the_engine(self):
        actions, library, _dialogs = make_actions()
        history = [{"question": f"q{index}", "answer": f"a{index}"} for index in range(8)]
        history.append({"question": "half", "answer": None})
        answer = actions.ask({"id": "s1", "question": "  E agora?  ", "history": history})
        _kind, _session, question, sent = library.calls[0]
        self.assertEqual(question, "E agora?")
        self.assertEqual([item["question"] for item in sent], ["q2", "q3", "q4", "q5", "q6", "q7"])
        self.assertEqual(answer["sources"], 2)
        self.assertEqual(answer["uncertainty"], "Incerteza baixa")

    def test_a_saved_answer_is_the_one_the_engine_gave(self):
        actions, library, _dialogs = make_actions()
        answer = actions.ask({"id": "s1", "question": "Decisões?", "history": []})
        with self.assertRaises(RpcFailure):
            actions.save_answer({"id": "other", "answerId": answer["answerId"]})
        self.assertEqual(actions.save_answer({"id": "s1", "answerId": answer["answerId"]}), {"saved": True})
        self.assertEqual(library.calls[-1], ("save_answer", "s1", "Decidimos manter a data.", "Decisões?", "r1"))
        with self.assertRaises(RpcFailure):
            actions.save_answer({"id": "s1", "answerId": answer["answerId"]})

    def test_an_engine_refusal_keeps_its_words_without_local_paths(self):
        actions, library, _dialogs = make_actions()
        library.refusal = RuntimeError(r"Aguarde a gravação terminar. C:\Users\someone\library\s1")
        with self.assertRaises(RpcFailure) as caught:
            actions.ask({"id": "s1", "question": "Oi?", "history": []})
        self.assertTrue(caught.exception.message.startswith("Aguarde a gravação terminar."))
        self.assertNotIn("someone", caught.exception.message)

    def test_questions_are_bounded(self):
        actions, library, _dialogs = make_actions()
        for question in ("   ", "x" * 2001):
            with self.subTest(length=len(question)), self.assertRaises(RpcFailure):
                actions.ask({"id": "s1", "question": question, "history": []})
        self.assertEqual(library.calls, [])


class FileTests(unittest.TestCase):
    def test_a_cancelled_dialog_exports_nothing(self):
        actions, library, dialogs = make_actions(path="")
        self.assertEqual(actions.export_text({"id": "s1", "format": "markdown"}), {"saved": False})
        self.assertEqual(library.calls, [])
        self.assertEqual(dialogs[0][:3], ("save", "Exportar transcrição", "Weekly planning.md"))

    def test_export_writes_the_chosen_path_and_names_only_the_file(self):
        target = os.path.join(os.sep, "exports", "notes.txt")
        actions, library, _dialogs = make_actions(path=target)
        self.assertEqual(actions.export_text({"id": "s1", "format": "text"}), {"saved": True, "name": "notes.txt"})
        self.assertEqual(library.calls, [("export", "s1", target, "text")])

    def test_final_audio_needs_a_saved_final(self):
        actions, library, dialogs = make_actions(path="x.mp3")
        library.inventory = {"final": None, "final_format": "", "microphone": 10, "system": None}
        self.assertEqual(actions.files({"id": "s1"}), {"final": None, "canPrepare": True})
        with self.assertRaises(RpcFailure):
            actions.export_audio({"id": "s1"})
        self.assertEqual(dialogs, [])

    def test_import_opens_audio_and_returns_the_new_recording(self):
        actions, library, dialogs = make_actions(path="call.m4a")
        self.assertEqual(actions.import_audio({}), {"id": "s9"})
        self.assertEqual(dialogs[0][0], "open")
        self.assertEqual(library.calls, [("import_audio", "call.m4a")])


class TrashTests(unittest.TestCase):
    def test_the_confirmed_plan_is_the_one_applied_once(self):
        actions, library, _dialogs = make_actions()
        preview = actions.trash_preview({"id": "s1"})
        self.assertTrue(preview["eligible"])
        self.assertIn("Weekly planning", preview["question"])
        self.assertIn("00:12:34", preview["question"])
        self.assertEqual(actions.trash({"token": preview["token"]}), {"id": "s1"})
        self.assertEqual(library.calls[-1], ("apply_retention", "s1", True))
        with self.assertRaises(RpcFailure):
            actions.trash({"token": preview["token"]})

    def test_a_blocked_plan_explains_itself_and_offers_no_token(self):
        actions, library, _dialogs = make_actions()
        library.plan.update(eligible=False, reasons=["Exportação em andamento"])
        preview = actions.trash_preview({"id": "s1"})
        self.assertEqual(preview["reasons"], "Exportação em andamento")
        self.assertNotIn("token", preview)

    def test_destructive_work_waits_for_startup_recovery(self):
        actions, library, _dialogs = make_actions(ready=False)
        for call in (lambda: actions.trash_preview({"id": "s1"}), lambda: actions.trash_list({}),
                     lambda: actions.purge({"id": "s1"}), lambda: actions.empty_expired({})):
            with self.assertRaises(RpcFailure):
                call()
        self.assertEqual(library.calls, [])

    def test_the_trash_counts_expired_recordings(self):
        actions, library, _dialogs = make_actions()
        library.trash_entries = [
            {"session_id": "20261001-abcdef1234", "deleted_at": "2026-10-01T10:00:00Z",
             "purge_after": "2026-10-02T10:00:00Z", "byte_estimate": 1024},
            {"session_id": "20261020-ffff0000", "deleted_at": "2026-10-20T10:00:00Z",
             "purge_after": "2999-01-01T00:00:00Z", "byte_estimate": 0},
        ]
        listed = actions.trash_list({})
        self.assertEqual([item["expired"] for item in listed["items"]], [True, False])
        self.assertEqual(listed["items"][0]["name"], "…cdef1234")
        self.assertEqual(listed["expired"], 1)
        self.assertEqual(listed["summary"], "2 gravações na lixeira · 1 expirada")

    def test_purge_and_empty_confirm_to_the_engine(self):
        actions, library, _dialogs = make_actions()
        actions.purge({"id": "s1"})
        actions.empty_expired({})
        self.assertEqual(library.calls, [("purge_session", "s1", True), ("empty_trash", True)])


class ManagerApiCompositionTests(unittest.TestCase):
    def test_library_methods_join_the_bridge(self):
        actions, _library, _dialogs = make_actions()
        api = ManagerApi(
            meetings=None, voice=None, settings_getter=dict, persist_settings=lambda _p: True,
            startup_status=lambda: (True, ""), voice_presentation=lambda: ("", None),
            history_status=lambda _s: ("", None), set_voice_enabled=lambda *_a: {}, on_language_changed=lambda _c: None,
            display_name="SnipVoice", version="1.3.0", extensions=[actions],
        )
        methods = api.methods()
        self.assertIs(methods["library.summary"].__self__, actions)
        self.assertIn("trash.list", methods)
        self.assertIn("app.hello", methods)


if __name__ == "__main__":
    unittest.main()
