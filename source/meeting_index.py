"""Disposable SQLite/FTS projection for the canonical meeting library.

The index never receives audio bytes.  Each operation owns one connection and
transactions are kept off the capture and Tk threads by the caller.  Full
rebuilds are assembled in a closed temporary database and published with one
``os.replace`` only after all handles and WAL files are closed.
"""

import contextlib
import copy
import hashlib
import json
import os
import sqlite3
import threading
import time
import unicodedata
import uuid


INDEX_SCHEMA_VERSION = 1
STATE_READY = "ready"
STATE_STALE = "stale"
STATE_REBUILDING = "rebuilding"
STATE_UNAVAILABLE = "unavailable"
STATE_INCOMPLETE = "incomplete"
INDEX_STATES = frozenset({STATE_READY, STATE_STALE, STATE_REBUILDING, STATE_UNAVAILABLE, STATE_INCOMPLETE})
DEFAULT_BATCH_SIZE = 128
DEFAULT_BUSY_TIMEOUT_MS = 5000
MAX_QUERY_CHARS = 512
LOCK_TIMEOUT_SECONDS = 5.0
LOCK_POLL_SECONDS = 0.05


_PATH_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS = {}


def _has_link_component(path):
    """Return whether an existing path component is a link or junction."""
    absolute = os.path.abspath(os.fspath(path))
    drive, tail = os.path.splitdrive(absolute)
    current = drive + os.sep if drive else os.sep
    for part in tail.strip("\\/").split(os.sep):
        if not part:
            continue
        current = os.path.join(current, part)
        if os.path.lexists(current) and (
            os.path.islink(current)
            or getattr(os.path, "isjunction", lambda _path: False)(current)
        ):
            return True
    return False


def _path_lock(path):
    key = os.path.normcase(os.path.abspath(os.fspath(path)))
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[key] = lock
        return lock


@contextlib.contextmanager
def _cross_process_lock(path):
    """Serialize index writers across processes without a dependency."""
    lock_path = f"{os.fspath(path)}.lock"
    if _has_link_component(os.path.dirname(lock_path)) or (
        os.path.lexists(lock_path)
        and (os.path.islink(lock_path)
             or getattr(os.path, "isjunction", lambda _path: False)(lock_path))
    ):
        raise IndexUnavailable("O bloqueio do índice aponta para um link ou junction.")
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    handle = open(lock_path, "a+b")
    locked = False
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
            while True:
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as error:
                    if time.monotonic() >= deadline:
                        raise IndexUnavailable(
                            "O bloqueio do índice está ocupado há muito tempo; "
                            "feche a outra instância ou remova o bloqueio após verificar o processo."
                        ) from error
                    threading.Event().wait(LOCK_POLL_SECONDS)
        else:
            import fcntl

            deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except (BlockingIOError, OSError) as error:
                    if time.monotonic() >= deadline:
                        raise IndexUnavailable(
                            "O bloqueio do índice está ocupado há muito tempo; "
                            "feche a outra instância ou remova o bloqueio após verificar o processo."
                        ) from error
                    threading.Event().wait(LOCK_POLL_SECONDS)
        locked = True
        yield
    finally:
        try:
            if locked and os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            elif locked:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


@contextlib.contextmanager
def _writer_lock(path):
    with _path_lock(path):
        with _cross_process_lock(path):
            yield


class MeetingIndexError(RuntimeError):
    """Base error for disposable-index failures."""


class IndexUnavailable(MeetingIndexError):
    """SQLite or FTS5 is not usable on this runtime/path."""


class IndexCancelled(MeetingIndexError):
    """A rebuild was cancelled before publication."""


def sqlite_capabilities():
    """Probe SQLite and FTS5 using only a private in-memory connection."""
    connection = sqlite3.connect(":memory:")
    try:
        try:
            connection.execute("CREATE VIRTUAL TABLE _fts_probe USING fts5(content)")
            connection.execute("INSERT INTO _fts_probe(content) VALUES (?)", ("revisão",))
            matched = connection.execute(
                "SELECT count(*) FROM _fts_probe WHERE _fts_probe MATCH ?", ("revisão",)
            ).fetchone()[0]
            if matched != 1:
                raise sqlite3.DatabaseError("FTS5 Unicode MATCH returned no row")
        except sqlite3.DatabaseError as error:
            return {"sqlite": sqlite3.sqlite_version, "fts5": False, "error": str(error)}
        return {"sqlite": sqlite3.sqlite_version, "fts5": True}
    finally:
        connection.close()


def fts5_available():
    return bool(sqlite_capabilities().get("fts5"))


def _json_piece(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _metadata_without_events(metadata):
    value = copy.deepcopy(metadata)
    value.pop("events", None)
    return value


def _sorted_transcripts(transcripts):
    if transcripts is None:
        return []
    if isinstance(transcripts, dict):
        result = []
        for revision_id in sorted(transcripts, key=str):
            segments = transcripts[revision_id]
            result.append((str(revision_id), sorted(
                (copy.deepcopy(item) for item in segments if isinstance(item, dict)),
                key=lambda item: str(item.get("id", "")),
            )))
        return result
    result = [("", sorted(
        (copy.deepcopy(item) for item in transcripts if isinstance(item, dict)),
        key=lambda item: str(item.get("id", "")),
    ))]
    return result


def fingerprint_canonical(metadata, annotations=None, transcripts=None, reports=None):
    """Hash canonical generations and content, including same-ID mutations."""
    digest = hashlib.sha256()

    def add(marker, value):
        encoded = _json_piece(value).encode("utf-8")
        digest.update(marker.encode("ascii"))
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)

    add("metadata\0", _metadata_without_events(metadata))
    add("annotations\0", annotations if annotations is not None else None)
    for revision_id, segments in _sorted_transcripts(transcripts):
        add("revision\0", revision_id)
        for segment in segments:
            add("segment\0", segment)
    for report in sorted((copy.deepcopy(item) for item in (reports or []) if isinstance(item, dict)),
                         key=lambda item: str(item.get("id", ""))):
        add("report\0", report)
    return digest.hexdigest()


class MeetingIndex:
    """Thread-confined SQLite projection with explicit lifecycle state."""

    def __init__(self, path, *, busy_timeout_ms=DEFAULT_BUSY_TIMEOUT_MS, batch_size=DEFAULT_BATCH_SIZE):
        if not isinstance(path, (str, os.PathLike)):
            raise ValueError("O caminho do índice é inválido.")
        if isinstance(busy_timeout_ms, bool) or not isinstance(busy_timeout_ms, int) or busy_timeout_ms < 0:
            raise ValueError("O tempo de espera do índice é inválido.")
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= 10_000:
            raise ValueError("O lote do índice é inválido.")
        candidate = os.path.abspath(os.fspath(path))
        if os.path.isdir(candidate):
            candidate = os.path.join(candidate, "library.sqlite")
        self.path = candidate
        self.busy_timeout_ms = busy_timeout_ms
        self.batch_size = batch_size
        self._lock = threading.RLock()
        self._state = STATE_UNAVAILABLE
        self._reason = "missing"
        self._probe = sqlite_capabilities()
        self._load_existing_state()

    @property
    def state(self):
        with self._lock:
            return self._state

    @property
    def reason(self):
        with self._lock:
            return self._reason

    @property
    def available(self):
        return self.state == STATE_READY

    def _set_memory_state(self, state, reason=None):
        self._state = state if state in INDEX_STATES else STATE_UNAVAILABLE
        self._reason = reason

    def _safe_database_path(self, path=None):
        target = self.path if path is None else os.path.abspath(os.fspath(path))
        if _has_link_component(os.path.dirname(target)):
            raise IndexUnavailable("A pasta do índice não pode conter links ou junctions.")
        if os.path.lexists(target) and (
            os.path.islink(target) or getattr(os.path, "isjunction", lambda _path: False)(target)
        ):
            raise IndexUnavailable("O caminho do índice não pode ser um link ou junction.")
        return target

    def _connect(self, path=None, *, create=False):
        target = self._safe_database_path(path)
        if not self._probe.get("fts5"):
            raise IndexUnavailable("FTS5 não está disponível neste interpretador.")
        if not create and not os.path.exists(target):
            raise IndexUnavailable("O índice ainda não foi criado.")
        if create:
            os.makedirs(os.path.dirname(target), exist_ok=True)
        connection = sqlite3.connect(
            target,
            timeout=self.busy_timeout_ms / 1000.0,
            isolation_level=None,
            check_same_thread=True,
        )
        try:
            connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
            return connection
        except Exception:
            connection.close()
            raise

    @staticmethod
    def _schema_sql(connection):
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                notes TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT,
                updated_at TEXT,
                duration REAL NOT NULL,
                error TEXT,
                active_revision TEXT,
                active_report TEXT,
                annotation_generation INTEGER NOT NULL,
                content_fingerprint TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS transcript_segments (
                session_id TEXT NOT NULL,
                revision_id TEXT NOT NULL,
                segment_id TEXT NOT NULL,
                start REAL,
                end REAL,
                track TEXT,
                speaker TEXT,
                text TEXT NOT NULL,
                PRIMARY KEY (session_id, revision_id, segment_id),
                FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS reports (
                session_id TEXT NOT NULL,
                report_id TEXT NOT NULL,
                profile_id TEXT,
                kind TEXT,
                reviewed INTEGER NOT NULL DEFAULT 0,
                text TEXT NOT NULL,
                PRIMARY KEY (session_id, report_id),
                FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS memberships (
                session_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                value TEXT NOT NULL,
                PRIMARY KEY (session_id, kind, value),
                FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
                content,
                source_kind UNINDEXED,
                session_id UNINDEXED,
                revision_id UNINDEXED,
                segment_id UNINDEXED,
                report_id UNINDEXED
            );
            CREATE TABLE IF NOT EXISTS index_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        current = connection.execute("PRAGMA user_version").fetchone()[0]
        if current not in (0, INDEX_SCHEMA_VERSION):
            raise IndexUnavailable("A versão do índice não é compatível.")
        if current == 0:
            connection.execute(f"PRAGMA user_version={INDEX_SCHEMA_VERSION}")
        connection.execute(
            "INSERT OR IGNORE INTO index_metadata(key, value) VALUES ('state', ?)",
            (STATE_INCOMPLETE,),
        )
        connection.execute(
            "INSERT OR IGNORE INTO index_metadata(key, value) VALUES ('build_revision', ?)",
            ("0",),
        )

    @staticmethod
    def _read_state(connection):
        try:
            row = connection.execute("SELECT value FROM index_metadata WHERE key='state'").fetchone()
            return row[0] if row and row[0] in INDEX_STATES else STATE_INCOMPLETE
        except sqlite3.DatabaseError:
            return STATE_INCOMPLETE

    def _load_existing_state(self):
        if not self._probe.get("fts5"):
            self._set_memory_state(STATE_UNAVAILABLE, self._probe.get("error", "fts5 unavailable"))
            return
        if not os.path.lexists(self.path):
            self._set_memory_state(STATE_UNAVAILABLE, "missing")
            return
        if os.path.islink(self.path) or getattr(os.path, "isjunction", lambda _path: False)(self.path):
            self._set_memory_state(STATE_UNAVAILABLE, "link")
            return
        connection = None
        try:
            connection = self._connect(create=False)
            current = connection.execute("PRAGMA user_version").fetchone()[0]
            if current != INDEX_SCHEMA_VERSION:
                raise IndexUnavailable("A versão do índice não é compatível.")
            self._set_memory_state(self._read_state(connection), "on-disk")
        except (sqlite3.DatabaseError, OSError, IndexUnavailable) as error:
            self._set_memory_state(STATE_UNAVAILABLE, str(error))
        finally:
            if connection is not None:
                connection.close()

    def _open_initialized(self, path=None, *, create=True):
        connection = self._connect(path, create=create)
        try:
            self._schema_sql(connection)
            return connection
        except Exception:
            connection.close()
            raise

    @staticmethod
    def _write_state(connection, state, reason=None):
        if state not in INDEX_STATES:
            raise ValueError("Estado do índice inválido.")
        connection.execute(
            "INSERT INTO index_metadata(key, value) VALUES ('state', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (state,),
        )
        if reason is not None:
            connection.execute(
                "INSERT INTO index_metadata(key, value) VALUES ('reason', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(reason)[:1024],),
            )

    @staticmethod
    def _clear_session(connection, session_id):
        connection.execute("DELETE FROM fts WHERE session_id=?", (session_id,))
        connection.execute("DELETE FROM transcript_segments WHERE session_id=?", (session_id,))
        connection.execute("DELETE FROM reports WHERE session_id=?", (session_id,))
        connection.execute("DELETE FROM memberships WHERE session_id=?", (session_id,))
        connection.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))

    @staticmethod
    def _report_rows(reports):
        for report in reports or ():
            if not isinstance(report, dict):
                continue
            report_id = report.get("id") or report.get("report_id")
            if not isinstance(report_id, str):
                continue
            payload = report.get("payload", report.get("generated", report.get("text", "")))
            if isinstance(payload, (dict, list)):
                payload = _json_piece(payload)
            if not isinstance(payload, str):
                payload = str(payload)
            yield (
                report_id,
                report.get("profile_id") or report.get("profile"),
                report.get("kind") or report.get("report_kind"),
                int(bool(report.get("reviewed") or report.get("reviewed_artifact"))),
                payload,
            )

    @staticmethod
    def _projection_rows(metadata, annotations, transcripts, reports):
        session_id = metadata.get("id")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("A projeção exige um identificador de reunião.")
        title = metadata.get("title", "")
        notes = metadata.get("notes", "")
        if annotations:
            title = annotations.get("title", title)
            notes = annotations.get("notes", notes)
        revisions = metadata.get("revisions", [])
        active_revision = revisions[-1].get("id") if revisions and isinstance(revisions[-1], dict) else None
        effective_reports = reports if reports is not None else metadata.get("reports", [])
        report_rows = list(MeetingIndex._report_rows(effective_reports))
        active_report = report_rows[-1][0] if report_rows else None
        generation = annotations.get("generation", 0) if annotations else 0
        fingerprint = fingerprint_canonical(metadata, annotations, transcripts, effective_reports)
        session_row = (
            session_id,
            str(title) if isinstance(title, str) else "",
            str(notes) if isinstance(notes, str) else "",
            str(metadata.get("status", "")),
            metadata.get("created_at"),
            metadata.get("updated_at"),
            float(metadata.get("duration", 0.0) or 0.0),
            metadata.get("error"),
            active_revision,
            active_report,
            int(generation),
            fingerprint,
        )
        segments = []
        for revision_id, values in _sorted_transcripts(transcripts):
            if not revision_id:
                revision_id = str(values[0].get("revision", "")) if values else ""
            for segment in values:
                segments.append((
                    session_id,
                    revision_id,
                    str(segment.get("id", "")),
                    segment.get("start"),
                    segment.get("end"),
                    segment.get("track"),
                    segment.get("speaker"),
                    str(segment.get("text", "")),
                ))
        memberships = []
        if annotations:
            membership_kinds = {
                "collection_ids": "collection",
                "tags": "tag",
                "people": "person",
            }
            for kind in ("collection_ids", "tags", "people"):
                for value in annotations.get(kind, []) or ():
                    memberships.append((session_id, membership_kinds[kind], str(value)))
            if annotations.get("series_id") is not None:
                memberships.append((session_id, "series", str(annotations["series_id"])))
        return session_row, segments, report_rows, memberships

    def _insert_projection(self, connection, metadata, annotations=None, transcripts=None, reports=None):
        """Insert one projection while consuming transcript sources incrementally.

        Rebuild callers commonly pass JSONL-backed generators.  Keeping the
        old ``_projection_rows`` helper for small direct projections is useful
        for compatibility, but the write path itself must never turn a whole
        meeting (or report collection) into Python lists.
        """
        if not isinstance(metadata, dict):
            raise ValueError("A projeção exige metadados de reunião válidos.")
        session_id = metadata.get("id")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("A projeção exige um identificador de reunião.")
        title = metadata.get("title", "")
        notes = metadata.get("notes", "")
        if annotations:
            title = annotations.get("title", title)
            notes = annotations.get("notes", notes)
        revisions = metadata.get("revisions", [])
        active_revision = revisions[-1].get("id") if revisions and isinstance(revisions[-1], dict) else None
        generation = annotations.get("generation", 0) if annotations else 0
        session_row = (
            session_id,
            str(title) if isinstance(title, str) else "",
            str(notes) if isinstance(notes, str) else "",
            str(metadata.get("status", "")),
            metadata.get("created_at"),
            metadata.get("updated_at"),
            float(metadata.get("duration", 0.0) or 0.0),
            metadata.get("error"),
            active_revision,
            None,
            int(generation),
            "",
        )
        self._clear_session(connection, session_id)
        connection.execute(
            "INSERT INTO sessions(session_id,title,notes,status,created_at,updated_at,duration,error,"
            "active_revision,active_report,annotation_generation,content_fingerprint) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            session_row,
        )

        digest = hashlib.sha256()

        def add(marker, value):
            encoded = _json_piece(value).encode("utf-8")
            digest.update(marker.encode("ascii"))
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)

        add("metadata\0", _metadata_without_events(metadata))
        add("annotations\0", annotations if annotations is not None else None)

        connection.execute(
            "INSERT INTO fts(session_id,source_kind,revision_id,segment_id,report_id,content) "
            "VALUES (?,?,?,?,?,?)",
            (session_id, "session", None, None, None,
             "\n".join(value for value in (session_row[1], session_row[2]) if value)),
        )

        def revision_sources(value):
            if value is None:
                return ()
            if isinstance(value, dict):
                return ((str(revision_id), value[revision_id])
                        for revision_id in sorted(value, key=str))
            return (("", value),)

        for revision_id, values in revision_sources(transcripts):
            add("revision\0", revision_id)
            if values is None:
                continue
            for segment in values:
                if not isinstance(segment, dict):
                    continue
                if not revision_id:
                    revision_id = str(segment.get("revision", ""))
                add("segment\0", segment)
                row = (
                    session_id,
                    revision_id,
                    str(segment.get("id", "")),
                    segment.get("start"),
                    segment.get("end"),
                    segment.get("track"),
                    segment.get("speaker"),
                    str(segment.get("text", "")),
                )
                connection.execute(
                    "INSERT INTO transcript_segments(session_id,revision_id,segment_id,start,end,track,speaker,text) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    row,
                )
                connection.execute(
                    "INSERT INTO fts(session_id,source_kind,revision_id,segment_id,report_id,content) "
                    "VALUES (?,?,?,?,?,?)",
                    (session_id, "transcript", row[1], row[2], None, row[7]),
                )

        active_report = None
        effective_reports = reports if reports is not None else metadata.get("reports", ())
        if effective_reports is None:
            effective_reports = ()
        for report in effective_reports:
            if not isinstance(report, dict):
                continue
            row = next(self._report_rows((report,)), None)
            if row is None:
                continue
            report_id, profile_id, kind, reviewed, payload = row
            add("report\0", report)
            # Canonical report readers provide creation order; the last
            # successfully indexed report is the active projection.  UUID
            # lexical order is unrelated to creation time.
            active_report = report_id
            connection.execute(
                "INSERT INTO reports(session_id,report_id,profile_id,kind,reviewed,text) VALUES (?,?,?,?,?,?)",
                (session_id, report_id, profile_id, kind, reviewed, payload),
            )
            connection.execute(
                "INSERT INTO fts(session_id,source_kind,revision_id,segment_id,report_id,content) "
                "VALUES (?,?,?,?,?,?)",
                (session_id, "report", None, None, report_id, payload),
            )

        if annotations:
            values = []
            for key in ("tags", "people", "collection_ids"):
                values.extend(str(item) for item in annotations.get(key, []) or ())
            if annotations.get("series_id"):
                values.append(str(annotations["series_id"]))
            if values:
                for kind, value in (("collection", annotations.get("collection_ids", ())),
                                    ("tag", annotations.get("tags", ())),
                                    ("person", annotations.get("people", ()) )):
                    connection.executemany(
                        "INSERT INTO memberships(session_id,kind,value) VALUES (?,?,?)",
                        ((session_id, kind, str(item)) for item in value or ()),
                    )
                if annotations.get("series_id") is not None:
                    connection.execute(
                        "INSERT INTO memberships(session_id,kind,value) VALUES (?,?,?)",
                        (session_id, "series", str(annotations["series_id"])),
                    )
                connection.execute(
                    "INSERT INTO fts(session_id,source_kind,revision_id,segment_id,report_id,content) "
                    "VALUES (?,?,?,?,?,?)",
                    (session_id, "annotation", None, None, None, " ".join(values)),
                )

        connection.execute(
            "UPDATE sessions SET active_report=?, content_fingerprint=? WHERE session_id=?",
            (active_report, digest.hexdigest(), session_id),
        )

    def index_session(self, metadata, annotations=None, transcripts=None, reports=None):
        """Atomically replace one session projection."""
        if not isinstance(metadata, dict):
            raise ValueError("Os metadados da reunião são inválidos.")
        with self._lock:
            with _writer_lock(self.path):
                connection = None
                try:
                    connection = self._open_initialized(create=True)
                    connection.execute("BEGIN IMMEDIATE")
                    self._write_state(connection, STATE_REBUILDING)
                    self._insert_projection(connection, metadata, annotations, transcripts, reports)
                    self._write_state(connection, STATE_READY)
                    connection.commit()
                    self._set_memory_state(STATE_READY, "indexed")
                    return True
                except Exception as error:
                    if connection is not None:
                        connection.rollback()
                    self._set_memory_state(STATE_STALE if os.path.exists(self.path) else STATE_UNAVAILABLE, str(error))
                    if isinstance(error, (ValueError, IndexUnavailable)):
                        raise
                    raise MeetingIndexError("Não foi possível atualizar o índice descartável.") from error
                finally:
                    if connection is not None:
                        connection.close()

    def index_store_session(self, store, session_id, *, annotations=None, reports=None):
        metadata = store.get(session_id, include_events=False)
        if annotations is None:
            annotations = None
        transcripts = {
            revision.get("id"): list(store.get_transcript(session_id, revision.get("id")))
            for revision in metadata.get("revisions", [])
            if isinstance(revision, dict) and isinstance(revision.get("id"), str)
        }
        return self.index_session(metadata, annotations, transcripts, reports)

    index_meeting = index_session

    def remove_session(self, session_id):
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("O identificador de reunião é inválido.")
        with self._lock:
            with _writer_lock(self.path):
                connection = None
                try:
                    connection = self._open_initialized(create=False)
                    connection.execute("BEGIN IMMEDIATE")
                    self._clear_session(connection, session_id)
                    self._write_state(connection, STATE_READY)
                    connection.commit()
                    self._set_memory_state(STATE_READY, "removed")
                    return True
                except IndexUnavailable:
                    self._set_memory_state(STATE_STALE, "missing")
                    return False
                except Exception as error:
                    if connection is not None:
                        connection.rollback()
                    self._set_memory_state(STATE_STALE, str(error))
                    raise MeetingIndexError("Não foi possível remover a projeção descartável.") from error
                finally:
                    if connection is not None:
                        connection.close()

    remove = remove_session

    def mark_stale(self, reason="canonical data changed"):
        with self._lock:
            with _writer_lock(self.path):
                self._set_memory_state(STATE_STALE, reason)
                if not os.path.exists(self.path):
                    return False
                connection = None
                try:
                    connection = self._open_initialized(create=False)
                    connection.execute("BEGIN IMMEDIATE")
                    self._write_state(connection, STATE_STALE, reason)
                    connection.commit()
                    return True
                except Exception:
                    if connection is not None:
                        connection.rollback()
                    return False
                finally:
                    if connection is not None:
                        connection.close()

    def _fts_query(self, query):
        if not isinstance(query, str) or len(query) > MAX_QUERY_CHARS:
            raise ValueError("A busca do índice é inválida.")
        if any(unicodedata.category(character).startswith("C") for character in query):
            raise ValueError("A busca do índice não pode conter caracteres de controle.")
        tokens = [token for token in query.split() if token]
        # Quoting each token prevents malformed user syntax from escaping the
        # FTS expression while preserving Unicode and ordinary word search.
        return " AND ".join('"' + token.replace('"', '""') + '"' for token in tokens)

    def list_sessions(self, *, offset=0, limit=50, query="", status=""):
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("O deslocamento do índice é inválido.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= 500:
            raise ValueError("O limite do índice é inválido.")
        if not isinstance(status, str):
            raise ValueError("O filtro do índice é inválido.")
        if limit == 0:
            return []
        if self.state != STATE_READY:
            return []
        fts_query = self._fts_query(query)
        connection = None
        try:
            connection = self._connect(create=False)
            if fts_query:
                sql = (
                    "SELECT s.session_id,s.title,s.status,s.created_at,s.duration,s.error "
                    "FROM sessions s JOIN (SELECT DISTINCT session_id FROM fts WHERE fts MATCH ?) f "
                    "ON f.session_id=s.session_id "
                )
                params = [fts_query]
            else:
                sql = "SELECT session_id,title,status,created_at,duration,error FROM sessions "
                params = []
            if status:
                sql += " WHERE s.status=?" if fts_query else " WHERE status=?"
                params.append(status)
            sql += " ORDER BY s.created_at DESC, s.session_id DESC LIMIT ? OFFSET ?" if fts_query else \
                " ORDER BY created_at DESC, session_id DESC LIMIT ? OFFSET ?"
            params.extend((limit, offset))
            rows = connection.execute(sql, params).fetchall()
            return [
                {"id": row[0], "title": row[1], "status": row[2], "created_at": row[3],
                 "duration": row[4], "error": row[5]}
                for row in rows
            ]
        except (sqlite3.DatabaseError, IndexUnavailable) as error:
            self._set_memory_state(STATE_UNAVAILABLE, str(error))
            return []
        finally:
            if connection is not None:
                connection.close()

    def search(self, query, *, limit=50, offset=0):
        if self.state != STATE_READY:
            return []
        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= 500:
            raise ValueError("O limite da busca é inválido.")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("O deslocamento da busca é inválido.")
        fts_query = self._fts_query(query)
        if not fts_query or limit == 0:
            return []
        connection = None
        try:
            connection = self._connect(create=False)
            rows = connection.execute(
                "SELECT source_kind,session_id,revision_id,segment_id,report_id,"
                "snippet(fts,0,'','', '…', 32) FROM fts WHERE fts MATCH ? "
                "ORDER BY bm25(fts) LIMIT ? OFFSET ?",
                (fts_query, limit, offset),
            ).fetchall()
            return [
                {"source_kind": row[0], "session_id": row[1], "revision_id": row[2],
                 "segment_id": row[3], "report_id": row[4], "snippet": row[5]}
                for row in rows
            ]
        except (sqlite3.DatabaseError, IndexUnavailable) as error:
            self._set_memory_state(STATE_UNAVAILABLE, str(error))
            return []
        finally:
            if connection is not None:
                connection.close()

    def _set_existing_state(self, state, reason=None):
        connection = None
        try:
            connection = self._open_initialized(create=False)
            connection.execute("BEGIN IMMEDIATE")
            self._write_state(connection, state, reason)
            connection.commit()
            return True
        finally:
            if connection is not None:
                connection.close()

    def _prepare_swap_target(self):
        """Checkpoint and remove old sidecars before replacing the database."""
        if os.path.exists(self.path):
            connection = None
            try:
                connection = sqlite3.connect(
                    self.path, timeout=self.busy_timeout_ms / 1000.0, isolation_level=None,
                )
                try:
                    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.DatabaseError:
                    # A corrupt disposable database can still be replaced. Its
                    # WAL cannot be trusted and must not be carried forward.
                    pass
            finally:
                if connection is not None:
                    connection.close()
        for suffix in ("-wal", "-shm"):
            sidecar = self.path + suffix
            try:
                os.remove(sidecar)
            except FileNotFoundError:
                pass

    @staticmethod
    def _remove_temp_sidecars(path):
        for suffix in ("-wal", "-shm"):
            try:
                os.remove(path + suffix)
            except OSError:
                pass

    @staticmethod
    def _normalize_rebuild_item(item):
        if isinstance(item, dict):
            return item, None, None, None
        if not isinstance(item, (tuple, list)) or not item or not isinstance(item[0], dict):
            raise ValueError("A entrada do rebuild é inválida.")
        values = list(item) + [None] * 4
        return values[0], values[1], values[2], values[3]

    def _publish_incomplete(self, temp_path):
        connection = None
        try:
            connection = self._open_initialized(temp_path, create=True)
            connection.execute("BEGIN IMMEDIATE")
            self._write_state(connection, STATE_INCOMPLETE, "rebuild cancelled")
            connection.commit()
        finally:
            if connection is not None:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                connection.close()
        self._remove_temp_sidecars(temp_path)
        self._prepare_swap_target()
        os.replace(temp_path, self.path)

    def rebuild(self, sessions, *, cancel_event=None, progress=None):
        """Build a deterministic disposable replacement and publish it once."""
        if cancel_event is not None and cancel_event.is_set():
            raise IndexCancelled("A reconstrução do índice foi cancelada.")
        with self._lock:
            with _writer_lock(self.path):
                had_existing = os.path.exists(self.path) and self.state in {
                    STATE_READY, STATE_STALE, STATE_INCOMPLETE
                }
                prior_state = self.state
                temp_path = f"{self.path}.rebuild-{uuid.uuid4().hex}.tmp"
                self._set_memory_state(STATE_REBUILDING, "building replacement")
                if had_existing:
                    try:
                        self._set_existing_state(STATE_REBUILDING, "building replacement")
                    except Exception as error:
                        self._set_memory_state(STATE_UNAVAILABLE, str(error))
                        raise IndexUnavailable("O índice existente não pode ser preparado para rebuild.") from error
                connection = None
                published = False
                processed = 0
                try:
                    connection = self._open_initialized(temp_path, create=True)
                    connection.execute("BEGIN IMMEDIATE")
                    self._write_state(connection, STATE_REBUILDING)
                    # ``sessions`` may be a generator whose transcript values
                    # are themselves JSONL streams.  Never materialize the
                    # catalog merely to sort it: callers that need a stable
                    # order provide one, while SQL readers retain explicit
                    # ordering for user-visible results.
                    total = len(sessions) if hasattr(sessions, "__len__") else None
                    for raw in sessions:
                        if cancel_event is not None and cancel_event.is_set():
                            raise IndexCancelled("A reconstrução do índice foi cancelada.")
                        metadata, annotations, transcripts, reports = self._normalize_rebuild_item(raw)
                        self._insert_projection(connection, metadata, annotations, transcripts, reports)
                        processed += 1
                        if processed % self.batch_size == 0:
                            connection.commit()
                            connection.execute("BEGIN IMMEDIATE")
                            if progress is not None:
                                progress(processed, total)
                    self._write_state(connection, STATE_READY)
                    connection.commit()
                    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    connection.close()
                    connection = None
                    self._remove_temp_sidecars(temp_path)
                    self._prepare_swap_target()
                    os.replace(temp_path, self.path)
                    published = True
                    self._set_memory_state(STATE_READY, f"rebuilt {processed}")
                    if progress is not None:
                        progress(processed, total if total is not None else processed)
                    return {"state": STATE_READY, "sessions": processed}
                except IndexCancelled:
                    if connection is not None:
                        connection.rollback()
                        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                        connection.close()
                        connection = None
                    self._remove_temp_sidecars(temp_path)
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass
                    if had_existing:
                        try:
                            self._set_existing_state(prior_state if prior_state in INDEX_STATES else STATE_READY)
                        except Exception:
                            pass
                        self._set_memory_state(prior_state if prior_state in INDEX_STATES else STATE_READY, "cancelled")
                    else:
                        # There was no usable index to preserve.  Publish only an
                        # explicitly incomplete empty disposable replacement.
                        self._publish_incomplete(temp_path)
                        self._set_memory_state(STATE_INCOMPLETE, "cancelled")
                    raise
                except Exception as error:
                    if connection is not None:
                        connection.rollback()
                        connection.close()
                        connection = None
                    self._remove_temp_sidecars(temp_path)
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass
                    if had_existing:
                        try:
                            self._set_existing_state(prior_state if prior_state in INDEX_STATES else STATE_STALE, str(error))
                        except Exception:
                            pass
                        self._set_memory_state(prior_state if prior_state in INDEX_STATES else STATE_STALE, str(error))
                    else:
                        self._set_memory_state(STATE_UNAVAILABLE, str(error))
                    if isinstance(error, (ValueError, IndexUnavailable)):
                        raise
                    raise MeetingIndexError("A reconstrução do índice falhou; o catálogo descartável foi preservado.") from error
                finally:
                    if connection is not None:
                        connection.close()
                    if not published:
                        for suffix in ("", "-wal", "-shm"):
                            try:
                                os.remove(temp_path + suffix)
                            except OSError:
                                pass


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "INDEX_SCHEMA_VERSION",
    "INDEX_STATES",
    "IndexCancelled",
    "IndexUnavailable",
    "MeetingIndex",
    "MeetingIndexError",
    "STATE_INCOMPLETE",
    "STATE_READY",
    "STATE_REBUILDING",
    "STATE_STALE",
    "STATE_UNAVAILABLE",
    "fingerprint_canonical",
    "fts5_available",
    "sqlite_capabilities",
]
