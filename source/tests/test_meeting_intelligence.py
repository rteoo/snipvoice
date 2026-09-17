import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from meeting_intelligence import (
    BUILTIN_PROFILE_IDS,
    SUPPORTED_SECTIONS,
    MeetingIntelligence,
    profile_hash,
    validate_profile,
)
from meeting_store import MeetingStore
from meeting_library import MeetingLibrary
from summary_catalog import DEFAULT_SUMMARY_MODEL


class MeetingIntelligenceProfileTests(unittest.TestCase):
    def test_builtin_profiles_are_bounded_and_deterministic_in_both_languages(self):
        for language in ("pt-BR", "en-US"):
            first = MeetingIntelligence.builtin_profiles(language)
            second = MeetingIntelligence.builtin_profiles(language)
            self.assertEqual(first, second)
            self.assertEqual({item["id"] for item in first}, set(BUILTIN_PROFILE_IDS))
            for profile in first:
                clean = validate_profile(profile, language=language)
                self.assertEqual(clean["profile_hash"], profile_hash(clean))
                self.assertTrue(set(clean["sections"]).issubset(SUPPORTED_SECTIONS))

    def test_malformed_custom_profile_is_rejected_before_workspace_mutation(self):
        valid = {
            "id": "customer-follow-up",
            "name": "Customer follow-up",
            "instructions": "Focus on confirmed customer feedback.",
            "sections": ["summary", "feedback", "follow_up_email"],
        }
        clean = validate_profile(valid)
        self.assertEqual(clean["version"], 1)
        self.assertEqual(clean["profile_hash"], profile_hash(clean))
        for mutation in (
            {"sections": ["summary", "summary"]},
            {"sections": ["unknown"]},
            {"name": "x" * 129},
            {"schema_version": 2},
            {"id": "one-on-one"},
        ):
            candidate = dict(valid, **mutation)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                validate_profile(candidate)

    def test_custom_profile_update_increments_version_and_hash(self):
        with tempfile.TemporaryDirectory(dir=self.temp_dir()) as root:
            library = MeetingLibrary(root)
            intelligence = MeetingIntelligence(library.store)
            with mock.patch.object(library, "update_workspace", wraps=library.update_workspace) as update:
                first = intelligence.save_custom_profile({
                    "id": "custom",
                    "name": "Custom",
                    "instructions": "Use evidence.",
                    "sections": ["summary"],
                }, library)
            self.assertIn("expected_generation", update.call_args.kwargs)
            second = intelligence.save_custom_profile({
                "id": "custom",
                "name": "Custom revised",
                "instructions": "Use evidence and preserve gaps.",
                "sections": ["summary", "risks"],
            }, library)
            self.assertEqual(first["version"], 1)
            self.assertEqual(second["version"], 2)
            self.assertNotEqual(first["profile_hash"], second["profile_hash"])
            self.assertEqual(library.read_workspace()["profiles"][0]["profile_hash"],
                             second["profile_hash"])

    def test_mixed_language_profiles_are_validated_independently_and_filtered_for_selector(self):
        with tempfile.TemporaryDirectory(dir=self.temp_dir()) as root:
            library = MeetingLibrary(root)
            intelligence = MeetingIntelligence(library.store)
            intelligence.save_custom_profile({
                "id": "pt-follow-up", "name": "Acompanhamento",
                "instructions": "Use fatos confirmados.", "sections": ["summary"],
                "language": "pt-BR",
            }, library)
            intelligence.save_custom_profile({
                "id": "en-follow-up", "name": "Follow-up",
                "instructions": "Use confirmed facts.", "sections": ["summary"],
                "language": "en-US",
            }, library)

            pt_profiles = intelligence.read_profiles(library, language="pt-BR")
            en_profiles = intelligence.read_profiles(library, language="en-US")
            all_profiles = intelligence.read_profiles(library)

        self.assertIn("pt-follow-up", {item["id"] for item in pt_profiles})
        self.assertNotIn("en-follow-up", {item["id"] for item in pt_profiles})
        self.assertIn("en-follow-up", {item["id"] for item in en_profiles})
        self.assertNotIn("pt-follow-up", {item["id"] for item in en_profiles})
        self.assertTrue({"pt-follow-up", "en-follow-up"}.issubset(
            {item["id"] for item in all_profiles}
        ))

    @staticmethod
    def temp_dir():
        path = Path(__file__).resolve().parent / "tmp"
        path.mkdir(exist_ok=True)
        return str(path)


class _FakeRuntime:
    def __init__(self, response_factory):
        self.response_factory = response_factory
        self.calls = []
        self.closed = False

    def generate(self, prompt, evidence, cancel_event=None, disable_thinking=False):
        self.calls.append((prompt, evidence, cancel_event, disable_thinking))
        return self.response_factory(prompt, evidence)

    def close(self):
        self.closed = True


class MeetingIntelligenceGenerationTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).resolve().parent / "tmp"
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.store = MeetingStore(self.temp.name)
        self.session_id = self.store.begin({})
        self.store.finish(self.session_id)
        self.revision = self.store.begin_revision(self.session_id, "local", "pt")
        self.store.add_transcript(self.session_id, self.revision, {
            "id": "s1", "track": "microphone", "start": 0, "end": 5,
            "text": "Alice will review the report by Friday.",
        })
        self.store.finish_revision(self.session_id, self.revision)
        self.store.save_summary(self.session_id, {"summary": "Prior report"})

    @staticmethod
    def _report(evidence, include_decisions=True):
        identifier = evidence[0].get("id") or evidence[0].get("segment_ids", [])[0]
        result = {
            "summary": "The report will be reviewed.",
            "segment_ids": [identifier],
            "action_items": [{
                "text": "Review the report",
                "owner": "Alice",
                "deadline": "Friday",
                "segment_ids": [identifier],
            }],
        }
        if include_decisions:
            result["decisions"] = []
        return result

    def _intelligence(self, response_factory=None):
        runtime = _FakeRuntime(response_factory or (lambda _prompt, evidence: json.dumps(self._report(evidence))))
        intelligence = MeetingIntelligence(
            self.store,
            runtime_factory=lambda _path, _context: runtime,
            model_path_resolver=lambda _model: "installed.gguf",
        )
        return intelligence, runtime

    def test_generate_report_uses_profile_sections_and_saves_only_after_valid_citations(self):
        intelligence, runtime = self._intelligence(
            lambda _prompt, evidence: json.dumps(self._report(evidence, include_decisions=False))
        )
        profile = validate_profile({
            "id": "focused",
            "name": "Focused",
            "instructions": "Use only confirmed facts.",
            "sections": ["summary", "action_items"],
        })
        result = intelligence.generate_report(self.session_id, DEFAULT_SUMMARY_MODEL, profile=profile)
        self.assertEqual(set(result) & {"summary", "action_items", "decisions"},
                         {"summary", "action_items"})
        self.assertEqual(result["revision"], self.revision)
        self.assertTrue(runtime.closed)
        self.assertEqual(self.store.get(self.session_id)["summary"]["summary"], result["summary"])

    def test_library_generation_persists_immutable_report_with_model_provenance(self):
        library = MeetingLibrary(self.store, workspace_root=self.temp.name)
        intelligence, runtime = self._intelligence()
        intelligence.library = library
        profile = validate_profile({
            "id": "focused", "name": "Focused", "instructions": "",
            "sections": ["summary", "decisions", "action_items"],
        })
        result = intelligence.generate_report(
            self.session_id, DEFAULT_SUMMARY_MODEL, profile=profile,
        )
        reports = library.list_reports(self.session_id, include_legacy=False)
        self.assertEqual(len(reports), 1)
        self.assertEqual(result["report_id"], reports[0]["id"])
        self.assertEqual(len(reports[0]["model"]["sha256"]), 64)
        self.assertEqual(reports[0]["generated"]["summary"]["citations"], ["s1"])
        with self.assertRaises(FileExistsError):
            library.save_report(self.session_id, reports[0])

    def test_explicit_answer_save_is_a_qa_report_and_memory_answer_stays_unwritten(self):
        library = MeetingLibrary(self.store, workspace_root=self.temp.name)
        intelligence, _runtime = self._intelligence(lambda _prompt, evidence: json.dumps({
            "answer": "Alice will review the report by Friday.",
            "citations": [evidence[0]["id"]], "uncertainty": "low",
        }))
        intelligence.library = library
        answer = intelligence.ask_this_meeting(
            self.session_id, "When is the review due?", DEFAULT_SUMMARY_MODEL,
            revision=self.revision,
        )
        self.assertEqual(library.list_reports(self.session_id, include_legacy=False), [])
        saved = intelligence.save_answer(
            self.session_id, answer, DEFAULT_SUMMARY_MODEL,
            question="When is the review due?", revision=self.revision,
        )
        self.assertEqual(saved["kind"], "qa")
        self.assertEqual(saved["generated"]["answer"]["citations"], ["s1"])

    def test_saved_answer_uses_ask_provenance_when_model_is_changed_or_uninstalled(self):
        library = MeetingLibrary(self.store, workspace_root=self.temp.name)
        intelligence, _runtime = self._intelligence(lambda _prompt, evidence: json.dumps({
            "answer": "Alice will review the report by Friday.",
            "citations": [evidence[0]["id"]], "uncertainty": "low",
        }))
        intelligence.library = library
        answer = intelligence.ask_this_meeting(
            self.session_id, "When is the review due?", DEFAULT_SUMMARY_MODEL,
            revision=self.revision, include_provenance=True,
        )
        provenance = answer.pop("_provenance")
        intelligence.model_path_resolver = lambda _model: None
        saved = intelligence.save_answer(
            self.session_id, answer, "model-selected-later", question="When is the review due?",
            provenance=provenance,
        )

        self.assertEqual(saved["kind"], "qa")
        self.assertEqual(saved["model"]["id"], DEFAULT_SUMMARY_MODEL)
        self.assertEqual(saved["transcript_revision"], self.revision)

    def test_untrusted_profile_and_question_are_user_evidence_not_system_prompt(self):
        marker = "DO_NOT_PUT_THIS_IN_SYSTEM_PROMPT"
        profile = validate_profile({
            "id": "focused",
            "name": "Focused",
            "instructions": marker,
            "sections": ["summary", "action_items", "decisions"],
        })
        intelligence, runtime = self._intelligence()
        intelligence.generate_report(self.session_id, DEFAULT_SUMMARY_MODEL, profile=profile)
        report_prompt, report_evidence, *_ = runtime.calls[0]
        self.assertNotIn(marker, report_prompt)
        self.assertEqual(report_evidence[-1]["kind"], "profile")
        self.assertEqual(report_evidence[-1]["instructions"], marker)

        intelligence, runtime = self._intelligence(lambda _prompt, evidence: json.dumps({
            "answer": "Alice will review the report by Friday.",
            "citations": [evidence[0]["id"]],
            "uncertainty": "low",
        }))
        question = marker + " question"
        intelligence.ask_this_meeting(self.session_id, question, DEFAULT_SUMMARY_MODEL)
        question_prompt, question_evidence, *_ = runtime.calls[0]
        self.assertNotIn(question, question_prompt)
        self.assertEqual(question_evidence[-1]["kind"], "question")
        self.assertEqual(question_evidence[-1]["question"], question)

    def test_action_owner_and_deadline_must_appear_in_each_item_citation(self):
        # The revision is already completed; use a fresh completed revision so
        # the added evidence is part of the selected immutable transcript.
        revision = self.store.begin_revision(self.session_id, "local", "pt")
        self.store.add_transcript(self.session_id, revision, {
            "id": "s1", "track": "microphone", "start": 0, "end": 5,
            "text": "Alice will review the report by Friday.",
        })
        self.store.add_transcript(self.session_id, revision, {
            "id": "s2", "track": "system", "start": 5, "end": 10,
            "text": "Bob will send the agenda on Monday.",
        })
        self.store.finish_revision(self.session_id, revision)

        def invalid(_prompt, _evidence):
            return json.dumps({
                "summary": "An agenda task exists.", "segment_ids": ["s2"],
                "decisions": [], "action_items": [{
                    "text": "Review the report", "owner": "Alice", "deadline": "Friday",
                    "segment_ids": ["s2"],
                }],
            })

        intelligence, runtime = self._intelligence(invalid)
        with self.assertRaises(ValueError):
            intelligence.generate_report(self.session_id, DEFAULT_SUMMARY_MODEL)
        self.assertTrue(runtime.closed)

    def test_action_owner_and_deadline_scope_survives_hierarchical_reduction(self):
        revision = self.store.begin_revision(self.session_id, "local", "pt")
        self.store.add_transcript(self.session_id, revision, {
            "id": "s1", "track": "microphone", "start": 0, "end": 5,
            "text": "Alice will review the report by Friday. " * 80,
        })
        self.store.add_transcript(self.session_id, revision, {
            "id": "s2", "track": "system", "start": 5, "end": 10,
            "text": "Bob will send the agenda on Monday. " * 80,
        })
        self.store.finish_revision(self.session_id, revision)

        def invalid_reduction(_prompt, evidence):
            first = evidence[0]
            if "id" in first:
                identifier = first["id"]
                owner, deadline = ("Alice", "Friday") if identifier == "s1" else ("Bob", "Monday")
                return json.dumps({
                    "summary": "A task exists.", "segment_ids": [identifier],
                    "decisions": [], "action_items": [{
                        "text": "Complete the task", "owner": owner, "deadline": deadline,
                        "segment_ids": [identifier],
                    }],
                })
            return json.dumps({
                "summary": "A task exists.", "segment_ids": ["s2"],
                "decisions": [], "action_items": [{
                    "text": "Review the report", "owner": "Alice", "deadline": "Friday",
                    "segment_ids": ["s2"],
                }],
            })

        intelligence, runtime = self._intelligence(invalid_reduction)
        with self.assertRaises(ValueError):
            intelligence.generate_report(self.session_id, DEFAULT_SUMMARY_MODEL)
        self.assertGreater(len(runtime.calls), 2)
        self.assertTrue(runtime.closed)

    def test_report_reduction_discards_uncited_transcript_text(self):
        document = {
            "summary": "One supported fact.",
            "segment_ids": ["kept"],
            "decisions": [],
            "action_items": [],
        }
        sources = {
            "kept": ["Canonical evidence."],
            "discarded": ["x" * 100_000],
        }
        self.assertEqual(
            MeetingIntelligence._cited_source_map(document, sources),
            {"kept": ["Canonical evidence."]},
        )

    def test_only_completed_transcript_revisions_are_selectable(self):
        revision = self.store.begin_revision(self.session_id, "local", "pt", status="pending")
        self.store.add_transcript(self.session_id, revision, {
            "id": "pending", "track": "microphone", "start": 0, "end": 1,
            "text": "Pending evidence.",
        })
        intelligence, runtime = self._intelligence()
        with self.assertRaisesRegex(ValueError, "concluída"):
            intelligence.generate_report(self.session_id, DEFAULT_SUMMARY_MODEL)
        self.assertFalse(runtime.calls)

    def test_extra_supported_report_sections_are_rejected(self):
        def extra(_prompt, evidence):
            result = self._report(evidence)
            result["risks"] = []
            return json.dumps(result)

        intelligence, runtime = self._intelligence(extra)
        with self.assertRaises(ValueError):
            intelligence.generate_report(self.session_id, DEFAULT_SUMMARY_MODEL)
        self.assertTrue(runtime.closed)

    def test_invalid_model_citation_or_owner_preserves_prior_report(self):
        def invalid(_prompt, _evidence):
            return json.dumps({
                "summary": "Injected certainty.", "segment_ids": ["invented"],
                "action_items": [],
            })

        intelligence, runtime = self._intelligence(invalid)
        with self.assertRaises(ValueError):
            intelligence.generate_report(self.session_id, DEFAULT_SUMMARY_MODEL)
        self.assertTrue(runtime.closed)
        self.assertEqual(self.store.get(self.session_id)["summary"], {"summary": "Prior report"})

    def test_ask_this_meeting_is_memory_only_and_returns_citations_and_uncertainty(self):
        def answer(_prompt, evidence):
            identifier = evidence[0].get("id") or evidence[0].get("segment_ids", [])[0]
            return json.dumps({
                "answer": "Alice will review the report by Friday.",
                "citations": [identifier],
                "uncertainty": "low",
            })

        intelligence, runtime = self._intelligence(answer)
        before = self.store.get(self.session_id)
        result = intelligence.ask_this_meeting(
            self.session_id, "When is the review due?", DEFAULT_SUMMARY_MODEL,
        )
        after = self.store.get(self.session_id)
        self.assertEqual(result, {
            "answer": "Alice will review the report by Friday.",
            "citations": ["s1"],
            "uncertainty": "low",
        })
        self.assertEqual(before, after)
        self.assertTrue(runtime.closed)

    def test_cancelled_question_never_writes(self):
        cancellation = threading.Event()
        cancellation.set()
        intelligence, runtime = self._intelligence()
        with self.assertRaisesRegex(RuntimeError, "cancelado"):
            intelligence.ask_this_meeting(self.session_id, "What happened?", DEFAULT_SUMMARY_MODEL,
                                          cancel_event=cancellation)
        self.assertFalse(runtime.closed)
        self.assertEqual(self.store.get(self.session_id)["summary"], {"summary": "Prior report"})

    def test_unanswerable_question_is_explicitly_uncertain(self):
        intelligence, runtime = self._intelligence(lambda _prompt, _evidence: json.dumps({
            "answer": "There is not enough evidence in this meeting.",
            "citations": [],
            "uncertainty": "high",
        }))
        result = intelligence.ask_this_meeting(
            self.session_id, "Which color was approved?", DEFAULT_SUMMARY_MODEL,
        )
        self.assertEqual(result["citations"], [])
        self.assertEqual(result["uncertainty"], "high")
        self.assertTrue(runtime.closed)

    def test_empty_answer_requires_high_uncertainty(self):
        intelligence, runtime = self._intelligence(lambda _prompt, _evidence: json.dumps({
            "answer": "", "citations": [], "uncertainty": "low",
        }))
        with self.assertRaises(ValueError):
            intelligence.ask_this_meeting(self.session_id, "What happened?", DEFAULT_SUMMARY_MODEL)
        self.assertTrue(runtime.closed)

    def test_runtime_close_failure_preserves_previous_report(self):
        class CloseFailure(_FakeRuntime):
            def close(self):
                self.closed = True
                raise RuntimeError("close failed")

        runtime = CloseFailure(lambda _prompt, evidence: json.dumps(self._report(evidence)))
        intelligence = MeetingIntelligence(
            self.store,
            runtime_factory=lambda _path, _context: runtime,
            model_path_resolver=lambda _model: "installed.gguf",
        )
        with self.assertRaisesRegex(RuntimeError, "close failed"):
            intelligence.generate_report(self.session_id, DEFAULT_SUMMARY_MODEL)
        self.assertEqual(self.store.get(self.session_id)["summary"], {"summary": "Prior report"})

    def test_long_question_uses_bounded_hierarchical_evidence(self):
        revision = self.store.begin_revision(self.session_id, "local", "pt")
        for index in range(64):
            self.store.add_transcript(self.session_id, revision, {
                "id": f"long-{index}", "track": "system", "start": index,
                "end": index + 1, "text": f"Fact {index} was discussed.",
            })
        self.store.finish_revision(self.session_id, revision)
        payload_sizes = []

        def answer(_prompt, evidence):
            payload_sizes.append(len(json.dumps(evidence, ensure_ascii=False).encode("utf-8")))
            first = evidence[0]
            identifier = first.get("id") or first.get("citations", [])[0]
            return json.dumps({
                "answer": "A fact was discussed.",
                "citations": [identifier],
                "uncertainty": "medium",
            })

        intelligence, runtime = self._intelligence(answer)
        result = intelligence.ask_this_meeting(
            self.session_id, "What was discussed?", DEFAULT_SUMMARY_MODEL,
        )
        self.assertEqual(result["citations"], ["long-0"])
        self.assertGreater(len(runtime.calls), 4)
        self.assertLessEqual(max(payload_sizes), intelligence.MAX_CONTEXT - 1536)


if __name__ == "__main__":
    unittest.main()
