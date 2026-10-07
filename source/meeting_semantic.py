"""Disposable transcript vectors and canonical-filtered hybrid retrieval."""

import contextlib
import hashlib
import json
import math
import os
import sqlite3
import struct
import threading
import uuid

from embedding_models import MODEL_REVISION
from embedding_runtime import (
    DIMENSION, MAX_BATCH, PREPROCESSING_VERSION, EmbeddingRuntime,
    check_cancelled, normalize_vector,
)
from i18n import tr
from meeting_index import IndexCancelled, IndexUnavailable, _bounded_text, _has_link_component, _writer_lock
from meeting_store import METADATA_NAME


# ceiling: exact cosine scan of at most 20,000 segments. Move to a measured
# ANN design when a representative library exceeds this bounded workload.
MAX_SEGMENTS = 20_000
RRF_K = 60  # Conventional default, not tuned on the held-out queries.
SCHEMA_VERSION = 1


class _Cancellation:
    def __init__(self, *events):
        self.events = events

    def is_set(self):
        return any(event is not None and event.is_set() for event in self.events)


def source_key(item):
    return (item.get("source_kind"), item.get("session_id"), item.get("revision_id"),
            item.get("segment_id"), item.get("report_id"))


def source_record(session_id, revision_id, segment, title=""):
    text = segment.get("text", "")
    if not isinstance(text, str):
        raise ValueError(tr("O texto do trecho é inválido."))
    if not isinstance(segment.get("id"), str) or not segment["id"] or len(segment["id"]) > 1024:
        raise ValueError(tr("A fonte de transcrição é incompleta."))
    start, end = segment.get("start"), segment.get("end")
    if any(isinstance(value, bool) or not isinstance(value, (int, float))
           or not math.isfinite(value) or value < 0 for value in (start, end)) or end < start:
        raise ValueError(tr("A fonte de transcrição é incompleta."))
    record = {"source_kind": "transcript", "session_id": session_id,
              "revision_id": revision_id, "segment_id": segment["id"], "report_id": None,
              "text": text, "title": str(title)[:512],
              "start": segment.get("start"), "end": segment.get("end")}
    record["source_hash"] = hashlib.sha256(json.dumps(
        record, sort_keys=True, ensure_ascii=False, allow_nan=False,
        separators=(",", ":")).encode("utf-8")).hexdigest()
    return record


def records_fingerprint(records):
    return hashlib.sha256("".join(sorted(item["source_hash"] for item in records)).encode("ascii")).hexdigest()


def fuse_rankings(keyword, semantic, *, limit=50, offset=0):
    scores, items = {}, {}
    for ranking in (keyword, semantic):
        seen = set()
        for rank, item in enumerate(ranking, 1):
            key = source_key(item)
            if key in seen:
                continue
            seen.add(key)
            scores[key] = scores.get(key, 0) + 1 / (RRF_K + rank)
            items.setdefault(key, dict(item))
    ordered = sorted(items, key=lambda key: (-scores[key], tuple(str(value or "") for value in key)))
    result = []
    for key in ordered:
        item = items[key]
        # Deduplicate near-identical passages in the same revision/time range;
        # different evidence kinds and revisions retain their provenance.
        if item.get("source_kind") == "transcript" and any(
            other.get("source_kind") == "transcript"
            and other.get("session_id") == item.get("session_id")
            and other.get("revision_id") == item.get("revision_id")
            and isinstance(item.get("start"), (int, float))
            and isinstance(other.get("start"), (int, float))
            and min(item.get("end") or item["start"], other.get("end") or other["start"])
                > max(item["start"], other["start"])
            and item.get("snippet") == other.get("snippet") for other in result
        ):
            continue
        item["hybrid_score"] = scores[key]
        result.append(item)
    return result[offset:offset + limit]


class SemanticIndex:
    """Whole-file atomic publication; readers open one immutable generation."""

    def __init__(self, path):
        self.path = os.path.abspath(os.fspath(path))

    def _safe_path(self, path=None):
        target = path or self.path
        if _has_link_component(target):
            raise IndexUnavailable(tr("O índice semântico não pode conter links ou junctions."))
        return target

    @contextlib.contextmanager
    def _read(self):
        path = self._safe_path()
        if not os.path.isfile(path):
            raise IndexUnavailable(tr("O índice semântico ainda não foi criado."))
        # Read-only avoids creating an empty database after a relocation race.
        from pathlib import Path
        connection = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True)
        try:
            yield connection
        finally:
            connection.close()

    def _metadata(self, connection):
        values = dict(connection.execute("SELECT key,value FROM metadata"))
        if values.get("schema") != str(SCHEMA_VERSION):
            raise IndexUnavailable(tr("O índice semântico precisa ser reconstruído."))
        return values

    def _compatible(self, values, model_revision):
        return (values.get("model") == model_revision and values.get("dimension") == str(DIMENSION)
                and values.get("preprocessing") == PREPROCESSING_VERSION)

    def rebuild(self, records, runtime, *, cancel_event=None, publish_check=None):
        if len(records) > MAX_SEGMENTS:
            raise ValueError(tr("A biblioteca excede o limite de 20000 trechos da busca semântica."))
        check_cancelled(cancel_event)
        self._safe_path()
        with _writer_lock(self.path):
            reusable = {}
            if os.path.exists(self.path):
                try:
                    with self._read() as connection:
                        metadata = self._metadata(connection)
                        if self._compatible(metadata, runtime.model_revision):
                            rows = connection.execute("SELECT source_hash,vector FROM vectors LIMIT ?", (MAX_SEGMENTS + 1,))
                            for source_hash, vector in rows:
                                if len(reusable) >= MAX_SEGMENTS:
                                    raise IndexUnavailable(tr("O índice semântico excede o limite permitido."))
                                try:
                                    normalize_vector(list(struct.unpack("<768f", vector)))
                                except (ValueError, struct.error):
                                    continue  # Explicit rebuild regenerates corrupt derived rows.
                                reusable[source_hash] = vector
                except (sqlite3.DatabaseError, IndexUnavailable):
                    reusable = {}  # An explicit rebuild repairs only derived data.
            temporary = self._safe_path(self.path + "." + uuid.uuid4().hex + ".tmp")
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            connection = None
            try:
                connection = sqlite3.connect(temporary)
                connection.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
                connection.execute("CREATE TABLE vectors(session TEXT,revision TEXT,segment TEXT,source_hash TEXT,"
                                   "vector BLOB NOT NULL,PRIMARY KEY(session,revision,segment))")
                for start in range(0, len(records), MAX_BATCH):
                    check_cancelled(cancel_event)
                    batch = records[start:start + MAX_BATCH]
                    missing = [item for item in batch if item["source_hash"] not in reusable]
                    generated = runtime.embed_documents(missing, cancel_event) if missing else []
                    if len(generated) != len(missing):
                        raise ValueError(tr("O runtime retornou um lote de embeddings incompleto."))
                    vectors = {item["source_hash"]: struct.pack("<768f", *normalize_vector(vector))
                               for item, vector in zip(missing, generated)}
                    for item in batch:
                        blob = vectors.get(item["source_hash"], reusable.get(item["source_hash"]))
                        normalize_vector(list(struct.unpack("<768f", blob)))
                        connection.execute("INSERT INTO vectors VALUES (?,?,?,?,?)", (
                            item["session_id"], item["revision_id"], item["segment_id"], item["source_hash"], blob))
                connection.executemany("INSERT INTO metadata VALUES (?,?)", {
                    "schema": str(SCHEMA_VERSION), "model": runtime.model_revision,
                    "dimension": str(DIMENSION), "preprocessing": PREPROCESSING_VERSION,
                    "fingerprint": records_fingerprint(records),
                }.items())
                connection.commit()
                connection.close()
                connection = None
                check_cancelled(cancel_event)
                if publish_check is not None and not publish_check():
                    raise IndexCancelled(tr("Os trechos mudaram durante a indexação; reconstrua a busca semântica."))
                self._safe_path()
                with open(temporary, "r+b") as handle:
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
                return len(records)
            finally:
                if connection is not None:
                    connection.close()
                for suffix in ("", "-journal", "-wal", "-shm"):
                    with contextlib.suppress(FileNotFoundError):
                        os.remove(temporary + suffix)

    def rank(self, query_vector, eligible, *, model_revision=MODEL_REVISION, cancel_event=None):
        query_vector = normalize_vector(query_vector)
        current = {source_key(item): item for item in eligible}
        hits = []
        with self._read() as connection:
            if not self._compatible(self._metadata(connection), model_revision):
                raise IndexUnavailable(tr("O modelo do índice semântico mudou; reconstrua o índice."))
            for count, row in enumerate(connection.execute("SELECT session,revision,segment,source_hash,vector FROM vectors")):
                check_cancelled(cancel_event)
                if count >= MAX_SEGMENTS:
                    raise IndexUnavailable(tr("O índice semântico excede o limite permitido."))
                session, revision, segment, source_hash, blob = row
                record = current.get(("transcript", session, revision, segment, None))
                if record is None or record["source_hash"] != source_hash:
                    continue
                vector = normalize_vector(list(struct.unpack("<768f", blob)))
                hit = {key: record[key] for key in ("source_kind", "session_id", "revision_id",
                                                   "segment_id", "report_id", "start", "end")}
                hit.update(title=record["title"], snippet=_bounded_text(record["text"]), evidence_weight=1.0, primary=True,
                           semantic_score=sum(a * b for a, b in zip(query_vector, vector)),
                           timestamp={"start": record["start"], "end": record["end"]})
                hits.append(hit)
        return sorted(hits, key=lambda item: (-item["semantic_score"], source_key(item)))[:500]

    def is_current(self, records, model_revision=MODEL_REVISION):
        with self._read() as connection:
            metadata = self._metadata(connection)
            count = connection.execute("SELECT count(*) FROM vectors").fetchone()[0]
            return (count == len(records) and self._compatible(metadata, model_revision)
                    and metadata.get("fingerprint") == records_fingerprint(records))


class SemanticLibrary:
    """Opt-in service. All entrypoints that touch data run on IO workers."""

    def __init__(self, library, *, runtime_factory=EmbeddingRuntime):
        self.library = library
        self.index = SemanticIndex(os.path.join(library.home_root, "semantic.sqlite"))
        self.runtime_factory = runtime_factory
        self._operation = threading.Lock()
        self._guard = threading.RLock()
        self._cancel = threading.Event()
        self._job_cancel = None
        self._query_cancel = None
        self._worker = None
        self._pending = False
        self._queued_cancel_event = None
        self._closed = False
        self._runtime = None
        self.status = "disabled"

    def enabled(self):
        return self.library.read_workspace().get("semantic_search", False) is True

    def _get_runtime(self, event):
        if self._runtime is None:
            self._runtime = self.runtime_factory(cancel_event=event)
        return self._runtime

    def _close_runtime(self):
        if self._runtime is not None:
            self._runtime.close()
            self._runtime = None

    def records(self, filters=None, cancel_event=None):
        catalog = self.library._catalog_filters(**(filters or {}))
        result = []
        for session_id in self.library._canonical_session_ids():
            check_cancelled(_Cancellation(self._cancel, cancel_event))
            self.library._canonical_path(session_id, METADATA_NAME)
            metadata = self.library.store.get(session_id, include_events=False)
            annotations = self.library.read_annotations(session_id)
            if not self.library._catalog_match(metadata, annotations, catalog):
                continue
            if metadata.get("status") not in {"completed", "transcribed"}:
                continue
            for revision in metadata.get("revisions", []):
                if not isinstance(revision, dict) or not isinstance(revision.get("id"), str):
                    raise ValueError(tr("As revisões da reunião são inválidas; o índice não foi publicado."))
                if revision.get("status") != "completed":
                    continue
                expected = revision.get("segments")
                if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
                    raise ValueError(tr("A transcrição está incompleta; o índice não foi marcado como íntegro."))
                path = self.library.store._revision_path(session_id, revision["id"])
                if _has_link_component(path):
                    raise IndexUnavailable(tr("O arquivo canônico não pode ser um link ou junction."))
                # MeetingStore's recovery reader tolerates partial files. A
                # completed semantic generation must be completely readable.
                with open(path, "rb") as handle:
                    handle.read(1)
                count = 0
                for segment in self.library.store.get_transcript(session_id, revision["id"]):
                    check_cancelled(_Cancellation(self._cancel, cancel_event))
                    count += 1
                    if not str(segment.get("text", "")).strip():
                        continue
                    if len(result) >= MAX_SEGMENTS:
                        raise ValueError(tr("A biblioteca excede o limite de 20000 trechos da busca semântica."))
                    result.append(source_record(session_id, revision["id"], segment,
                                                annotations.get("title", metadata.get("title", ""))))
                if count != expected:
                    raise ValueError(tr("A transcrição está incompleta; o índice não foi marcado como íntegro."))
        return result

    def rebuild(self, cancel_event=None):
        with self._operation:
            with self._guard:
                self._job_cancel = threading.Event()
                event = _Cancellation(self._cancel, cancel_event, self._job_cancel)
            check_cancelled(event)
            if not self.enabled():
                self._close_runtime()
                self.status = "disabled"
                return {"segments": 0, "state": self.status}
            self.status = "rebuilding"
            try:
                records = self.records(cancel_event=event)
                runtime = self._get_runtime(event)
                fingerprint = records_fingerprint(records)
                count = self.index.rebuild(records, runtime, cancel_event=event,
                                           publish_check=lambda: (not self._closed and self.enabled()
                                               and records_fingerprint(self.records(cancel_event=event)) == fingerprint))
                self.status = "ready"
                return {"segments": count, "state": self.status}
            except Exception:
                self.status = "unavailable"
                self._close_runtime()
                raise

    def _filter_keyword(self, keyword, filters):
        catalog = self.library._catalog_filters(**filters)
        fallback = []
        for item in keyword:
            try:
                self.library._canonical_path(item["session_id"], METADATA_NAME)
                metadata = self.library.store.get(item["session_id"], include_events=False)
                annotations = self.library.read_annotations(item["session_id"])
                if self.library._catalog_match(metadata, annotations, catalog):
                    resolved = self.library.resolve_search_result(item)
                    fallback.append({**item, **{key: resolved[key] for key in (
                        "start", "end", "timestamp") if key in resolved}})
            except (OSError, ValueError, RuntimeError):
                continue
        return fallback

    def search(self, query, keyword, *, limit=50, offset=0, **filters):
        check_cancelled(self._cancel)
        fallback = self._filter_keyword(keyword, filters)
        if not self._operation.acquire(blocking=False):
            # Indexing must never hold keyword search behind native inference.
            return fallback[offset:offset + limit]
        with self._guard:
            self._query_cancel = threading.Event()
            event = _Cancellation(self._cancel, self._query_cancel)
        try:
            if not self.enabled():
                self.status = "disabled"
                return fallback[offset:offset + limit]
            all_records = self.records(cancel_event=event)
            if not self.index.is_current(all_records):
                self.status = tr("Índice semântico desatualizado; reconstruindo. Busca por palavras ativa.")
                self.queue()
                return fallback[offset:offset + limit]
            eligible = self.records(filters, cancel_event=event)
            if not eligible:
                self.status = "ready"
                return fallback[offset:offset + limit]
            runtime = self._get_runtime(event)
            semantic = self.index.rank(runtime.embed_query(query, event), eligible,
                                       model_revision=runtime.model_revision, cancel_event=event)
            # Revalidate live canonical eligibility before fusion. Never
            # allow a removed or edited disposable hit to become evidence.
            live = {source_key(item): item["source_hash"] for item in self.records(filters, cancel_event=event)}
            hashes = {source_key(item): item["source_hash"] for item in eligible}
            semantic = [item for item in semantic if live.get(source_key(item)) == hashes[source_key(item)]]
            self.status = "ready"
            return fuse_rankings(fallback, semantic, limit=limit, offset=offset)
        except IndexUnavailable:
            self.status = tr("Índice semântico ausente ou incompatível; reconstruindo. Busca por palavras ativa.")
            self.queue()
            return fallback[offset:offset + limit]
        except (OSError, ValueError, RuntimeError, sqlite3.DatabaseError, struct.error):
            self._close_runtime()
            self.status = tr("Busca semântica indisponível. Instale um modelo e runtime compatíveis e repare o índice. Busca por palavras ativa.")
            return fallback[offset:offset + limit]
        finally:
            with self._guard:
                self._query_cancel = None
            self._operation.release()

    def cancel_index(self):
        with self._guard:
            self._pending = False
            if self._job_cancel is not None:
                self._job_cancel.set()
            if self._query_cancel is not None:
                self._query_cancel.set()

    def queue(self, cancel_event=None):
        with self._guard:
            if self._closed:
                return None
            self._pending = True
            self._queued_cancel_event = cancel_event
            if self._worker is not None and self._worker.is_alive():
                return self._worker
            def run():
                while True:
                    with self._guard:
                        if not self._pending or self._closed:
                            self._worker = None
                            return
                        self._pending = False
                        event = self._queued_cancel_event
                    try:
                        self.rebuild(event) if event is not None else self.rebuild()
                    except (OSError, ValueError, RuntimeError, sqlite3.DatabaseError, struct.error):
                        self.status = tr("Busca semântica indisponível. Busca por palavras ativa.")
            self._worker = threading.Thread(target=run, daemon=True, name="MeetingSemanticProjection")
            self._worker.start()
            return self._worker

    def shutdown(self, timeout=12):
        with self._guard:
            self._closed = True
            self._cancel.set()
            worker = self._worker
        if worker and worker is not threading.current_thread():
            worker.join(timeout)
            if worker.is_alive():
                raise RuntimeError(tr("A indexação semântica ainda está encerrando."))
        if not self._operation.acquire(timeout=timeout):
            raise RuntimeError(tr("A busca semântica ainda está encerrando."))
        try:
            self._close_runtime()
        finally:
            self._operation.release()
