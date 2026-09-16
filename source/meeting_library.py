"""Canonical meeting-library seam above :mod:`meeting_store`.

``MeetingStore`` remains the capture/recovery authority.  This module owns the
additive human state in ``annotations.json`` and ``workspace.json`` and treats
the SQLite catalog as disposable.  It deliberately keeps the public methods
small so GUI/controller code never needs to know which canonical file or
projection supplies a value.
"""

import contextlib
import copy
from datetime import datetime
import json
import math
import os
import re
import threading
import time
import unicodedata
import uuid

from meeting_store import MeetingStore
from snippet_utils import write_json_atomic


WORKSPACE_SCHEMA_VERSION = 1
ANNOTATIONS_SCHEMA_VERSION = 1
WORKSPACE_FILENAME = "workspace.json"
ANNOTATIONS_FILENAME = "annotations.json"
REPORTS_DIRECTORY = "reports"
REPORT_SCHEMA_VERSION = 1
MAX_WORKSPACE_BYTES = 2 * 1024 * 1024
MAX_ANNOTATIONS_BYTES = 2 * 1024 * 1024
MAX_REPORT_BYTES = 2 * 1024 * 1024
MAX_REPORTS = 10_000
MAX_TITLE_CHARS = 400
MAX_NOTES_BYTES = 1024 * 1024
MAX_BOOKMARKS = 10_000
MAX_HIGHLIGHTS = 10_000
MAX_LABEL_CHARS = 256
MAX_TAGS = 512
MAX_PEOPLE = 512
MAX_COLLECTIONS = 1024
MAX_SPEAKER_LABELS = 10_000
MAX_HIGHLIGHT_SEGMENTS = 512
MAX_ID_CHARS = 128
REPORT_SECTIONS = frozenset({
    "summary", "decisions", "action_items", "open_questions", "risks",
    "objections", "feedback", "follow_up_email", "answer",
})
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_REFERENCE_RE = re.compile(r"^[^/\\\x00]{1,128}$")
_UNSET = object()


class MeetingLibraryError(ValueError):
    """Base error for malformed or unsafe canonical library state."""


class SchemaError(MeetingLibraryError):
    """A canonical file is malformed or uses an unsupported older schema."""


class UnsupportedSchemaError(SchemaError):
    """A newer canonical schema was found and is therefore read-only."""


class PathSafetyError(MeetingLibraryError):
    """A bundle or canonical file would follow a link outside the library."""


class AnnotationConflict(MeetingLibraryError):
    """A compare-and-swap annotation update observed a newer generation."""

    def __init__(self, expected, actual):
        super().__init__(
            "A anotação mudou em outra janela; recarregue antes de salvar "
            f"(esperado {expected}, atual {actual})."
        )
        self.expected = expected
        self.actual = actual


class WorkspaceConflict(MeetingLibraryError):
    """A compare-and-swap workspace update observed a newer generation."""

    def __init__(self, expected, actual):
        super().__init__(
            "O espaço de reuniões mudou em outra janela; recarregue antes de salvar "
            f"(esperado {expected}, atual {actual})."
        )
        self.expected = expected
        self.actual = actual


class _LibraryStoreView:
    """Read-only store-shaped view used by legacy export/playback helpers."""

    def __init__(self, library):
        self._library = library
        self.root = library.meetings_root

    def get(self, session_id, include_events=True):
        return self._library.get_session(session_id, include_events=include_events)

    def get_transcript(self, session_id, revision=None):
        return self._library.get_transcript(session_id, revision)

    def iter_events(self, session_id):
        return self._library.store.iter_events(session_id)

    def iter_audio(self, session_id, track=None, start=0.0):
        return self._library.store.iter_audio(session_id, track=track, start=start)


_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS = {}


def _utc_timestamp():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _path_lock(path):
    """Return a process-wide lock for one canonical file path.

    The lock spans disk read, compare, merge, and atomic replace for multiple
    ``MeetingLibrary`` instances in this process.  Atomic replacement still
    provides the crash boundary; cross-process callers must pass a generation
    and will be rejected after the next disk read.
    """
    key = os.path.normcase(os.path.abspath(os.fspath(path)))
    with _LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[key] = lock
        return lock


def _copy_json(value, label):
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise MeetingLibraryError(f"{label} contém valores que não podem ser salvos.") from error
    return copy.deepcopy(value), len(encoded.encode("utf-8"))


def _valid_generation(value, label="generation"):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SchemaError(f"A geração {label} é inválida.")
    if value > 2**63 - 1:
        raise SchemaError(f"A geração {label} excede o limite permitido.")
    return value


def _valid_id(value, *, reference=False):
    if not isinstance(value, str) or len(value) > MAX_ID_CHARS:
        return False
    return bool((_REFERENCE_RE if reference else _ID_RE).fullmatch(value))


def _valid_timestamp(value):
    if not isinstance(value, str) or not value or len(value) > 64:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return parsed.tzinfo is not None


def _finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _is_link_or_junction(path):
    return os.path.islink(path) or getattr(os.path, "isjunction", lambda _path: False)(path)


def _commonpath_is(root, path):
    try:
        return os.path.commonpath((root, path)) == root
    except (OSError, ValueError):
        return False


def _has_link_component(path):
    """Return whether an existing parent component is a link or junction."""
    absolute = os.path.abspath(os.fspath(path))
    drive, tail = os.path.splitdrive(absolute)
    current = drive + os.sep if drive else os.sep
    for part in tail.strip("\\/").split(os.sep):
        if not part:
            continue
        current = os.path.join(current, part)
        if os.path.lexists(current) and _is_link_or_junction(current):
            return True
    return False


@contextlib.contextmanager
def _cross_process_lock(path):
    """Serialize canonical compare-and-swap writers across processes."""
    lock_path = f"{os.fspath(path)}.lock"
    if _has_link_component(os.path.dirname(lock_path)) or (
        os.path.lexists(lock_path) and _is_link_or_junction(lock_path)
    ):
        raise PathSafetyError("O bloqueio canônico aponta para um link ou junction.")
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

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
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


class MeetingLibrary:
    """User-facing canonical meeting operations and disposable-index seam.

    ``root`` may be an application home (containing ``meetings``) or a
    ``MeetingStore``/meeting root for tests.  The application passes an
    explicit ``workspace_root`` so the database and workspace sit beside, not
    inside, the meeting bundles.
    """

    def __init__(self, root=None, *, store=None, index=None, workspace_root=None):
        if isinstance(root, MeetingStore) and store is None:
            store = root
            root = None
        if store is not None:
            if not hasattr(store, "root"):
                raise ValueError("O armazenamento de reuniões é inválido.")
            meetings_root = os.path.abspath(os.fspath(store.root))
            if root is None:
                root = workspace_root
            if root is None:
                root = os.path.dirname(meetings_root) if os.path.basename(meetings_root).casefold() == "meetings" else meetings_root
        else:
            if root is None:
                raise ValueError("A pasta da biblioteca é obrigatória.")
            root = os.path.abspath(os.fspath(root))
            candidate = os.path.join(root, "meetings")
            if os.path.isdir(candidate):
                meetings_root = candidate
            elif os.path.basename(root).casefold() == "meetings":
                meetings_root = root
                root = os.path.dirname(root)
            else:
                meetings_root = root
        self.home_root = os.path.abspath(os.fspath(workspace_root or root))
        self.meetings_root = os.path.abspath(meetings_root)
        self._store = store
        self._store_lock = threading.RLock()
        self._index = index
        self._index_lock = threading.RLock()
        self._index_workers = set()
        self._index_workers_by_session = {}
        self._index_pending = set()
        self._index_closed = False
        self._index_stale = False
        self._catalog_lock = threading.RLock()
        if _has_link_component(self.home_root) or _has_link_component(self.meetings_root):
            raise PathSafetyError("As raízes da biblioteca não podem conter links ou junctions.")
        os.makedirs(self.home_root, exist_ok=True)
        os.makedirs(self.meetings_root, exist_ok=True)

    @property
    def store(self):
        """Return the one injected/lazily-created ``MeetingStore`` instance."""
        with self._store_lock:
            if self._store is None:
                self._store = MeetingStore(self.meetings_root)
            return self._store

    @property
    def index(self):
        """Return the disposable index, created only when a projection is used."""
        with self._index_lock:
            if self._index is None:
                from meeting_index import MeetingIndex

                self._index = MeetingIndex(os.path.join(self.home_root, "library.sqlite"))
            return self._index

    @property
    def index_state(self):
        try:
            return self.index.state
        except Exception:
            return "unavailable"

    # -- Canonical path and JSON helpers ---------------------------------

    def _session_dir(self, session_id):
        if not _valid_id(session_id):
            raise ValueError("Identificador de reunião inválido.")
        root = os.path.realpath(self.meetings_root)
        result = os.path.abspath(os.path.join(self.meetings_root, session_id))
        if not _commonpath_is(root, os.path.realpath(result)):
            raise PathSafetyError("A pasta da reunião aponta para fora da biblioteca.")
        if os.path.lexists(result) and _is_link_or_junction(result):
            raise PathSafetyError("A pasta da reunião não pode ser um link ou junction.")
        # Check every existing component beneath the meetings root.  This also
        # catches a nested link if a future bundle path gains subdirectories.
        relative = os.path.relpath(result, self.meetings_root)
        current = os.path.abspath(self.meetings_root)
        for part in relative.split(os.sep):
            if part in ("", "."):
                continue
            current = os.path.join(current, part)
            if os.path.lexists(current) and _is_link_or_junction(current):
                raise PathSafetyError("A pasta da reunião contém um link ou junction.")
        return result

    def _canonical_path(self, session_id, name):
        session_dir = self._session_dir(session_id)
        path = os.path.join(session_dir, name)
        if os.path.lexists(path) and _is_link_or_junction(path):
            raise PathSafetyError("O arquivo canônico não pode ser um link ou junction.")
        if not _commonpath_is(os.path.realpath(self.meetings_root), os.path.realpath(path)):
            raise PathSafetyError("O arquivo canônico aponta para fora da biblioteca.")
        return path

    def _workspace_path(self):
        path = os.path.join(self.home_root, WORKSPACE_FILENAME)
        if os.path.lexists(path) and _is_link_or_junction(path):
            raise PathSafetyError("O workspace não pode ser um link ou junction.")
        return path

    @staticmethod
    def _load_versioned(path, expected, label, max_bytes):
        # ``lexists`` is deliberate: a broken link is a safety/corruption
        # condition, never permission to fall back to legacy metadata.
        if not os.path.lexists(path):
            return None
        if _is_link_or_junction(path):
            raise PathSafetyError(f"O arquivo {label} não pode ser um link ou junction.")
        try:
            size = os.path.getsize(path)
            if size > max_bytes:
                raise SchemaError(f"O arquivo {label} excede o limite permitido.")
            with open(path, "r", encoding="utf-8") as handle:
                value = json.load(handle)
        except SchemaError:
            raise
        except (OSError, UnicodeError, ValueError, TypeError) as error:
            raise SchemaError(
                f"O arquivo {label} está corrompido; ele foi preservado e precisa de reparo manual."
            ) from error
        if not isinstance(value, dict):
            raise SchemaError(f"O arquivo {label} deve conter um objeto; ele foi preservado.")
        version = value.get("schema_version")
        if isinstance(version, bool) or not isinstance(version, int):
            raise SchemaError(f"O schema de {label} é inválido; o arquivo foi preservado.")
        if version > expected:
            raise UnsupportedSchemaError(
                f"O schema de {label} é mais novo que esta versão; abra em uma versão compatível."
            )
        if version < expected:
            raise SchemaError(f"O schema antigo de {label} não é gravável; atualize/reimporte o arquivo.")
        return value

    # -- Workspace --------------------------------------------------------

    @staticmethod
    def _default_workspace():
        return {
            "schema_version": WORKSPACE_SCHEMA_VERSION,
            "generation": 0,
            "collections": [],
            "series": [],
            "profiles": [],
            "privacy_defaults": {},
            "retention_defaults": {},
            "updated_at": _utc_timestamp(),
        }

    def read_workspace(self):
        value = self._load_versioned(
            self._workspace_path(), WORKSPACE_SCHEMA_VERSION, "workspace.json", MAX_WORKSPACE_BYTES
        )
        if value is None:
            value = self._default_workspace()
        self._validate_workspace(value)
        return copy.deepcopy(value)

    def _validate_workspace(self, value):
        if not isinstance(value, dict) or value.get("schema_version") != WORKSPACE_SCHEMA_VERSION:
            raise SchemaError("O workspace tem um schema inválido; o arquivo foi preservado.")
        _valid_generation(value.get("generation"), "do workspace")
        if not _valid_timestamp(value.get("updated_at")):
            raise SchemaError("A data de atualização do workspace é inválida; o arquivo foi preservado.")
        for key in ("collections", "series", "profiles"):
            if not isinstance(value.get(key), list) or len(value[key]) > MAX_COLLECTIONS:
                raise SchemaError(f"A lista {key} do workspace é inválida; o arquivo foi preservado.")
            seen = set()
            seen_names = set()
            for item in value[key]:
                if not isinstance(item, dict) or not _valid_id(item.get("id")):
                    raise SchemaError(f"A definição {key} do workspace é inválida; o arquivo foi preservado.")
                if item["id"] in seen:
                    raise SchemaError(f"A definição {key} do workspace é duplicada; o arquivo foi preservado.")
                seen.add(item["id"])
                name = item.get("name", "")
                if (
                    not isinstance(name, str)
                    or not name
                    or len(name) > MAX_LABEL_CHARS
                    or name != unicodedata.normalize("NFC", name.strip())
                ):
                    raise SchemaError(f"O nome de {key} do workspace é inválido; o arquivo foi preservado.")
                folded_name = name.casefold()
                if folded_name in seen_names:
                    raise SchemaError(f"O nome de {key} do workspace é duplicado; o arquivo foi preservado.")
                seen_names.add(folded_name)
                if key == "collections" and item.get("kind", "folder") not in {"folder", "project"}:
                    raise SchemaError("O tipo da coleção é inválido; o arquivo foi preservado.")
                if "archived" in item and not isinstance(item["archived"], bool):
                    raise SchemaError(f"O estado arquivado de {key} é inválido; o arquivo foi preservado.")
        for key in ("privacy_defaults", "retention_defaults"):
            if not isinstance(value.get(key), dict):
                raise SchemaError(f"As configurações {key} do workspace são inválidas; o arquivo foi preservado.")
        _, size = _copy_json(value, "workspace.json")
        if size > MAX_WORKSPACE_BYTES:
            raise SchemaError("O workspace excede o limite permitido; o arquivo foi preservado.")

    def update_workspace(self, patch, *, expected_generation=_UNSET):
        if not isinstance(patch, dict):
            raise ValueError("A alteração do workspace deve ser um objeto.")
        path = self._workspace_path()
        with _writer_lock(path):
            current = self.read_workspace()
            actual = _valid_generation(current["generation"], "do workspace")
            if expected_generation is not _UNSET:
                expected = _valid_generation(expected_generation, "esperada")
                if expected != actual:
                    raise WorkspaceConflict(expected, actual)
            merged = copy.deepcopy(current)
            for key, value in patch.items():
                if key in {"schema_version", "generation", "updated_at"}:
                    raise ValueError("Campos de versão do workspace são controlados pela biblioteca.")
                merged[key] = copy.deepcopy(value)
            merged["generation"] = actual + 1
            merged["updated_at"] = _utc_timestamp()
            self._validate_workspace(merged)
            _, size = _copy_json(merged, "workspace.json")
            if size > MAX_WORKSPACE_BYTES:
                raise ValueError("O workspace excede o limite permitido.")
            os.makedirs(self.home_root, exist_ok=True)
            write_json_atomic(path, merged)
            return copy.deepcopy(merged)

    # Compatibility aliases for callers that use a save/read vocabulary.
    read_workspace_state = read_workspace
    save_workspace = update_workspace

    @staticmethod
    def _workspace_definition(value, *, kind):
        if not isinstance(value, dict) or not _valid_id(value.get("id")):
            raise ValueError(f"A definição de {kind} é inválida.")
        name = value.get("name")
        if not isinstance(name, str):
            raise ValueError(f"O nome de {kind} é inválido.")
        name = unicodedata.normalize("NFC", name.strip())
        if not name or len(name) > MAX_LABEL_CHARS:
            raise ValueError(f"O nome de {kind} é inválido.")
        result = {
            "id": value["id"],
            "name": name,
            "archived": bool(value.get("archived", False)),
        }
        if kind == "coleção":
            collection_kind = value.get("kind", "folder")
            if collection_kind not in {"folder", "project"}:
                raise ValueError("O tipo da coleção é inválido.")
            result["kind"] = collection_kind
        return result

    def _save_workspace_definition(self, key, definition, *, expected_generation):
        workspace = self.read_workspace()
        if workspace["generation"] != expected_generation:
            raise WorkspaceConflict(expected_generation, workspace["generation"])
        values = copy.deepcopy(workspace[key])
        replaced = False
        for index, current in enumerate(values):
            if current.get("id") == definition["id"]:
                values[index] = copy.deepcopy(definition)
                replaced = True
                break
        if not replaced:
            values.append(copy.deepcopy(definition))
        updated = self.update_workspace(
            {key: values}, expected_generation=expected_generation,
        )
        return next(copy.deepcopy(item) for item in updated[key] if item["id"] == definition["id"])

    def save_collection(self, value, *, expected_generation):
        definition = self._workspace_definition(value, kind="coleção")
        return self._save_workspace_definition(
            "collections", definition, expected_generation=expected_generation,
        )

    def save_series(self, value, *, expected_generation):
        definition = self._workspace_definition(value, kind="série")
        return self._save_workspace_definition(
            "series", definition, expected_generation=expected_generation,
        )

    # -- Annotations ------------------------------------------------------

    @staticmethod
    def _default_annotations(metadata):
        bookmarks = metadata.get("bookmarks", [])
        if not isinstance(bookmarks, list):
            bookmarks = []
        return {
            "schema_version": ANNOTATIONS_SCHEMA_VERSION,
            "generation": 0,
            "title": metadata.get("title", "") if isinstance(metadata.get("title", ""), str) else "",
            "notes": metadata.get("notes", "") if isinstance(metadata.get("notes", ""), str) else "",
            "bookmarks": copy.deepcopy(bookmarks),
            "highlights": [],
            "speaker_labels": {},
            "collection_ids": [],
            "tags": [],
            "people": [],
            "series_id": None,
            "reviewed_artifacts": {},
            "reviewed_summary": copy.deepcopy(metadata.get("reviewed_summary", "")),
            "active_report_id": None,
            "retention_override": None,
            "updated_at": metadata.get("updated_at") if _valid_timestamp(metadata.get("updated_at")) else _utc_timestamp(),
        }

    def _annotations_path(self, session_id):
        return self._canonical_path(session_id, ANNOTATIONS_FILENAME)

    def has_annotation_sidecar(self, session_id):
        """Return whether the versioned annotation file exists on disk."""
        return os.path.lexists(self._annotations_path(session_id))

    def read_annotations(self, session_id, *, revision=None, active_only=False, active_revision=False):
        metadata = self.store.get(session_id, include_events=False)
        path = self._annotations_path(session_id)
        value = self._load_versioned(
            path, ANNOTATIONS_SCHEMA_VERSION, "annotations.json", MAX_ANNOTATIONS_BYTES
        )
        if value is None:
            value = self._default_annotations(metadata)
            self._validate_annotations(value, metadata, from_disk=False)
            result = value
        else:
            self._validate_annotations(value, metadata, from_disk=True)
            result = copy.deepcopy(value)
        if active_revision:
            active_only = True
        if active_only:
            revision = revision or self._active_revision_id(metadata)
        if revision is not None:
            if not isinstance(revision, str) or not _valid_id(revision, reference=True):
                raise ValueError("A revisão de transcrição é inválida.")
            result = self._filter_annotation_revision(result, revision)
        return result

    @staticmethod
    def _filter_annotation_revision(value, revision):
        result = copy.deepcopy(value)
        result["highlights"] = [
            item for item in result.get("highlights", [])
            if (item.get("revision") or item.get("transcript_revision")) == revision
        ]
        result["speaker_labels"] = {
            key: item for key, item in result.get("speaker_labels", {}).items()
            if (item.get("revision") or item.get("transcript_revision")) == revision
        }
        result["revision_filter"] = revision
        return result

    @staticmethod
    def _active_revision_id(metadata):
        """Return the newest usable revision without rewriting older provenance."""
        explicit = metadata.get("active_revision")
        revisions = metadata.get("revisions", [])
        known = {
            item.get("id") for item in revisions
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        if isinstance(explicit, str) and explicit in known:
            return explicit
        for item in reversed(revisions):
            if (
                isinstance(item, dict)
                and item.get("status") == "completed"
                and isinstance(item.get("id"), str)
            ):
                return item["id"]
        for item in reversed(revisions):
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                return item["id"]
        return None

    @staticmethod
    def _session_duration(metadata, track=None):
        duration = metadata.get("duration", 0.0)
        duration = float(duration) if _finite_number(duration) else 0.0
        tracks = metadata.get("tracks", {})
        if isinstance(tracks, dict):
            selected = tracks.get(track) if track is not None else None
            items = [selected] if isinstance(selected, dict) else list(tracks.values())
            track_ends = []
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get("segments"), list):
                    continue
                for segment in item["segments"]:
                    end = segment.get("end") if isinstance(segment, dict) else None
                    if _finite_number(end):
                        track_ends.append(float(end))
            if track is not None and track_ends:
                track_duration = max(track_ends)
                duration = min(duration, track_duration) if duration > 0 else track_duration
        return max(0.0, duration)

    @staticmethod
    def _validate_highlight_record(highlight, metadata, transcript_segments, seen_ids):
        if highlight["id"] in seen_ids:
            raise SchemaError("Há destaques duplicados; o arquivo foi preservado.")
        revision = highlight.get("revision") or highlight.get("transcript_revision")
        if revision not in transcript_segments:
            raise SchemaError("Um destaque referencia uma revisão inexistente; o arquivo foi preservado.")
        start, end = highlight.get("start"), highlight.get("end")
        if (
            not _finite_number(start) or not _finite_number(end)
            or float(start) < 0 or float(end) <= float(start)
            or float(end) > MeetingLibrary._session_duration(metadata, highlight.get("track"))
        ):
            raise SchemaError("O intervalo de um destaque é inválido; o arquivo foi preservado.")
        track = highlight.get("track")
        tracks = metadata.get("tracks", {})
        if track not in {"microphone", "system"} or not isinstance(tracks, dict) or track not in tracks:
            raise SchemaError("A fonte de um destaque é inválida; o arquivo foi preservado.")
        segment_ids = highlight.get("segment_ids", highlight.get("segments", []))
        if (
            not isinstance(segment_ids, list) or not segment_ids
            or len(segment_ids) > MAX_HIGHLIGHT_SEGMENTS
            or len(set(segment_ids)) != len(segment_ids)
            or any(not _valid_id(item, reference=True) for item in segment_ids)
        ):
            raise SchemaError("Os segmentos de um destaque são inválidos; o arquivo foi preservado.")
        for segment_id in segment_ids:
            segment = transcript_segments[revision].get(segment_id)
            if segment is None:
                raise SchemaError("Um destaque referencia segmento inexistente; o arquivo foi preservado.")
            if segment.get("track") not in {None, track}:
                raise SchemaError("A fonte de um destaque não corresponde aos segmentos citados; o arquivo foi preservado.")
        label = highlight.get("label", "")
        note = highlight.get("note", "")
        if not isinstance(label, str) or len(label) > MAX_LABEL_CHARS:
            raise SchemaError("O rótulo de um destaque é inválido; o arquivo foi preservado.")
        if not isinstance(note, str) or len(note.encode("utf-8")) > MAX_NOTES_BYTES:
            raise SchemaError("A nota de um destaque é inválida; o arquivo foi preservado.")

    def _transcript_segments(self, session_id, revision):
        metadata = self.store.get(session_id, include_events=False)
        revisions = {
            item.get("id") for item in metadata.get("revisions", [])
            if isinstance(item, dict)
        }
        if revision not in revisions:
            raise ValueError("A revisão de transcrição não existe.")
        segments = list(self.store.get_transcript(session_id, revision))
        by_id = {}
        for segment in segments:
            segment_id = segment.get("id") if isinstance(segment, dict) else None
            if not _valid_id(segment_id, reference=True):
                raise SchemaError("A identidade de um segmento de transcrição é inválida.")
            if segment_id in by_id:
                raise SchemaError("Há segmentos de transcrição duplicados.")
            by_id[segment_id] = segment
        return metadata, by_id

    def _validate_annotations(self, value, metadata, *, from_disk):
        if not isinstance(value, dict) or value.get("schema_version") != ANNOTATIONS_SCHEMA_VERSION:
            raise SchemaError("O schema de annotations.json é inválido; o arquivo foi preservado.")
        _valid_generation(value.get("generation"), "das anotações")
        title = value.get("title")
        notes = value.get("notes")
        if not isinstance(title, str) or len(title) > MAX_TITLE_CHARS:
            raise SchemaError("O título da anotação é inválido; o arquivo foi preservado.")
        if not isinstance(notes, str) or len(notes.encode("utf-8")) > MAX_NOTES_BYTES:
            raise SchemaError("As notas da anotação são inválidas; o arquivo foi preservado.")
        bookmarks = value.get("bookmarks")
        if not isinstance(bookmarks, list) or len(bookmarks) > MAX_BOOKMARKS:
            raise SchemaError("Os marcadores da anotação são inválidos; o arquivo foi preservado.")
        for bookmark in bookmarks:
            if not isinstance(bookmark, dict):
                raise SchemaError("Os marcadores da anotação são inválidos; o arquivo foi preservado.")
            for key in ("time", "timestamp"):
                if key in bookmark and (not _finite_number(bookmark[key]) or float(bookmark[key]) < 0):
                    raise SchemaError("O instante de um marcador é inválido; o arquivo foi preservado.")
            if "label" in bookmark and (not isinstance(bookmark["label"], str)
                                         or len(bookmark["label"]) > MAX_LABEL_CHARS):
                raise SchemaError("O rótulo de um marcador é inválido; o arquivo foi preservado.")
        revisions = {
            item.get("id"): item for item in metadata.get("revisions", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        referenced_revisions = {
            item.get("revision") or item.get("transcript_revision")
            for item in value.get("highlights", []) if isinstance(item, dict)
        }
        referenced_revisions.update(
            item.get("revision") or item.get("transcript_revision")
            for item in value.get("speaker_labels", {}).values() if isinstance(item, dict)
        )
        transcript_segments = {}
        for revision_id in referenced_revisions:
            if revision_id not in revisions:
                raise SchemaError("Uma anotação referencia uma revisão inexistente; o arquivo foi preservado.")
            try:
                segments = list(self.store.get_transcript(metadata["id"], revision_id))
            except (OSError, ValueError, KeyError) as error:
                raise SchemaError("A transcrição referenciada pelas anotações não pôde ser lida.") from error
            by_id = {}
            for segment in segments:
                if not isinstance(segment, dict) or not _valid_id(segment.get("id"), reference=True):
                    raise SchemaError("A identidade de um segmento de transcrição é inválida.")
                if segment["id"] in by_id:
                    raise SchemaError("Há segmentos de transcrição duplicados.")
                by_id[segment["id"]] = segment
            transcript_segments[revision_id] = by_id

        highlights = value.get("highlights")
        if not isinstance(highlights, list) or len(highlights) > MAX_HIGHLIGHTS:
            raise SchemaError("Os destaques da anotação são inválidos; o arquivo foi preservado.")
        seen_ids = set()
        for highlight in highlights:
            if not isinstance(highlight, dict) or not _valid_id(highlight.get("id"), reference=True):
                raise SchemaError("Um destaque tem identificador inválido; o arquivo foi preservado.")
            self._validate_highlight_record(highlight, metadata, transcript_segments, seen_ids)
            seen_ids.add(highlight["id"])

        speaker_labels = value.get("speaker_labels")
        if not isinstance(speaker_labels, dict) or len(speaker_labels) > MAX_SPEAKER_LABELS:
            raise SchemaError("Os rótulos de locutor são inválidos; o arquivo foi preservado.")
        for key, record in speaker_labels.items():
            if not _valid_id(key, reference=True) or not isinstance(record, dict):
                # Empty legacy maps are retained for sidecar compatibility;
                # non-empty unscoped maps cannot be safely projected forward.
                if isinstance(record, str):
                    raise SchemaError("Os rótulos de locutor precisam de revisão e segmento; o arquivo foi preservado.")
                raise SchemaError("Os rótulos de locutor são inválidos; o arquivo foi preservado.")
            if record.get("id", key) != key:
                raise SchemaError("O identificador de um rótulo de locutor é inconsistente; o arquivo foi preservado.")
            revision = record.get("revision") or record.get("transcript_revision")
            segment_id = record.get("segment_id")
            if revision not in transcript_segments or not _valid_id(segment_id, reference=True):
                raise SchemaError("Um rótulo de locutor referencia revisão/segmento inválido; o arquivo foi preservado.")
            segment = transcript_segments[revision].get(segment_id)
            if segment is None:
                raise SchemaError("Um rótulo de locutor referencia segmento inexistente; o arquivo foi preservado.")
            label = record.get("label")
            if not isinstance(label, str) or not label.strip() or len(label) > MAX_LABEL_CHARS:
                raise SchemaError("Os rótulos de locutor são inválidos; o arquivo foi preservado.")
            note = record.get("note", "")
            if not isinstance(note, str) or len(note.encode("utf-8")) > MAX_NOTES_BYTES:
                raise SchemaError("A nota do rótulo de locutor é inválida; o arquivo foi preservado.")
            if "track" in record:
                track = record["track"]
                if track not in {"microphone", "system"} or (
                    segment.get("track") not in {None, track}
                ):
                    raise SchemaError("A fonte do rótulo de locutor é inválida; o arquivo foi preservado.")
            if key in seen_ids:
                raise SchemaError("Há identificadores de anotação duplicados; o arquivo foi preservado.")
            seen_ids.add(key)
        for key in ("collection_ids", "tags", "people"):
            limit = MAX_COLLECTIONS if key == "collection_ids" else MAX_TAGS if key == "tags" else MAX_PEOPLE
            values = value.get(key)
            if not isinstance(values, list) or len(values) > limit or any(
                not isinstance(item, str) or not _valid_id(item, reference=True) or len(item) > MAX_ID_CHARS
                for item in values
            ) or len(set(values)) != len(values):
                raise SchemaError(f"A lista {key} da anotação é inválida; o arquivo foi preservado.")
            if key in {"tags", "people"} and any(
                item != unicodedata.normalize("NFC", item.strip()) for item in values
            ):
                raise SchemaError(f"A lista {key} não está normalizada; o arquivo foi preservado.")
        series_id = value.get("series_id")
        if series_id is not None and not _valid_id(series_id, reference=True):
            raise SchemaError("A série da anotação é inválida; o arquivo foi preservado.")
        reviewed_summary = value.get("reviewed_summary", "")
        if not isinstance(reviewed_summary, str) or len(reviewed_summary.encode("utf-8")) > MAX_NOTES_BYTES:
            raise SchemaError("O resumo revisado é inválido; o arquivo foi preservado.")
        if not isinstance(value.get("reviewed_artifacts"), dict):
            raise SchemaError("Os artefatos revisados são inválidos; o arquivo foi preservado.")
        reviewed_artifacts = value["reviewed_artifacts"]
        if len(reviewed_artifacts) > MAX_REPORTS:
            raise SchemaError("Há artefatos revisados demais; o arquivo foi preservado.")
        for report_id, artifact in reviewed_artifacts.items():
            if not _valid_id(report_id) or not isinstance(artifact, dict):
                raise SchemaError("Um artefato revisado é inválido; o arquivo foi preservado.")
            _valid_generation(artifact.get("generation"), "do artefato revisado")
            sections = artifact.get("sections")
            if not isinstance(sections, dict) or not sections or any(
                key not in REPORT_SECTIONS or not isinstance(text, str)
                for key, text in sections.items()
            ):
                raise SchemaError("As seções revisadas são inválidas; o arquivo foi preservado.")
            if not _valid_timestamp(artifact.get("updated_at")):
                raise SchemaError("A data do artefato revisado é inválida; o arquivo foi preservado.")
        active_report_id = value.get("active_report_id")
        if active_report_id is not None and not _valid_id(active_report_id):
            raise SchemaError("O relatório ativo é inválido; o arquivo foi preservado.")
        override = value.get("retention_override")
        if override is not None and not isinstance(override, dict):
            raise SchemaError("A política de retenção é inválida; o arquivo foi preservado.")
        if not _valid_timestamp(value.get("updated_at")):
            raise SchemaError("A data de atualização da anotação é inválida; o arquivo foi preservado.")
        _, size = _copy_json(value, "annotations.json")
        if size > MAX_ANNOTATIONS_BYTES:
            raise SchemaError("As anotações excedem o limite permitido; o arquivo foi preservado.")
        # Sidecars and newly-created projections must not silently refer to
        # unknown workspace IDs.  Legacy virtual annotations have empty
        # membership by construction, so this is safe for old bundles too.
        workspace = self.read_workspace()
        collection_ids = {
            item.get("id") for item in workspace.get("collections", []) if isinstance(item, dict)
        }
        if any(item not in collection_ids for item in value.get("collection_ids", [])):
            raise SchemaError("A anotação referencia uma coleção ausente; o arquivo foi preservado.")
        series_ids = {item.get("id") for item in workspace.get("series", []) if isinstance(item, dict)}
        if value.get("series_id") is not None and value["series_id"] not in series_ids:
            raise SchemaError("A anotação referencia uma série ausente; o arquivo foi preservado.")

    def _merge_session(self, metadata, annotations, *, has_sidecar):
        if not has_sidecar:
            return copy.deepcopy(metadata)
        result = copy.deepcopy(metadata)
        for key in ("title", "notes", "bookmarks", "reviewed_summary"):
            if key in annotations:
                result[key] = copy.deepcopy(annotations[key])
        result["annotation_generation"] = annotations["generation"]
        result["annotations"] = copy.deepcopy(annotations)
        return result

    def get_session(self, session_id, include_events=True):
        metadata = self.store.get(session_id, include_events=include_events)
        path = self._annotations_path(session_id)
        has_sidecar = os.path.lexists(path)
        annotations = self.read_annotations(session_id) if has_sidecar else None
        return self._merge_session(metadata, annotations, has_sidecar=has_sidecar)

    # Compatibility aliases matching MeetingStore's public vocabulary.
    get = get_session

    def update_annotations(self, session_id, patch=None, *, expected_generation=_UNSET, **fields):
        if patch is None:
            patch = {}
        if not isinstance(patch, dict):
            raise ValueError("A alteração das anotações deve ser um objeto.")
        if fields:
            patch = {**patch, **fields}
        path = self._annotations_path(session_id)
        with self._catalog_lock:
            with _writer_lock(self._session_dir(session_id)):
                with _writer_lock(path):
                    metadata = self.store.get(session_id, include_events=False)
                    current_file = self._load_versioned(
                        path, ANNOTATIONS_SCHEMA_VERSION, "annotations.json", MAX_ANNOTATIONS_BYTES
                    )
                    current = current_file or self._default_annotations(metadata)
                    self._validate_annotations(current, metadata, from_disk=bool(current_file))
                    actual = _valid_generation(current["generation"], "atual")
                    if expected_generation is not _UNSET:
                        expected = _valid_generation(expected_generation, "esperada")
                        if expected != actual:
                            raise AnnotationConflict(expected, actual)
                    merged = copy.deepcopy(current)
                    for key, value in patch.items():
                        if key in {"schema_version", "generation", "updated_at"}:
                            raise ValueError("Campos de versão das anotações são controlados pela biblioteca.")
                        merged[key] = copy.deepcopy(value)
                    merged["generation"] = actual + 1
                    merged["updated_at"] = _utc_timestamp()
                    self._validate_annotations(merged, metadata, from_disk=False)
                    _, size = _copy_json(merged, "annotations.json")
                    if size > MAX_ANNOTATIONS_BYTES:
                        raise ValueError("As anotações excedem o limite permitido.")
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    write_json_atomic(path, merged)
                    self._mirror_legacy_fields(session_id, merged)
                    result = copy.deepcopy(merged)
        self._project_after_canonical_write(session_id)
        return result

    @staticmethod
    def _generated_annotation_id(prefix):
        return f"{prefix}-{uuid.uuid4().hex}"

    def _annotation_with_generation(self, session_id, expected_generation, transform):
        """Apply one annotation transform through the existing atomic CAS seam."""
        current = self.read_annotations(session_id)
        if expected_generation is not _UNSET and current["generation"] != expected_generation:
            raise AnnotationConflict(expected_generation, current["generation"])
        updated = transform(copy.deepcopy(current))
        if not isinstance(updated, dict):
            raise ValueError("A transformação da anotação deve produzir um objeto.")
        patch = {
            key: value for key, value in updated.items()
            if key not in {"schema_version", "generation", "updated_at"}
        }
        return self.update_annotations(
            session_id, patch, expected_generation=expected_generation,
        )

    def create_speaker_label(
        self, session_id, value=None, *, expected_generation, revision=None,
        segment_id=None, label=None, note="", label_id=None, **fields,
    ):
        """Create a revision/segment-scoped manual speaker label."""
        if isinstance(value, dict):
            payload = {**value, **fields}
        else:
            payload = {**fields, "revision": revision, "segment_id": segment_id,
                       "label": label, "note": note}
            if value is not None:
                payload["revision"] = value
        record_id = payload.get("id", payload.get("label_id", label_id))
        if record_id is None:
            record_id = self._generated_annotation_id("speaker")
        payload["id"] = record_id
        revision = payload.get("revision") or payload.get("transcript_revision")
        segment_id = payload.get("segment_id")
        if not isinstance(revision, str) or not isinstance(segment_id, str):
            raise ValueError("O rótulo exige revisão e segmento de transcrição.")
        metadata, segments = self._transcript_segments(session_id, revision)
        if segment_id not in segments:
            raise ValueError("O segmento de transcrição não existe nessa revisão.")
        record = {
            "id": record_id,
            "revision": revision,
            "segment_id": segment_id,
            "label": payload.get("label"),
        }
        if "note" in payload and payload.get("note", "") != "":
            record["note"] = payload["note"]
        if "track" in payload:
            record["track"] = payload["track"]

        def add(current):
            labels = current.setdefault("speaker_labels", {})
            if record_id in labels:
                raise ValueError("Já existe um rótulo de locutor com esse identificador.")
            if any(
                item.get("revision") == revision and item.get("segment_id") == segment_id
                for item in labels.values() if isinstance(item, dict)
            ):
                raise ValueError("O segmento já possui um rótulo de locutor nessa revisão.")
            if record_id in {item.get("id") for item in current.get("highlights", [])}:
                raise ValueError("O identificador da anotação já está em uso.")
            labels[record_id] = copy.deepcopy(record)
            return current

        return self._annotation_with_generation(session_id, expected_generation, add)

    def update_speaker_label(
        self, session_id, label_id, patch=None, *, expected_generation, **fields,
    ):
        if patch is None:
            patch = {}
        if not isinstance(patch, dict):
            raise ValueError("A alteração do rótulo de locutor deve ser um objeto.")
        patch = {**patch, **fields}
        if "id" in patch or "label_id" in patch:
            raise ValueError("O identificador do rótulo não pode ser alterado.")
        allowed = {"revision", "transcript_revision", "segment_id", "label", "note", "track"}
        if set(patch) - allowed:
            raise ValueError("Há campos de rótulo de locutor não reconhecidos.")

        def edit(current):
            labels = current.get("speaker_labels", {})
            if label_id not in labels:
                raise KeyError("O rótulo de locutor não existe.")
            record = copy.deepcopy(labels[label_id])
            record.update(copy.deepcopy(patch))
            if "transcript_revision" in record:
                record["revision"] = record.pop("transcript_revision")
            revision = record.get("revision")
            segment_id = record.get("segment_id")
            metadata, segments = self._transcript_segments(session_id, revision)
            if segment_id not in segments:
                raise ValueError("O segmento de transcrição não existe nessa revisão.")
            if any(
                key != label_id and isinstance(item, dict)
                and item.get("revision") == revision and item.get("segment_id") == segment_id
                for key, item in labels.items()
            ):
                raise ValueError("O segmento já possui um rótulo de locutor nessa revisão.")
            labels[label_id] = record
            return current

        return self._annotation_with_generation(session_id, expected_generation, edit)

    edit_speaker_label = update_speaker_label

    def delete_speaker_label(self, session_id, label_id, *, expected_generation):
        def remove(current):
            labels = current.get("speaker_labels", {})
            if label_id not in labels:
                raise KeyError("O rótulo de locutor não existe.")
            del labels[label_id]
            return current

        return self._annotation_with_generation(session_id, expected_generation, remove)

    def set_speaker_label(
        self, session_id, revision, segment_id, label, *, expected_generation,
        note="", label_id=None,
    ):
        current = self.read_annotations(session_id)
        for item_id, item in current.get("speaker_labels", {}).items():
            if item.get("revision") == revision and item.get("segment_id") == segment_id:
                return self.update_speaker_label(
                    session_id, item_id, {"label": label, "note": note},
                    expected_generation=expected_generation,
                )
        return self.create_speaker_label(
            session_id,
            {"id": label_id, "revision": revision, "segment_id": segment_id,
             "label": label, "note": note} if label_id else {
                 "revision": revision, "segment_id": segment_id,
                 "label": label, "note": note,
             },
            expected_generation=expected_generation,
        )

    def add_speaker_label(
        self, session_id, revision, segment_id, label, *, expected_generation,
        note="", label_id=None,
    ):
        return self.create_speaker_label(
            session_id,
            {"id": label_id, "revision": revision, "segment_id": segment_id,
             "label": label, "note": note} if label_id else {
                 "revision": revision, "segment_id": segment_id,
                 "label": label, "note": note,
             },
            expected_generation=expected_generation,
        )

    save_speaker_label = add_speaker_label
    remove_speaker_label = delete_speaker_label

    def create_highlight(
        self, session_id, value=None, *, expected_generation, revision=None,
        start=None, end=None, track=None, segment_ids=None, label="", note="",
        highlight_id=None, **fields,
    ):
        """Create a bounded, revision-scoped highlight without editing JSONL."""
        if isinstance(value, dict):
            payload = {**value, **fields}
        else:
            payload = {**fields, "revision": revision, "start": start, "end": end,
                       "track": track, "segment_ids": segment_ids, "label": label,
                       "note": note}
            if value is not None:
                payload["revision"] = value
        record_id = payload.get("id", payload.get("highlight_id", highlight_id))
        if record_id is None:
            record_id = self._generated_annotation_id("highlight")
        payload["id"] = record_id
        revision = payload.get("revision") or payload.get("transcript_revision")
        if not isinstance(revision, str):
            raise ValueError("O destaque exige uma revisão de transcrição.")
        metadata, segments = self._transcript_segments(session_id, revision)
        record = {
            "id": record_id,
            "revision": revision,
            "start": payload.get("start"),
            "end": payload.get("end"),
            "track": payload.get("track"),
            "segment_ids": copy.deepcopy(payload.get("segment_ids", payload.get("segments"))),
            "label": payload.get("label", ""),
            "note": payload.get("note", ""),
        }
        if "transcript_revision" in payload and "revision" not in payload:
            record["revision"] = payload["transcript_revision"]
        # Validate before touching the sidecar so malformed requests cannot
        # create an empty generation or alter transcript source files.
        self._validate_highlight_record(record, metadata, {revision: segments}, set())

        def add(current):
            if any(item.get("id") == record_id for item in current.get("highlights", [])):
                raise ValueError("Já existe um destaque com esse identificador.")
            if record_id in current.get("speaker_labels", {}):
                raise ValueError("O identificador da anotação já está em uso.")
            current.setdefault("highlights", []).append(copy.deepcopy(record))
            return current

        return self._annotation_with_generation(session_id, expected_generation, add)

    def update_highlight(
        self, session_id, highlight_id, patch=None, *, expected_generation, **fields,
    ):
        if patch is None:
            patch = {}
        if not isinstance(patch, dict):
            raise ValueError("A alteração do destaque deve ser um objeto.")
        patch = {**patch, **fields}
        if "id" in patch or "highlight_id" in patch:
            raise ValueError("O identificador do destaque não pode ser alterado.")
        allowed = {"revision", "transcript_revision", "start", "end", "track",
                   "segment_ids", "segments", "label", "note"}
        if set(patch) - allowed:
            raise ValueError("Há campos de destaque não reconhecidos.")

        def edit(current):
            highlights = current.get("highlights", [])
            for index, item in enumerate(highlights):
                if item.get("id") != highlight_id:
                    continue
                record = copy.deepcopy(item)
                record.update(copy.deepcopy(patch))
                if "transcript_revision" in record:
                    record["revision"] = record.pop("transcript_revision")
                revision = record.get("revision")
                metadata, segments = self._transcript_segments(session_id, revision)
                self._validate_highlight_record(record, metadata, {revision: segments}, set())
                highlights[index] = record
                return current
            raise KeyError("O destaque não existe.")

        return self._annotation_with_generation(session_id, expected_generation, edit)

    edit_highlight = update_highlight

    def delete_highlight(self, session_id, highlight_id, *, expected_generation):
        def remove(current):
            highlights = current.get("highlights", [])
            for index, item in enumerate(highlights):
                if item.get("id") == highlight_id:
                    del highlights[index]
                    return current
            raise KeyError("O destaque não existe.")

        return self._annotation_with_generation(session_id, expected_generation, remove)

    def add_highlight(
        self, session_id, revision, start, end, track, segment_ids, *,
        expected_generation, label="", note="", highlight_id=None,
    ):
        value = {
            "revision": revision, "start": start, "end": end, "track": track,
            "segment_ids": segment_ids, "label": label, "note": note,
        }
        if highlight_id is not None:
            value["id"] = highlight_id
        return self.create_highlight(
            session_id, value, expected_generation=expected_generation,
        )

    save_highlight = add_highlight
    remove_highlight = delete_highlight

    def list_highlights(self, session_id, *, revision=None, active_only=False):
        return self.read_annotations(
            session_id, revision=revision, active_only=active_only,
        ).get("highlights", [])

    def list_speaker_labels(self, session_id, *, revision=None, active_only=False):
        return list(self.read_annotations(
            session_id, revision=revision, active_only=active_only,
        ).get("speaker_labels", {}).values())

    def active_annotations(self, session_id):
        return self.read_annotations(session_id, active_only=True)

    def _mirror_legacy_fields(self, session_id, annotations):
        """Keep old direct ``MeetingStore`` readers useful after a sidecar edit.

        The sidecar is committed first and remains authoritative.  A mirror
        failure is intentionally best effort: old builds may show stale fields,
        while this build reads the committed sidecar.
        """
        fields = {
            key: copy.deepcopy(annotations[key])
            for key in ("title", "notes", "bookmarks", "reviewed_summary")
            if key in annotations
        }
        if not fields:
            return
        try:
            self.store.update(session_id, **fields)
        except Exception:
            return

    def update(self, session_id, **fields):
        expected = fields.pop("expected_generation", _UNSET)
        allowed = {"title", "notes", "bookmarks", "reviewed_summary", "highlights",
                   "speaker_labels", "collection_ids", "tags", "people", "series_id",
                   "reviewed_artifacts", "active_report_id", "retention_override"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError("Há campos de reunião não reconhecidos.")
        self.update_annotations(session_id, fields, expected_generation=expected)
        return True

    @staticmethod
    def _normalized_labels(values, label, limit):
        if not isinstance(values, list) or len(values) > limit:
            raise ValueError(f"A lista {label} é inválida.")
        result = []
        seen = set()
        for value in values:
            if not isinstance(value, str):
                raise ValueError(f"A lista {label} é inválida.")
            normalized = unicodedata.normalize("NFC", value.strip())
            if not normalized or len(normalized) > MAX_ID_CHARS or not _valid_id(normalized, reference=True):
                raise ValueError(f"A lista {label} é inválida.")
            folded = normalized.casefold()
            if folded in seen:
                continue
            seen.add(folded)
            result.append(normalized)
        return result

    def assign_organization(
        self, session_id, *, collection_ids=None, tags=None, people=None,
        series_id=_UNSET, expected_generation,
    ):
        patch = {}
        if collection_ids is not None:
            patch["collection_ids"] = self._normalized_labels(
                collection_ids, "de coleções", MAX_COLLECTIONS,
            )
        if tags is not None:
            patch["tags"] = self._normalized_labels(tags, "de tags", MAX_TAGS)
        if people is not None:
            patch["people"] = self._normalized_labels(people, "de pessoas", MAX_PEOPLE)
        if series_id is not _UNSET:
            if series_id is not None and not _valid_id(series_id):
                raise ValueError("A série é inválida.")
            patch["series_id"] = series_id
        if not patch:
            raise ValueError("Nenhuma organização foi alterada.")
        return self.update_annotations(
            session_id, patch, expected_generation=expected_generation,
        )

    def _canonical_session_ids(self):
        try:
            with os.scandir(self.meetings_root) as entries:
                values = [
                    entry.name for entry in entries
                    if _valid_id(entry.name)
                    and entry.is_dir(follow_symlinks=False)
                    and not _is_link_or_junction(os.path.join(self.meetings_root, entry.name))
                ]
        except OSError as error:
            raise SchemaError("A biblioteca não pôde ser enumerada.") from error
        return sorted(values)

    def preview_collection_delete(self, collection_id):
        if not _valid_id(collection_id):
            raise ValueError("A coleção é inválida.")
        workspace = self.read_workspace()
        if not any(item.get("id") == collection_id for item in workspace["collections"]):
            raise KeyError("A coleção não existe.")
        affected = []
        for session_id in self._canonical_session_ids():
            annotations = self.read_annotations(session_id)
            if collection_id in annotations.get("collection_ids", []):
                affected.append(session_id)
        return {
            "collection_id": collection_id,
            "workspace_generation": workspace["generation"],
            "session_ids": affected,
        }

    def delete_collection(self, collection_id, *, expected_generation, confirmed_session_ids):
        preview = self.preview_collection_delete(collection_id)
        if preview["workspace_generation"] != expected_generation:
            raise WorkspaceConflict(expected_generation, preview["workspace_generation"])
        if sorted(set(confirmed_session_ids)) != preview["session_ids"]:
            raise ValueError("A confirmação não corresponde à prévia atual da coleção.")
        originals = {}
        updated = []
        try:
            for session_id in preview["session_ids"]:
                annotations = self.read_annotations(session_id)
                originals[session_id] = annotations
                memberships = [
                    item for item in annotations["collection_ids"] if item != collection_id
                ]
                self.update_annotations(
                    session_id,
                    {"collection_ids": memberships},
                    expected_generation=annotations["generation"],
                )
                updated.append(session_id)
            workspace = self.read_workspace()
            collections = [
                item for item in workspace["collections"] if item.get("id") != collection_id
            ]
            self.update_workspace(
                {"collections": collections}, expected_generation=expected_generation,
            )
        except Exception:
            for session_id in reversed(updated):
                original = originals[session_id]
                current = self.read_annotations(session_id)
                restore = {
                    key: copy.deepcopy(value)
                    for key, value in original.items()
                    if key not in {"schema_version", "generation", "updated_at"}
                }
                self.update_annotations(
                    session_id, restore, expected_generation=current["generation"],
                )
            raise
        return {"collection_id": collection_id, "removed_from": preview["session_ids"]}

    # -- Catalog projection and compatibility fallback ------------------

    def list_sessions(self, offset=0, limit=50, query="", status=""):
        try:
            if self.index_state == "ready" and not self._index_stale:
                return self.index.list_sessions(offset=offset, limit=limit, query=query, status=status)
        except Exception:
            self._mark_index_stale()
        sessions = self.store.list_sessions(offset=offset, limit=limit, query=query, status=status)
        projected = []
        for item in sessions:
            session_id = item.get("id") if isinstance(item, dict) else None
            if not isinstance(session_id, str) or not self.has_annotation_sidecar(session_id):
                projected.append(item)
                continue
            annotations = self.read_annotations(session_id)
            value = copy.deepcopy(item)
            for key in ("title", "notes", "bookmarks", "reviewed_summary"):
                if key in annotations:
                    value[key] = copy.deepcopy(annotations[key])
            value["annotation_generation"] = annotations["generation"]
            projected.append(value)
        return projected

    def get_transcript(self, session_id, revision=None):
        return self.store.get_transcript(session_id, revision)

    def export(self, session_id, path, format="markdown", cancel_event=None):
        """Export through the sidecar-aware metadata view."""
        from meeting_files import export_meeting

        return export_meeting(
            _LibraryStoreView(self), session_id, path, format, cancel_event=cancel_event
        )

    # -- Generated report revisions ------------------------------------

    def _reports_dir(self, session_id, *, create=False):
        session_dir = self._session_dir(session_id)
        result = os.path.join(session_dir, REPORTS_DIRECTORY)
        if os.path.lexists(result) and _is_link_or_junction(result):
            raise PathSafetyError("A pasta de relatórios não pode ser um link ou junction.")
        if not _commonpath_is(os.path.realpath(session_dir), os.path.realpath(result)):
            raise PathSafetyError("A pasta de relatórios aponta para fora da reunião.")
        if create:
            os.makedirs(result, exist_ok=True)
        return result

    def _report_path(self, session_id, report_id, *, create_directory=False):
        if not _valid_id(report_id):
            raise ValueError("O identificador do relatório é inválido.")
        directory = self._reports_dir(session_id, create=create_directory)
        path = os.path.join(directory, f"{report_id}.json")
        if os.path.lexists(path) and _is_link_or_junction(path):
            raise PathSafetyError("O relatório não pode ser um link ou junction.")
        if not _commonpath_is(os.path.realpath(directory), os.path.realpath(path)):
            raise PathSafetyError("O relatório aponta para fora da reunião.")
        return path

    @staticmethod
    def _report_citations(generated):
        citations = []

        def visit(value, key=None):
            if key in {"citations", "segment_ids"}:
                if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                    raise SchemaError("As citações do relatório são inválidas.")
                citations.extend(value)
                return
            if isinstance(value, dict):
                for child_key, child in value.items():
                    visit(child, child_key)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(generated)
        return citations

    def _validate_report(self, session_id, envelope):
        if not isinstance(envelope, dict):
            raise SchemaError("O relatório deve ser um objeto.")
        allowed = {
            "schema_version", "id", "report_id", "kind", "profile_id",
            "profile_version", "session_id", "transcript_revision", "model",
            "generated", "payload", "status", "created_at", "completed_at",
        }
        if set(envelope) - allowed:
            raise SchemaError("O relatório contém campos não reconhecidos.")
        if envelope.get("schema_version") != REPORT_SCHEMA_VERSION:
            raise SchemaError("A versão do relatório é incompatível.")
        report_id = envelope.get("id", envelope.get("report_id"))
        if not _valid_id(report_id):
            raise SchemaError("O identificador do relatório é inválido.")
        if envelope.get("session_id") != session_id:
            raise SchemaError("O relatório referencia outra reunião.")
        kind = envelope.get("kind")
        if kind not in {"report", "qa"}:
            raise SchemaError("O tipo do relatório é inválido.")
        if not _valid_id(envelope.get("profile_id")):
            raise SchemaError("O perfil do relatório é inválido.")
        profile_version = envelope.get("profile_version")
        if isinstance(profile_version, bool) or not isinstance(profile_version, int) or profile_version < 1:
            raise SchemaError("A versão do perfil é inválida.")
        metadata = self.store.get(session_id, include_events=False)
        revisions = {
            item.get("id") for item in metadata.get("revisions", []) if isinstance(item, dict)
        }
        revision_id = envelope.get("transcript_revision")
        if not isinstance(revision_id, str) or revision_id not in revisions:
            raise SchemaError("O relatório referencia uma revisão inexistente.")
        model = envelope.get("model")
        if not isinstance(model, dict) or set(model) - {"id", "sha256", "runtime", "context_limit"}:
            raise SchemaError("A proveniência do modelo é inválida.")
        if not _valid_id(model.get("id"), reference=True):
            raise SchemaError("O modelo do relatório é inválido.")
        digest = model.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            raise SchemaError("O hash do modelo é inválido.")
        if not isinstance(model.get("runtime"), str) or not model["runtime"]:
            raise SchemaError("O runtime do relatório é inválido.")
        generated = envelope.get("generated", envelope.get("payload"))
        if not isinstance(generated, dict) or not generated or any(
            key not in REPORT_SECTIONS for key in generated
        ):
            raise SchemaError("As seções geradas são inválidas.")
        if not _valid_timestamp(envelope.get("created_at")):
            raise SchemaError("A data do relatório é inválida.")
        segments = {
            item.get("id")
            for item in self.store.get_transcript(session_id, revision_id)
            if isinstance(item, dict)
        }
        if any(item not in segments for item in self._report_citations(generated)):
            raise SchemaError("O relatório contém citações que não existem na revisão.")
        normalized = copy.deepcopy(envelope)
        normalized["id"] = report_id
        normalized.pop("report_id", None)
        normalized["generated"] = normalized.pop("payload", generated)
        _, size = _copy_json(normalized, "relatório")
        if size > MAX_REPORT_BYTES:
            raise SchemaError("O relatório excede o limite permitido.")
        return normalized

    def save_report(self, session_id, envelope):
        normalized = self._validate_report(session_id, envelope)
        path = self._report_path(session_id, normalized["id"], create_directory=True)
        with _writer_lock(path):
            if os.path.lexists(path):
                raise FileExistsError("Uma revisão de relatório com este identificador já existe.")
            write_json_atomic(path, normalized)
        self._project_after_canonical_write(session_id)
        return copy.deepcopy(normalized)

    def _read_report_files(self, session_id):
        directory = self._reports_dir(session_id)
        if not os.path.isdir(directory):
            return []
        reports = []
        with os.scandir(directory) as entries:
            names = sorted(
                entry.name for entry in entries
                if entry.is_file(follow_symlinks=False) and entry.name.endswith(".json")
            )
        if len(names) > MAX_REPORTS:
            raise SchemaError("Há relatórios demais nesta reunião.")
        for name in names:
            report_id = name[:-5]
            path = self._report_path(session_id, report_id)
            value = self._load_versioned(
                path, REPORT_SCHEMA_VERSION, f"reports/{name}", MAX_REPORT_BYTES,
            )
            reports.append(self._validate_report(session_id, value))
        reports.sort(key=lambda item: (item.get("created_at", ""), item["id"]))
        return reports

    def list_reports(self, session_id, *, include_legacy=True):
        reports = self._read_report_files(session_id)
        annotations = self.read_annotations(session_id)
        reviewed = annotations.get("reviewed_artifacts", {})
        result = []
        if include_legacy:
            metadata = self.store.get(session_id, include_events=False)
            summary = metadata.get("summary")
            reviewed_summary = annotations.get("reviewed_summary")
            if summary or reviewed_summary:
                result.append({
                    "schema_version": 0,
                    "id": "legacy-summary",
                    "kind": "legacy-summary",
                    "virtual": True,
                    "generated": copy.deepcopy(summary),
                    "reviewed_artifact": reviewed_summary or None,
                    "created_at": metadata.get("updated_at") or metadata.get("created_at"),
                })
        for report in reports:
            value = copy.deepcopy(report)
            if report["id"] in reviewed:
                value["reviewed_artifact"] = copy.deepcopy(reviewed[report["id"]])
            result.append(value)
        return result

    def get_report(self, session_id, report_id):
        if report_id == "legacy-summary":
            for report in self.list_reports(session_id):
                if report["id"] == report_id:
                    return report
            raise FileNotFoundError("O resumo legado não existe.")
        path = self._report_path(session_id, report_id)
        value = self._load_versioned(
            path, REPORT_SCHEMA_VERSION, f"reports/{report_id}.json", MAX_REPORT_BYTES,
        )
        if value is None:
            raise FileNotFoundError("O relatório não existe.")
        report = self._validate_report(session_id, value)
        reviewed = self.read_annotations(session_id).get("reviewed_artifacts", {})
        if report_id in reviewed:
            report["reviewed_artifact"] = copy.deepcopy(reviewed[report_id])
        return report

    def review_report(self, session_id, report_id, sections, *, expected_generation):
        self.get_report(session_id, report_id)
        if report_id == "legacy-summary":
            raise ValueError("Regenere o resumo legado antes de revisar seções estruturadas.")
        if not isinstance(sections, dict) or not sections or any(
            key not in REPORT_SECTIONS or not isinstance(text, str)
            for key, text in sections.items()
        ):
            raise ValueError("As seções revisadas são inválidas.")
        annotations = self.read_annotations(session_id)
        reviewed = copy.deepcopy(annotations.get("reviewed_artifacts", {}))
        current = reviewed.get(report_id)
        actual = current.get("generation", 0) if isinstance(current, dict) else 0
        expected = _valid_generation(expected_generation, "esperada do artefato")
        if expected != actual:
            raise AnnotationConflict(expected, actual)
        artifact = {
            "generation": actual + 1,
            "sections": copy.deepcopy(sections),
            "updated_at": _utc_timestamp(),
        }
        reviewed[report_id] = artifact
        self.update_annotations(
            session_id,
            {"reviewed_artifacts": reviewed, "active_report_id": report_id},
            expected_generation=annotations["generation"],
        )
        return copy.deepcopy(artifact)

    def delete(self, session_id):
        with self._catalog_lock:
            with _writer_lock(self._session_dir(session_id)):
                result = self.store.delete(session_id)
                try:
                    removed = self.index.remove_session(session_id)
                    if not removed:
                        self._mark_index_stale_with_reason("canonical deletion was not projected")
                except Exception:
                    # The canonical deletion wins.  Keep all indexed reads on
                    # the canonical fallback until the disposable projection
                    # is rebuilt, so removed plaintext cannot be surfaced.
                    self._mark_index_stale_with_reason("canonical deletion was not projected")
                return result

    delete_session = delete

    @staticmethod
    def _index_annotation_view(metadata, annotations):
        """Keep canonical history intact while projecting only active overlays."""
        if annotations is None:
            return None
        active = MeetingLibrary._active_revision_id(metadata)
        return MeetingLibrary._filter_annotation_revision(annotations, active) if active else copy.deepcopy(annotations)

    @staticmethod
    def _index_transcripts(metadata, transcripts, annotations):
        if annotations is None:
            return transcripts
        active = MeetingLibrary._active_revision_id(metadata)
        if not active:
            return transcripts
        labels = annotations.get("speaker_labels", {})
        by_segment = {
            item.get("segment_id"): item.get("label")
            for item in labels.values() if isinstance(item, dict)
            and item.get("revision") == active and item.get("segment_id")
        }
        projected = copy.deepcopy(transcripts)
        for segment in projected.get(active, []):
            if segment.get("id") in by_segment:
                segment["speaker"] = by_segment[segment["id"]]
        return projected

    def _project_after_canonical_write(self, session_id):
        with self._catalog_lock:
            with _writer_lock(self._session_dir(session_id)):
                try:
                    path = self._annotations_path(session_id)
                    metadata = self.store.get(session_id, include_events=False)
                    annotations = self.read_annotations(session_id) if os.path.lexists(path) else None
                    projected_annotations = self._index_annotation_view(metadata, annotations)
                    reports = self._read_report_files(session_id)
                    if annotations and annotations.get("speaker_labels") and hasattr(self.index, "index_session"):
                        transcripts = {
                            revision.get("id"): list(self.store.get_transcript(session_id, revision.get("id")))
                            for revision in metadata.get("revisions", [])
                            if isinstance(revision, dict) and isinstance(revision.get("id"), str)
                        }
                        transcripts = self._index_transcripts(metadata, transcripts, annotations)
                        result = bool(self.index.index_session(
                            metadata, annotations=projected_annotations,
                            transcripts=transcripts, reports=reports,
                        ))
                    else:
                        result = bool(self.index.index_store_session(
                            self.store, session_id, annotations=projected_annotations,
                            reports=reports,
                        ))
                    if result:
                        self._index_stale = False
                    return result
                except Exception:
                    self._mark_index_stale()
                    return False

    def _mark_index_stale(self):
        return self._mark_index_stale_with_reason("canonical data changed")

    def _mark_index_stale_with_reason(self, reason):
        self._index_stale = True
        if self._index is None and not os.path.lexists(os.path.join(self.home_root, "library.sqlite")):
            return
        try:
            self.index.mark_stale(reason)
        except Exception:
            return

    def project_session(self, session_id):
        return self._project_after_canonical_write(session_id)

    # Named completion hooks keep processing modules independent of SQLite.
    on_session_finalized = project_session
    on_transcription_complete = project_session
    on_report_complete = project_session
    finalize_session = project_session

    def queue_index_session(self, session_id):
        """Schedule projection work without opening SQLite on the capture loop."""
        index_path = os.path.join(self.home_root, "library.sqlite")
        # A missing catalog does not need to be created from the capture
        # finalizer.  Canonical reads remain the safe fallback until an
        # explicit rebuild or user-facing indexed operation creates it.
        if self._index_closed:
            return None
        if (
            not os.path.lexists(index_path)
            and (self._index is None or self._index.state == "unavailable")
        ):
            return None
        with self._index_lock:
            existing = self._index_workers_by_session.get(session_id)
            if existing is not None and existing.is_alive():
                self._index_pending.add(session_id)
                return existing
            self._index_pending.discard(session_id)

            def run():
                while True:
                    self.project_session(session_id)
                    with self._index_lock:
                        if session_id not in self._index_pending:
                            self._index_workers.discard(threading.current_thread())
                            self._index_workers_by_session.pop(session_id, None)
                            return
                        self._index_pending.discard(session_id)

            worker = threading.Thread(target=run, daemon=True, name="MeetingIndexProjection")
            self._index_workers.add(worker)
            self._index_workers_by_session[session_id] = worker
            worker.start()
            return worker

    schedule_index_session = queue_index_session

    def reconcile(self, cancel_event=None, progress=None):
        """Rebuild/reconcile the disposable index from canonical bundles."""
        with self._catalog_lock:
            sessions = []
            try:
                with os.scandir(self.meetings_root) as entries:
                    session_ids = []
                    for entry in entries:
                        if not _valid_id(entry.name):
                            continue
                        path = os.path.join(self.meetings_root, entry.name)
                        if _is_link_or_junction(path) or not entry.is_dir(follow_symlinks=False):
                            continue
                        session_ids.append(entry.name)
                    session_ids.sort()
            except OSError as error:
                self._mark_index_stale_with_reason("meeting directory scan failed")
                raise SchemaError("A biblioteca não pôde ser lida completamente; o índice não foi publicado.") from error
            for session_id in session_ids:
                if cancel_event is not None and cancel_event.is_set():
                    break
                try:
                    metadata = self.store.get(session_id, include_events=False)
                except (OSError, ValueError) as error:
                    self._mark_index_stale_with_reason("meeting metadata is unreadable")
                    raise SchemaError("Os metadados da reunião não puderam ser lidos; o índice não foi publicado.") from error
                annotations_path = self._annotations_path(session_id)
                # A malformed/future sidecar is actionable corruption, not an
                # invitation to silently rebuild from legacy metadata.
                annotations = self.read_annotations(session_id) if os.path.lexists(annotations_path) else None
                transcripts = {}
                revisions = metadata.get("revisions", [])
                if not isinstance(revisions, list):
                    self._mark_index_stale_with_reason("meeting revisions are malformed")
                    raise SchemaError("As revisões da reunião são inválidas; o índice não foi publicado.")
                for revision in revisions:
                    if not isinstance(revision, dict) or not isinstance(revision.get("id"), str):
                        self._mark_index_stale_with_reason("meeting revision identity is malformed")
                        raise SchemaError("A identidade da revisão é inválida; o índice não foi publicado.")
                    revision_id = revision["id"]
                    values = list(self.store.get_transcript(session_id, revision_id))
                    expected = revision.get("segments")
                    if isinstance(expected, int) and not isinstance(expected, bool) and expected != len(values):
                        self._mark_index_stale_with_reason(
                            "transcript corruption prevents a complete rebuild"
                        )
                        raise SchemaError("A transcrição está incompleta; o índice não foi marcado como íntegro.")
                    transcripts[revision_id] = values
                projected_annotations = self._index_annotation_view(metadata, annotations)
                projected_transcripts = self._index_transcripts(metadata, transcripts, annotations)
                sessions.append((metadata, projected_annotations, projected_transcripts))
            if cancel_event is not None and cancel_event.is_set():
                # Let MeetingIndex publish its explicit cancellation state.
                sessions = []
            result = self.index.rebuild(sessions, cancel_event=cancel_event, progress=progress)
            if result.get("state") == "ready":
                self._index_stale = False
            return result

    def shutdown(self, timeout=12):
        """Join all queued projection workers before their bundle roots close."""
        with self._index_lock:
            self._index_closed = True
            workers = list(self._index_workers)
        for worker in workers:
            if worker is threading.current_thread():
                continue
            worker.join(timeout)
            if worker.is_alive():
                raise RuntimeError("Uma projeção do índice de reuniões ainda está encerrando.")


__all__ = [
    "ANNOTATIONS_FILENAME",
    "ANNOTATIONS_SCHEMA_VERSION",
    "AnnotationConflict",
    "MeetingLibrary",
    "MeetingLibraryError",
    "PathSafetyError",
    "SchemaError",
    "UnsupportedSchemaError",
    "WORKSPACE_FILENAME",
    "WORKSPACE_SCHEMA_VERSION",
    "WorkspaceConflict",
]
