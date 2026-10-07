"""Canonical provenance, atomicity, filtering, and worker failure contracts."""

import contextlib
import io
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

import embedding_models
from embedding_runtime import (
    DIMENSION, EmbeddingRuntime, document_text, normalize_vector, query_text,
)
from embedding_runtime_worker import serve
from meeting_index import IndexCancelled, IndexUnavailable
from meeting_library import MeetingLibrary, SchemaError
from meeting_semantic import SemanticIndex, SemanticLibrary, fuse_rankings, source_record
from meeting_store import MeetingStore


TMP_ROOT = Path(__file__).parent / "tmp"
FIXTURE = Path(__file__).parent / "fixtures/meeting-v1"


def vector(axis=0):
    value = [0.0] * DIMENSION
    value[axis] = 1.0
    return value


class SyntheticRuntime:
    model_revision = embedding_models.MODEL_REVISION

    def __init__(self, **kwargs):
        self.calls = []
        self.closed = False

    def embed_documents(self, records, cancel_event=None):
        self.calls.extend(records)
        return [vector() for item in records]

    def embed_query(self, query, cancel_event=None):
        return vector()

    def close(self):
        self.closed = True


class VectorTests(unittest.TestCase):
    def test_dimension_nonfinite_boolean_and_zero_vectors_fail_closed(self):
        for invalid in ([1.0], [0.0] * DIMENSION, [True] * DIMENSION,
                        [math.nan] * DIMENSION, [math.inf] * DIMENSION,
                        ["1"] * DIMENSION, [10 ** 400] * DIMENSION, None):
            with self.subTest(value=type(invalid).__name__), self.assertRaises(ValueError):
                normalize_vector(invalid)
        normalized = normalize_vector([1e308] + [0.0] * (DIMENSION - 1))
        self.assertEqual(normalized, vector())

    def test_prefixes_do_not_truncate_and_documents_preserve_title(self):
        self.assertEqual(query_text("Quando?"), "task: search result | query: Quando?")
        self.assertEqual(document_text("Trecho"), "title: none | text: Trecho")
        self.assertEqual(document_text("Trecho", "Reunião"), "title: Reunião | text: Trecho")
        with self.assertRaises(ValueError):
            document_text("a" * 8001)

    def test_source_hash_covers_content_title_offsets_and_revision(self):
        segment = {"id": "segment-1", "text": "Trecho", "start": 2, "end": 4}
        original = source_record("session", "revision", segment, "Title")
        for change in ({"text": "Edit"}, {"start": 3}, {"end": 5}):
            self.assertNotEqual(original["source_hash"], source_record(
                "session", "revision", {**segment, **change}, "Title")["source_hash"])
        self.assertNotEqual(original["source_hash"], source_record("session", "other", segment, "Title")["source_hash"])


class IndexTests(unittest.TestCase):
    def setUp(self):
        TMP_ROOT.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.index = SemanticIndex(Path(self.temp.name) / "semantic.sqlite")
        self.runtime = SyntheticRuntime()
        self.records = [source_record("session", "revision", {
            "id": "segment", "text": "Uma decisão", "start": 12.5, "end": 20})]

    def test_rank_returns_exact_source_offset_and_reuses_verified_generation(self):
        self.index.rebuild(self.records, self.runtime)
        self.index.rebuild(self.records, self.runtime)
        self.assertEqual(len(self.runtime.calls), 1)
        result = self.index.rank(vector(), self.records)[0]
        self.assertEqual(result["revision_id"], "revision")
        self.assertEqual(result["timestamp"], {"start": 12.5, "end": 20})
        self.assertTrue(result["primary"])
        self.assertTrue(self.index.is_current(self.records))

    def test_removed_filtered_and_same_id_changed_sources_never_rank(self):
        self.index.rebuild(self.records, self.runtime)
        self.assertEqual(self.index.rank(vector(), []), [])
        changed = [source_record("session", "revision", {
            "id": "segment", "text": "Texto alterado", "start": 12.5, "end": 20})]
        self.assertEqual(self.index.rank(vector(), changed), [])
        self.assertFalse(self.index.is_current(changed))
        self.index.rebuild(changed, self.runtime)
        self.assertEqual(len(self.runtime.calls), 2)

    def test_cancelled_failed_interrupted_and_changed_snapshots_preserve_previous_bytes(self):
        self.index.rebuild(self.records, self.runtime)
        original = Path(self.index.path).read_bytes()
        event = threading.Event()
        event.set()
        with self.assertRaises(IndexCancelled):
            self.index.rebuild(self.records, self.runtime, cancel_event=event)
        with self.assertRaises(IndexCancelled):
            self.index.rebuild(self.records, self.runtime, publish_check=lambda: False)
        changed = [{**self.records[0], "source_hash": "new"}]
        with mock.patch.object(self.runtime, "embed_documents", return_value=[[math.nan] * DIMENSION]):
            with self.assertRaises(ValueError):
                self.index.rebuild(changed, self.runtime)
        with mock.patch("meeting_semantic.os.replace", side_effect=OSError("synthetic interruption")):
            with self.assertRaises(OSError):
                self.index.rebuild(self.records, self.runtime)
        self.assertEqual(Path(self.index.path).read_bytes(), original)
        self.assertEqual(list(Path(self.temp.name).glob("*.tmp*")), [])

    def test_model_dimension_preprocessing_and_corrupt_vector_fail_closed(self):
        self.index.rebuild(self.records, self.runtime)
        with self.assertRaises(IndexUnavailable):
            self.index.rank(vector(), self.records, model_revision="other-model")
        with contextlib.closing(sqlite3.connect(self.index.path)) as connection:
            connection.execute("UPDATE metadata SET value='other' WHERE key='preprocessing'")
            connection.commit()
        with self.assertRaises(IndexUnavailable):
            self.index.rank(vector(), self.records)
        self.index.rebuild(self.records, self.runtime)
        with contextlib.closing(sqlite3.connect(self.index.path)) as connection:
            connection.execute("UPDATE vectors SET vector=?", (struct.pack("<768f", *([math.nan] * DIMENSION)),))
            connection.commit()
        with self.assertRaises(ValueError):
            self.index.rank(vector(), self.records)
        self.index.rebuild(self.records, self.runtime)
        self.assertTrue(self.index.is_current(self.records))
        self.assertTrue(self.index.rank(vector(), self.records))

    def test_relocated_projection_contains_no_absolute_path_dependency(self):
        self.index.rebuild(self.records, self.runtime)
        relocated = Path(self.temp.name) / "relocated"
        relocated.mkdir()
        target = relocated / "semantic.sqlite"
        shutil.copyfile(self.index.path, target)
        self.assertEqual(SemanticIndex(target).rank(vector(), self.records)[0]["start"], 12.5)

    def test_linked_path_is_rejected(self):
        with mock.patch("meeting_semantic._has_link_component", return_value=True):
            with self.assertRaises(IndexUnavailable):
                self.index.rebuild(self.records, self.runtime)

    def test_missing_derived_rows_are_stale_and_regenerated(self):
        self.index.rebuild(self.records, self.runtime)
        with contextlib.closing(sqlite3.connect(self.index.path)) as connection:
            connection.execute("DELETE FROM vectors")
            connection.commit()
        self.assertFalse(self.index.is_current(self.records))
        self.index.rebuild(self.records, self.runtime)
        self.assertTrue(self.index.is_current(self.records))

    def test_fusion_deduplicates_sources_without_collapsing_report_evidence(self):
        first = {**self.records[0], "snippet": "Uma decisão", "primary": True}
        report = {"source_kind": "report", "session_id": "session", "report_id": "report",
                  "revision_id": "revision", "segment_id": None, "primary": False}
        duplicate = {**first, "segment_id": "overlap"}
        result = fuse_rankings([first, report], [first, duplicate])
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["segment_id"], "segment")
        self.assertFalse(result[1]["primary"])


class LibraryTests(unittest.TestCase):
    def setUp(self):
        TMP_ROOT.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        shutil.copytree(FIXTURE, self.home / "meetings/fixture-meeting-v1")
        self.library = MeetingLibrary(MeetingStore(self.home / "meetings"), workspace_root=self.home)
        self.runtime = SyntheticRuntime()
        self.service = SemanticLibrary(self.library, runtime_factory=lambda **kwargs: self.runtime)
        self.library._semantic = self.service
        self.addCleanup(self.library.shutdown)

    def enable(self):
        self.library.update_workspace({"semantic_search": True})
        self.service.rebuild()

    def test_disabled_by_default_and_does_not_load_runtime(self):
        with mock.patch.object(self.service, "runtime_factory") as factory:
            self.library.search("marco")
            factory.assert_not_called()
        self.assertFalse(self.service.enabled())
        self.assertFalse(Path(self.service.index.path).exists())

    def test_hybrid_paraphrase_opens_original_revision_and_timestamp(self):
        self.enable()
        hits = self.library.search("O que foi decidido?")
        self.assertTrue(hits)
        self.assertEqual(hits[0]["title"], "Weekly product review")
        resolved = self.library.resolve_search_result(hits[0])
        self.assertEqual(resolved["revision_id"], "revision-1")
        self.assertEqual(resolved["start"], 0.0)
        self.assertEqual(self.service.status, "ready")

    def test_metadata_filters_apply_before_semantic_ranking(self):
        self.enable()
        for filters in ({"tag": "excluded"}, {"person": "excluded"}, {"collection": "excluded"},
                        {"series": "excluded"}, {"date_from": "9999"}, {"status": "recording"}):
            with self.subTest(filters=filters):
                self.assertEqual(self.library.search("Pergunta", **filters), [])

    def test_missing_model_runtime_and_offline_failure_keep_keyword_results_visible(self):
        self.enable()
        with mock.patch.object(self.runtime, "embed_query", side_effect=RuntimeError("synthetic offline failure")):
            hits = self.library.search("marco")
        self.assertTrue(hits)
        self.assertIn("palavras", self.service.status)
        self.assertTrue(self.runtime.closed)

    def test_removed_and_inaccessible_meeting_cannot_escape_projection(self):
        self.enable()
        shutil.move(self.home / "meetings/fixture-meeting-v1", self.home / "removed")
        with mock.patch.object(self.service, "queue"):
            self.assertEqual(self.library.search("Pergunta"), [])
        shutil.move(self.home / "removed", self.home / "meetings/fixture-meeting-v1")
        with mock.patch.object(self.library.store, "get", side_effect=PermissionError("synthetic")):
            self.assertEqual(self.library.search("Pergunta"), [])
        self.assertIn("palavras", self.service.status)

    def test_incomplete_revision_is_not_indexed_and_workspace_option_is_validated(self):
        original = self.library.store.get
        def read(*args, **kwargs):
            metadata = original(*args, **kwargs)
            metadata["revisions"][0]["status"] = "pending"
            return metadata
        with mock.patch.object(self.library.store, "get", side_effect=read):
            self.assertEqual(self.service.records(), [])
        with self.assertRaises(SchemaError):
            self.library.update_workspace({"semantic_search": "yes"})

    def test_partially_readable_completed_transcript_never_publishes(self):
        self.enable()
        original = Path(self.service.index.path).read_bytes()
        with mock.patch.object(self.library.store, "get_transcript", return_value=iter(())):
            with self.assertRaises(ValueError):
                self.service.rebuild()
        self.assertEqual(Path(self.service.index.path).read_bytes(), original)

    def test_linked_transcript_is_rejected_before_open(self):
        with mock.patch("meeting_semantic._has_link_component", return_value=True):
            with self.assertRaises(IndexUnavailable):
                self.service.records()

    def test_coalesced_background_queue_and_shutdown_are_bounded(self):
        started, release = threading.Event(), threading.Event()
        calls = []
        def rebuild():
            calls.append(1)
            started.set()
            release.wait(2)
        with mock.patch.object(self.service, "rebuild", side_effect=rebuild):
            worker = self.service.queue()
            self.assertTrue(started.wait(2))
            for _ in range(10):
                self.assertIs(self.service.queue(), worker)
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(calls), 2)

    def test_background_cancellation_reaches_inference_and_closes_runtime(self):
        self.library.update_workspace({"semantic_search": True})
        started = threading.Event()
        def embed(records, event):
            started.set()
            for _ in range(300):
                if event.is_set():
                    raise IndexCancelled("synthetic cancellation")
                threading.Event().wait(.01)
            raise AssertionError("Cancellation did not reach inference")
        with mock.patch.object(self.runtime, "embed_documents", side_effect=embed):
            worker = self.service.queue()
            self.assertTrue(started.wait(2))
            self.service.cancel_index()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertTrue(self.runtime.closed)
        self.assertFalse(Path(self.service.index.path).exists())

    def test_disabling_preserves_projection_and_releases_warm_runtime(self):
        self.enable()
        original = Path(self.service.index.path).read_bytes()
        self.library.update_workspace({"semantic_search": False})
        self.service.rebuild()
        self.assertTrue(self.runtime.closed)
        self.assertEqual(Path(self.service.index.path).read_bytes(), original)

    def test_cancel_stops_active_query_and_keeps_keyword_results(self):
        self.enable()
        started, release = threading.Event(), threading.Event()
        result = []
        def embed(query, event):
            started.set()
            while not release.wait(.01):
                if event.is_set():
                    raise IndexCancelled("synthetic cancellation")
            raise AssertionError("Cancellation did not reach the query")
        with mock.patch.object(self.runtime, "embed_query", side_effect=embed):
            worker = threading.Thread(target=lambda: result.extend(self.library.search("marco")))
            worker.start()
            try:
                self.assertTrue(started.wait(2))
                self.service.cancel_index()
                worker.join(3)
                self.assertFalse(worker.is_alive())
            finally:
                release.set()
                worker.join(3)
        self.assertTrue(result)
        self.assertTrue(self.runtime.closed)

    def test_keyword_search_remains_available_while_native_indexing_is_busy(self):
        self.library.update_workspace({"semantic_search": True})
        started, release = threading.Event(), threading.Event()
        def embed(records, event):
            started.set()
            if not release.wait(3):
                raise AssertionError("Keyword search waited for native indexing")
            return [vector() for item in records]
        with mock.patch.object(self.runtime, "embed_documents", side_effect=embed), \
             mock.patch.object(self.runtime, "embed_query") as query:
            worker = self.service.queue()
            try:
                self.assertTrue(started.wait(2))
                self.assertTrue(self.library.search("marco"))
                query.assert_not_called()
                self.assertTrue(worker.is_alive())
            finally:
                release.set()
                worker.join(3)
        self.assertFalse(worker.is_alive())


class WorkerTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows venv redirector")
    def test_windows_venv_worker_targets_interpreter_and_preserves_environment(self):
        import embedding_runtime
        with mock.patch.object(sys, "prefix", "synthetic-venv"), \
             mock.patch.object(sys, "base_prefix", "synthetic-base"), \
             mock.patch.object(sys, "_base_executable", "synthetic-interpreter.exe"), \
             mock.patch("embedding_runtime.subprocess.Popen") as spawn:
            embedding_runtime._spawn_worker()
        self.assertEqual(spawn.call_args.args[0][0], "synthetic-interpreter.exe")
        self.assertEqual(spawn.call_args.kwargs["env"]["__PYVENV_LAUNCHER__"], sys.executable)
        self.assertEqual(spawn.call_args.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)

    def test_worker_rejects_malformed_json_without_echoing_input(self):
        output = io.StringIO()
        serve(io.StringIO("private-invalid-input\n"), output)
        self.assertFalse(json.loads(output.getvalue())["ok"])
        self.assertNotIn("private-invalid-input", output.getvalue())

    def test_native_worker_requests_mean_pooling_and_no_truncation(self):
        instances = []
        class HiddenChannelsLlama:
            def __init__(self, **kwargs):
                self.options = kwargs
                self._model = mock.Mock(model="synthetic-native-handle")
                self.closed = False
                instances.append(self)

            def n_embd(self):
                return 512

            def embed(self, texts, **kwargs):
                self.embed_options = kwargs
                return [vector()[:self.n_embd()] for text in texts]

            def close(self):
                self.closed = True

        module = mock.Mock(Llama=HiddenChannelsLlama, LLAMA_POOLING_TYPE_MEAN=1,
                           llama_model_n_embd_out=mock.Mock(return_value=DIMENSION))
        output = io.StringIO()
        requests = [{"type": "open", "model_path": "synthetic.gguf"},
                    {"type": "embed", "texts": [query_text("Pergunta")]}, {"type": "close"}]
        with mock.patch.dict(sys.modules, {"llama_cpp": module}):
            serve(io.StringIO("".join(json.dumps(item) + "\n" for item in requests)), output)
        model = instances[0]
        self.assertEqual(model.embed_options, {"normalize": False, "truncate": False})
        self.assertEqual(model.options["pooling_type"], 1)
        self.assertEqual(model.options["n_gpu_layers"], 0)
        self.assertTrue(model.closed)
        self.assertEqual(len(json.loads(output.getvalue().splitlines()[1])["vectors"][0]), DIMENSION)
        module.llama_model_n_embd_out.assert_called_with("synthetic-native-handle")

    def test_real_worker_exit_is_reported_and_process_is_reaped(self):
        import embedding_runtime
        processes = []
        original = embedding_runtime._spawn_worker
        def spawn():
            process = original()
            processes.append(process)
            return process
        with mock.patch("embedding_runtime.embedding_model_path", return_value="nonexistent-synthetic.gguf"), \
             mock.patch("embedding_runtime._spawn_worker", side_effect=spawn):
            with self.assertRaises(RuntimeError):
                EmbeddingRuntime()
        self.assertIsNotNone(processes[0].poll())
        self.assertTrue(processes[0].stdin.closed)

    def test_cancel_terminates_stalled_native_process_and_preserves_no_temp_files(self):
        TMP_ROOT.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            script = Path(directory) / "worker.py"
            script.write_text("import sys,time\nfor line in sys.stdin:\n time.sleep(30)\n", encoding="utf-8")
            event = threading.Event()
            process = None
            timer = threading.Timer(.25, event.set)
            def spawn():
                nonlocal process
                options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
                process = subprocess.Popen([sys.executable, "-u", str(script)], stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8", **options)
                return process
            timer.start()
            try:
                with mock.patch("embedding_runtime.embedding_model_path", return_value="synthetic.gguf"), \
                     mock.patch("embedding_runtime._spawn_worker", side_effect=spawn):
                    with self.assertRaises(IndexCancelled):
                        EmbeddingRuntime(cancel_event=event)
            finally:
                timer.cancel()
                timer.join()
                if process and process.poll() is None:
                    process.kill()
                    process.wait(3)
            self.assertIsNotNone(process.poll())
            self.assertTrue(process.stdout.closed)

    def test_cache_uses_relocatable_model_root_and_download_is_explicit(self):
        with mock.patch("app_paths.configured_models_dir", return_value=os.path.abspath("synthetic-cache")):
            self.assertEqual(embedding_models.embedding_cache_dir(), os.path.abspath("synthetic-cache/embedding-models"))
        with mock.patch("embedding_models.installed_model_path", return_value=None), \
             mock.patch("embedding_models.download_model") as download:
            self.assertIsNone(embedding_models.embedding_model_path())
            download.assert_not_called()

    def test_linked_resume_file_is_rejected_before_download_or_mutation(self):
        with mock.patch("embedding_models._has_link_component",
                        side_effect=lambda path: path.endswith(".partial")), \
             mock.patch("embedding_models.download_model") as download:
            with self.assertRaises(ValueError):
                embedding_models.download_embedding_model(cache_dir=os.path.abspath("synthetic-cache"))
        download.assert_not_called()


if __name__ == "__main__":
    unittest.main()
