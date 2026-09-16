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

from meeting_store import MeetingStore
from snippet_utils import write_json_atomic


WORKSPACE_SCHEMA_VERSION = 1
ANNOTATIONS_SCHEMA_VERSION = 1
WORKSPACE_FILENAME = "workspace.json"
ANNOTATIONS_FILENAME = "annotations.json"
MAX_WORKSPACE_BYTES = 2 * 1024 * 1024
MAX_ANNOTATIONS_BYTES = 2 * 1024 * 1024
MAX_TITLE_CHARS = 400
MAX_NOTES_BYTES = 1024 * 1024
MAX_BOOKMARKS = 10_000
MAX_HIGHLIGHTS = 10_000
MAX_LABEL_CHARS = 256
MAX_TAGS = 512
MAX_PEOPLE = 512
MAX_COLLECTIONS = 1024
MAX_SPEAKER_LABELS = 10_000
MAX_ID_CHARS = 128
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
            for item in value[key]:
                if not isinstance(item, dict) or not _valid_id(item.get("id")):
                    raise SchemaError(f"A definição {key} do workspace é inválida; o arquivo foi preservado.")
                if item["id"] in seen:
                    raise SchemaError(f"A definição {key} do workspace é duplicada; o arquivo foi preservado.")
                seen.add(item["id"])
                if not isinstance(item.get("name", ""), str) or len(item.get("name", "")) > MAX_LABEL_CHARS:
                    raise SchemaError(f"O nome de {key} do workspace é inválido; o arquivo foi preservado.")
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
            "retention_override": None,
            "updated_at": metadata.get("updated_at") if _valid_timestamp(metadata.get("updated_at")) else _utc_timestamp(),
        }

    def _annotations_path(self, session_id):
        return self._canonical_path(session_id, ANNOTATIONS_FILENAME)

    def has_annotation_sidecar(self, session_id):
        """Return whether the versioned annotation file exists on disk."""
        return os.path.lexists(self._annotations_path(session_id))

    def read_annotations(self, session_id):
        metadata = self.store.get(session_id, include_events=False)
        path = self._annotations_path(session_id)
        value = self._load_versioned(
            path, ANNOTATIONS_SCHEMA_VERSION, "annotations.json", MAX_ANNOTATIONS_BYTES
        )
        if value is None:
            value = self._default_annotations(metadata)
            self._validate_annotations(value, metadata, from_disk=False)
            return value
        self._validate_annotations(value, metadata, from_disk=True)
        return copy.deepcopy(value)

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
        highlights = value.get("highlights")
        if not isinstance(highlights, list) or len(highlights) > MAX_HIGHLIGHTS:
            raise SchemaError("Os destaques da anotação são inválidos; o arquivo foi preservado.")
        seen_highlights = set()
        revision_ids = {item.get("id") for item in metadata.get("revisions", []) if isinstance(item, dict)}
        for highlight in highlights:
            if not isinstance(highlight, dict) or not _valid_id(highlight.get("id"), reference=True):
                raise SchemaError("Um destaque tem identificador inválido; o arquivo foi preservado.")
            if highlight["id"] in seen_highlights:
                raise SchemaError("Há destaques duplicados; o arquivo foi preservado.")
            seen_highlights.add(highlight["id"])
            revision = highlight.get("revision") or highlight.get("transcript_revision")
            if not isinstance(revision, str) or not _valid_id(revision, reference=True) or revision not in revision_ids:
                raise SchemaError("Um destaque referencia uma revisão inexistente; o arquivo foi preservado.")
            start, end = highlight.get("start"), highlight.get("end")
            if not _finite_number(start) or not _finite_number(end) or float(start) < 0 or float(end) < float(start):
                raise SchemaError("O intervalo de um destaque é inválido; o arquivo foi preservado.")
            if "track" in highlight and highlight["track"] not in {"microphone", "system"}:
                raise SchemaError("A fonte de um destaque é inválida; o arquivo foi preservado.")
            segment_ids = highlight.get("segment_ids", highlight.get("segments", []))
            if not isinstance(segment_ids, list) or len(segment_ids) > 512 or any(
                not _valid_id(item, reference=True) for item in segment_ids
            ):
                raise SchemaError("Os segmentos de um destaque são inválidos; o arquivo foi preservado.")
            if "label" in highlight and (not isinstance(highlight["label"], str)
                                           or len(highlight["label"]) > MAX_LABEL_CHARS):
                raise SchemaError("O rótulo de um destaque é inválido; o arquivo foi preservado.")
        speaker_labels = value.get("speaker_labels")
        if not isinstance(speaker_labels, dict) or len(speaker_labels) > MAX_SPEAKER_LABELS:
            raise SchemaError("Os rótulos de locutor são inválidos; o arquivo foi preservado.")
        for key, label in speaker_labels.items():
            if not _valid_id(key, reference=True) or not isinstance(label, str) or len(label) > MAX_LABEL_CHARS:
                raise SchemaError("Os rótulos de locutor são inválidos; o arquivo foi preservado.")
        for key in ("collection_ids", "tags", "people"):
            limit = MAX_COLLECTIONS if key == "collection_ids" else MAX_TAGS if key == "tags" else MAX_PEOPLE
            values = value.get(key)
            if not isinstance(values, list) or len(values) > limit or any(
                not isinstance(item, str) or not _valid_id(item, reference=True) or len(item) > MAX_ID_CHARS
                for item in values
            ) or len(set(values)) != len(values):
                raise SchemaError(f"A lista {key} da anotação é inválida; o arquivo foi preservado.")
        series_id = value.get("series_id")
        if series_id is not None and not _valid_id(series_id, reference=True):
            raise SchemaError("A série da anotação é inválida; o arquivo foi preservado.")
        reviewed_summary = value.get("reviewed_summary", "")
        if not isinstance(reviewed_summary, str) or len(reviewed_summary.encode("utf-8")) > MAX_NOTES_BYTES:
            raise SchemaError("O resumo revisado é inválido; o arquivo foi preservado.")
        if not isinstance(value.get("reviewed_artifacts"), dict):
            raise SchemaError("Os artefatos revisados são inválidos; o arquivo foi preservado.")
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
                   "reviewed_artifacts", "retention_override"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError("Há campos de reunião não reconhecidos.")
        self.update_annotations(session_id, fields, expected_generation=expected)
        return True

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

    def _project_after_canonical_write(self, session_id):
        with self._catalog_lock:
            with _writer_lock(self._session_dir(session_id)):
                try:
                    path = self._annotations_path(session_id)
                    annotations = self.read_annotations(session_id) if os.path.lexists(path) else None
                    result = bool(self.index.index_store_session(
                        self.store, session_id, annotations=annotations,
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
                sessions.append((metadata, annotations, transcripts))
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
