"""Canonical meeting-library seam above :mod:`meeting_store`.

``MeetingStore`` remains the capture/recovery authority.  This module owns the
additive human state in ``annotations.json`` and ``workspace.json`` and treats
the SQLite catalog as disposable.  It deliberately keeps the public methods
small so GUI/controller code never needs to know which canonical file or
projection supplies a value.
"""

import contextlib
import copy
import base64
from datetime import datetime
import hashlib
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
REPORT_HISTORY_LIMIT = 500
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
MAX_BATCH_ASSIGNMENTS = 500
MAX_LIBRARY_CURSOR_CHARS = 2048
MAX_LIBRARY_BACKSTACK = 32
MAX_REPORT_CITATIONS = 16
MAX_ID_CHARS = 128
LOCK_TIMEOUT_SECONDS = 5.0
LOCK_POLL_SECONDS = 0.05
DEFAULT_TRASH_RETENTION_DAYS = 30.0
SUPPORTED_NOTICE_LANGUAGES = frozenset(("pt-BR", "en", "en-US"))
SUPPORTED_QA_MODES = frozenset(("memory_only", "explicit_save"))
REPORT_SECTIONS = frozenset({
    "summary", "decisions", "action_items", "open_questions", "risks",
    "objections", "feedback", "follow_up_email", "answer",
})
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_REFERENCE_RE = re.compile(r"^[^/\\\x00]{1,128}$")
_UNSET = object()


def _library_cursor_encode(value):
    """Encode one bounded canonical keyset cursor.

    MeetingIndex owns its own revision-bound cursor format.  Canonical fallback
    cursors intentionally carry only a filter digest and the last visible
    ordering key, so a fallback page never depends on an offset into a mutable
    directory listing.
    """
    try:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        encoded = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
    except (TypeError, ValueError, UnicodeError) as error:
        raise ValueError("O cursor da biblioteca é inválido.") from error
    if not encoded or len(encoded) > MAX_LIBRARY_CURSOR_CHARS:
        raise ValueError("O cursor da biblioteca é grande demais.")
    return "c1." + encoded


def _library_cursor_decode(value):
    if value is None:
        return None
    if not isinstance(value, str) or not value.startswith("c1."):
        return None
    encoded = value[3:]
    if not encoded or len(encoded) > MAX_LIBRARY_CURSOR_CHARS:
        raise ValueError("O cursor da biblioteca é inválido.")
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        result = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError, base64.binascii.Error) as error:
        raise ValueError("O cursor da biblioteca é inválido.") from error
    if not isinstance(result, dict) or result.get("kind") != "canonical" or result.get("v") != 1:
        raise ValueError("O cursor da biblioteca é incompatível.")
    position = result.get("position")
    if (not isinstance(position, list) or len(position) != 2
            or any(not isinstance(item, str) for item in position)):
        raise ValueError("O cursor da biblioteca é inválido.")
    return result


class BatchOrganizationRollbackError(RuntimeError):
    """A batch failed and one or more compensating writes also failed."""

    def __init__(self, original_error, result):
        self.original_error = original_error
        self.result = copy.deepcopy(result)
        failures = ", ".join(item["session_id"] for item in result["rollback_failures"])
        super().__init__(
            "A atribuição em lote falhou e não foi possível desfazer todas as alterações: "
            f"{failures}."
        )


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

    def read_annotations(self, session_id, **kwargs):
        """Expose the canonical sidecar seam to export/file helpers."""
        return self._library.read_annotations(session_id, **kwargs)

    def list_report_metadata(self, session_id, **kwargs):
        """Expose bounded report metadata without bypassing the library."""
        return self._library.list_report_metadata(session_id, **kwargs)

    def get_report(self, session_id, report_id):
        """Resolve report bodies only after an explicit export selection."""
        return self._library.get_report(session_id, report_id)

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

            deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
            while True:
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as error:
                    if time.monotonic() >= deadline:
                        raise MeetingLibraryError(
                            "O bloqueio canônico está ocupado há muito tempo; "
                            "feche a outra instância ou remova o bloqueio após verificar o processo."
                        ) from error
                    time.sleep(LOCK_POLL_SECONDS)
        else:
            import fcntl

            deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except (BlockingIOError, OSError) as error:
                    if time.monotonic() >= deadline:
                        raise MeetingLibraryError(
                            "O bloqueio canônico está ocupado há muito tempo; "
                            "feche a outra instância ou remova o bloqueio após verificar o processo."
                        ) from error
                    time.sleep(LOCK_POLL_SECONDS)
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
            # These defaults are returned in memory only.  A legacy workspace
            # is never rewritten merely because it is opened.
            "privacy_defaults": {
                "recording_notice": {"enabled": False, "language": "pt-BR"},
                # Existing workspaces historically allowed explicit answer
                # saving.  Keep that compatibility behavior until an operator
                # opts into the stricter memory-only mode.
                "qa_mode": "explicit_save",
            },
            "retention_defaults": {
                "whole_meeting": {"mode": "keep"},
                "raw_audio": {"mode": "keep", "tracks": []},
                "trash_days": DEFAULT_TRASH_RETENTION_DAYS,
            },
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
        # These sections were added after the first workspace schema.  Their
        # absence is a valid legacy state; malformed present sections remain
        # read-only errors.
        self._validate_privacy_defaults(value.get("privacy_defaults", {}))
        self._validate_retention_defaults(value.get("retention_defaults", {}))
        _, size = _copy_json(value, "workspace.json")
        if size > MAX_WORKSPACE_BYTES:
            raise SchemaError("O workspace excede o limite permitido; o arquivo foi preservado.")

    @staticmethod
    def _validate_privacy_defaults(value):
        """Validate known privacy keys while retaining future nested keys.

        The workspace is deliberately an extensible object.  Known fields are
        strict because a malformed consent or Q&A setting must fail closed;
        unknown fields remain available for a newer build and are never
        normalized or discarded.
        """
        if not isinstance(value, dict):
            raise SchemaError(
                "As configurações privacy_defaults do workspace são inválidas; o arquivo foi preservado."
            )
        notice = value.get("recording_notice")
        if notice is not None:
            if not isinstance(notice, dict):
                raise SchemaError("A configuração de aviso de gravação é inválida; o arquivo foi preservado.")
            if "enabled" in notice and not isinstance(notice["enabled"], bool):
                raise SchemaError("O estado do aviso de gravação é inválido; o arquivo foi preservado.")
            language = notice.get("language")
            if language is not None and language not in SUPPORTED_NOTICE_LANGUAGES:
                raise SchemaError("O idioma do aviso de gravação é inválido; o arquivo foi preservado.")
        if "recording_notice_enabled" in value and not isinstance(value["recording_notice_enabled"], bool):
            raise SchemaError("O estado do aviso de gravação é inválido; o arquivo foi preservado.")
        if "recording_notice_language" in value and value["recording_notice_language"] not in SUPPORTED_NOTICE_LANGUAGES:
            raise SchemaError("O idioma do aviso de gravação é inválido; o arquivo foi preservado.")
        qa_mode = value.get("qa_mode")
        if qa_mode is not None and qa_mode not in SUPPORTED_QA_MODES:
            raise SchemaError("O modo de Q&A do workspace é inválido; o arquivo foi preservado.")

    @staticmethod
    def _retention_policy_value(value, default_mode):
        """Validate one user-facing policy and adapt it to RetentionPolicy.

        ``RetentionPolicy`` intentionally rejects unknown keys.  This adapter
        adds the one ergonomic shorthand used by workspace settings while
        keeping unrelated workspace keys out of the destructive seam.
        """
        from meeting_retention import RetentionPolicy

        if isinstance(value, dict):
            candidate = copy.deepcopy(value)
            selectors = {"mode", "action", "kind", "policy"}
            if not selectors.intersection(candidate):
                candidate["mode"] = default_mode
        else:
            candidate = value
        try:
            policy = RetentionPolicy.from_value(candidate)
        except (TypeError, ValueError) as error:
            raise SchemaError("A política de retenção do workspace é inválida; o arquivo foi preservado.") from error
        if default_mode == "whole_meeting" and policy.mode not in {"keep", "whole_meeting"}:
            raise SchemaError("A política de retenção de reuniões é incompatível; o arquivo foi preservado.")
        if default_mode == "raw_tracks" and policy.mode not in {"keep", "raw_tracks"}:
            raise SchemaError("A política de retenção de áudio é incompatível; o arquivo foi preservado.")
        return policy

    @classmethod
    def _validate_retention_defaults(cls, value):
        if not isinstance(value, dict):
            raise SchemaError(
                "As configurações retention_defaults do workspace são inválidas; o arquivo foi preservado."
            )
        if "whole_meeting" in value:
            cls._retention_policy_value(value["whole_meeting"], "whole_meeting")
        if "whole_meeting_policy" in value:
            cls._retention_policy_value(value["whole_meeting_policy"], "whole_meeting")
        if "raw_audio" in value:
            policy = cls._retention_policy_value(value["raw_audio"], "raw_tracks")
            if policy.mode == "raw_tracks" and any(track not in {"microphone", "system"} for track in policy.tracks):
                raise SchemaError("As fontes da política de áudio são inválidas; o arquivo foi preservado.")
        if "raw_audio_policy" in value:
            policy = cls._retention_policy_value(value["raw_audio_policy"], "raw_tracks")
            if policy.mode == "raw_tracks" and any(track not in {"microphone", "system"} for track in policy.tracks):
                raise SchemaError("As fontes da política de áudio são inválidas; o arquivo foi preservado.")
        for key in ("raw_audio_tracks",):
            tracks = value.get(key)
            if tracks is not None and (
                not isinstance(tracks, list)
                or len(tracks) > 2
                or any(track not in {"microphone", "system"} for track in tracks)
                or len(set(tracks)) != len(tracks)
            ):
                raise SchemaError("As fontes da política de áudio são inválidas; o arquivo foi preservado.")
        # ``raw_tracks`` was used by one pre-release build; keep it readable
        # while the canonical user-facing key remains ``raw_audio``.
        if "raw_tracks" in value:
            cls._retention_policy_value(value["raw_tracks"], "raw_tracks")
        policy_fields = {
            "mode", "action", "kind", "policy", "after_days", "age_days",
            "whole_meeting_after_days", "whole_after_days",
            "raw_track_after_days", "raw_after_days", "tracks", "track",
            "purge_after_days", "trash_after_days", "override",
        }
        if policy_fields.intersection(value):
            candidate = {
                key: copy.deepcopy(item)
                for key, item in value.items()
                if key not in {"trash_days"}
            }
            mode = candidate.get("mode", candidate.get("action", candidate.get("kind", candidate.get("policy"))))
            default_mode = "raw_tracks" if str(mode).casefold() in {"raw", "raw_tracks", "raw_track"} else "whole_meeting"
            cls._retention_policy_value(candidate, default_mode)
        if "trash_days" in value:
            days = value["trash_days"]
            if not _finite_number(days) or float(days) < 0 or float(days) > 36500:
                raise SchemaError("O prazo da lixeira é inválido; o arquivo foi preservado.")

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
                if key in {"privacy_defaults", "retention_defaults"} and isinstance(value, dict):
                    # Configuration sections are extensible.  A partial known
                    # settings update must not erase keys introduced by a
                    # newer build or an operator's future defaults.
                    value = copy.deepcopy(value)
                    if key == "retention_defaults":
                        for policy_key, default_mode in (
                            ("whole_meeting", "whole_meeting"),
                            ("raw_audio", "raw_tracks"),
                            ("raw_tracks", "raw_tracks"),
                        ):
                            candidate = value.get(policy_key)
                            if isinstance(candidate, dict) and not {
                                "mode", "action", "kind", "policy",
                            }.intersection(candidate):
                                # The shorthand ``{after_days: N}`` is a
                                # policy for this named section, even when the
                                # in-memory legacy default was ``keep``.
                                value[policy_key] = {
                                    **candidate, "mode": default_mode,
                                }
                    merged[key] = self._merge_workspace_mapping(
                        merged.get(key) if isinstance(merged.get(key), dict) else {}, value,
                    )
                else:
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

    @staticmethod
    def _merge_workspace_mapping(current, patch):
        result = copy.deepcopy(current)
        for key, value in patch.items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = MeetingLibrary._merge_workspace_mapping(result[key], value)
            else:
                result[key] = copy.deepcopy(value)
        return result

    def read_privacy_defaults(self):
        """Return detached, validated privacy defaults for the controller."""
        value = self.read_workspace().get("privacy_defaults", {})
        result = copy.deepcopy(value)
        notice = result.setdefault("recording_notice", {})
        if isinstance(notice, dict):
            notice.setdefault("enabled", False)
            notice.setdefault("language", "pt-BR")
        result.setdefault("qa_mode", "explicit_save")
        return result

    def read_retention_defaults(self):
        """Return detached retention defaults without rewriting the workspace."""
        value = self.read_workspace().get("retention_defaults", {})
        result = copy.deepcopy(value)
        if "whole_meeting" not in result and "whole_meeting_policy" not in result:
            result["whole_meeting"] = {"mode": "keep"}
        if "raw_audio" not in result and "raw_audio_policy" not in result and "raw_tracks" not in result:
            result["raw_audio"] = {"mode": "keep", "tracks": []}
        result.setdefault("trash_days", DEFAULT_TRASH_RETENTION_DAYS)
        return result

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

    # The explicit aliases keep UI/controller code independent of whether a
    # definition is being created or replaced.  ``save_*`` remains the
    # compare-and-swap primitive and therefore preserves the existing schema
    # and generation semantics.
    create_collection = save_collection
    update_collection = save_collection
    create_series = save_series
    update_series = save_series

    def list_collections(self, *, include_archived=True):
        """Return detached workspace collection definitions for selectors."""
        values = self.read_workspace().get("collections", [])
        if not include_archived:
            values = [item for item in values if not item.get("archived", False)]
        return copy.deepcopy(values)

    def list_series(self, *, include_archived=True):
        """Return detached manually-defined series for selectors."""
        values = self.read_workspace().get("series", [])
        if not include_archived:
            values = [item for item in values if not item.get("archived", False)]
        return copy.deepcopy(values)

    def _preview_definition_delete(self, key, identifier):
        if not _valid_id(identifier):
            raise ValueError("A definição de organização é inválida.")
        workspace = self.read_workspace()
        definitions = workspace.get(key, [])
        if not any(item.get("id") == identifier for item in definitions):
            raise KeyError("A definição de organização não existe.")
        affected = []
        for session_id in self._canonical_session_ids():
            annotations = self.read_annotations(session_id)
            if key == "collections":
                matches = identifier in annotations.get("collection_ids", [])
            else:
                matches = annotations.get("series_id") == identifier
            if matches:
                affected.append(session_id)
        return {
            "kind": key,
            "definition_id": identifier,
            "workspace_generation": workspace["generation"],
            "session_ids": affected,
        }

    def preview_series_delete(self, series_id):
        return self._preview_definition_delete("series", series_id)

    def delete_series(self, series_id, *, expected_generation, confirmed_session_ids):
        """Delete one series definition after an exact membership preview."""
        preview = self.preview_series_delete(series_id)
        if preview["workspace_generation"] != expected_generation:
            raise WorkspaceConflict(expected_generation, preview["workspace_generation"])
        if sorted(set(confirmed_session_ids)) != preview["session_ids"]:
            raise ValueError("A confirmação não corresponde à prévia atual da série.")
        originals, updated = {}, []
        try:
            for session_id in preview["session_ids"]:
                annotations = self.read_annotations(session_id)
                originals[session_id] = annotations
                self.update_annotations(
                    session_id, {"series_id": None},
                    expected_generation=annotations["generation"],
                )
                updated.append(session_id)
            workspace = self.read_workspace()
            self.update_workspace(
                {"series": [item for item in workspace["series"] if item.get("id") != series_id]},
                expected_generation=expected_generation,
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
        return {"series_id": series_id, "removed_from": preview["session_ids"]}

    def archive_collection(self, collection_id, *, archived=True, expected_generation):
        workspace = self.read_workspace()
        definition = next((item for item in workspace["collections"] if item.get("id") == collection_id), None)
        if definition is None:
            raise KeyError("A coleção não existe.")
        updated = copy.deepcopy(definition)
        updated["archived"] = bool(archived)
        return self.save_collection(updated, expected_generation=expected_generation)

    def archive_series(self, series_id, *, archived=True, expected_generation):
        workspace = self.read_workspace()
        definition = next((item for item in workspace["series"] if item.get("id") == series_id), None)
        if definition is None:
            raise KeyError("A série não existe.")
        updated = copy.deepcopy(definition)
        updated["archived"] = bool(archived)
        return self.save_series(updated, expected_generation=expected_generation)

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

    def preview_organization_batch(self, session_ids, *, collection_ids=None,
                                   tags=None, people=None, series_id=_UNSET):
        """Validate a bounded batch before any sidecar is changed."""
        if not isinstance(session_ids, (list, tuple)) or not session_ids:
            raise ValueError("A seleção de reuniões é inválida.")
        if (len(session_ids) > MAX_BATCH_ASSIGNMENTS
                or any(not isinstance(item, str) for item in session_ids)
                or len(set(session_ids)) != len(session_ids)):
            raise ValueError("A seleção de reuniões excede o limite ou contém duplicatas.")
        patch = {}
        if collection_ids is not None:
            patch["collection_ids"] = self._normalized_labels(collection_ids, "de coleções", MAX_COLLECTIONS)
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
        items = []
        for session_id in session_ids:
            annotations = self.read_annotations(session_id)
            items.append({"id": session_id, "generation": annotations["generation"]})
        return {"items": items, "patch": copy.deepcopy(patch), "count": len(items)}

    def assign_organization_batch(self, session_ids, *, collection_ids=None, tags=None,
                                  people=None, series_id=_UNSET,
                                  expected_generations, cancel_event=None):
        """Apply a preflighted organization patch with rollback on any failure."""
        preview = self.preview_organization_batch(
            session_ids, collection_ids=collection_ids, tags=tags,
            people=people, series_id=series_id,
        )
        if not isinstance(expected_generations, dict) or {
            item["id"] for item in preview["items"]
        } != set(expected_generations):
            raise ValueError("A confirmação de gerações não corresponde à prévia.")
        for item in preview["items"]:
            if expected_generations[item["id"]] != item["generation"]:
                raise AnnotationConflict(expected_generations[item["id"]], item["generation"])
        originals, updated = {}, []
        try:
            for item in preview["items"]:
                if cancel_event is not None and cancel_event.is_set():
                    raise RuntimeError("A atribuição em lote foi cancelada antes da conclusão.")
                session_id = item["id"]
                originals[session_id] = self.read_annotations(session_id)
                self.update_annotations(
                    session_id, preview["patch"], expected_generation=item["generation"],
                )
                updated.append(session_id)
            return {"updated": updated, "rolled_back": [], "count": len(updated)}
        except Exception as original_error:
            rollback_failures = []
            rolled_back = []
            for session_id in reversed(updated):
                try:
                    current = self.read_annotations(session_id)
                    original = originals[session_id]
                    restore = {
                        key: copy.deepcopy(value) for key, value in original.items()
                        if key not in {"schema_version", "generation", "updated_at"}
                    }
                    self.update_annotations(
                        session_id, restore, expected_generation=current["generation"],
                    )
                    rolled_back.append(session_id)
                except Exception as rollback_error:
                    rollback_failures.append({
                        "session_id": session_id,
                        "error": str(rollback_error),
                    })
            if rollback_failures:
                raise BatchOrganizationRollbackError(
                    original_error,
                    {
                        "updated": list(updated),
                        "rolled_back": rolled_back,
                        "rollback_failures": rollback_failures,
                        "count": len(updated),
                    },
                ) from original_error
            raise

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

    @staticmethod
    def _catalog_filters(*, collection=None, tag=None, person=None, series=None,
                         status="", date_from=None, date_to=None):
        def values(value):
            if value is None or value == "":
                return ()
            if isinstance(value, str):
                return (value,)
            if isinstance(value, (list, tuple, set, frozenset)):
                if any(not isinstance(item, str) or not item for item in value):
                    raise ValueError("O filtro da biblioteca é inválido.")
                return tuple(value)
            raise ValueError("O filtro da biblioteca é inválido.")
        for value, label in ((date_from, "data inicial"), (date_to, "data final")):
            if value is not None and (not isinstance(value, str) or len(value) > 64):
                raise ValueError(f"O filtro {label} é inválido.")
        if date_from and date_to and date_from > date_to:
            raise ValueError("O intervalo de datas da biblioteca é inválido.")
        return {
            "collection": values(collection), "tag": values(tag), "person": values(person),
            "series": values(series), "status": values(status),
            "date_from": date_from or "", "date_to": date_to or "",
        }

    @staticmethod
    def _catalog_match(metadata, annotations, filters):
        if filters["status"] and metadata.get("status") not in filters["status"]:
            return False
        created = str(metadata.get("created_at") or "")
        if filters["date_from"] and created < filters["date_from"]:
            return False
        if filters["date_to"] and created > filters["date_to"]:
            return False
        for key, annotation_key in (("collection", "collection_ids"), ("tag", "tags"),
                                     ("person", "people"), ("series", "series_id")):
            if filters[key]:
                values = annotations.get(annotation_key, []) if annotation_key != "series_id" else [annotations.get(annotation_key)]
                if not any(item in values for item in filters[key]):
                    return False
        return True

    @staticmethod
    def _catalog_filter_digest(query, filters):
        payload = {
            "query": query,
            "status": filters.get("status", ()),
            "collection": filters.get("collection", ()),
            "tag": filters.get("tag", ()),
            "person": filters.get("person", ()),
            "series": filters.get("series", ()),
            "date_from": filters.get("date_from", ""),
            "date_to": filters.get("date_to", ""),
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def list_sessions_page(self, *, limit=50, cursor=None, query="", status="",
                           collection=None, tag=None, person=None, series=None,
                           date_from=None, date_to=None, collection_id=None,
                           series_id=None, offset=0):
        if collection is None:
            collection = collection_id
        if series is None:
            series = series_id
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("O deslocamento da biblioteca é inválido.")
        if cursor is not None and offset:
            raise ValueError("O cursor não pode ser combinado com deslocamento.")
        try:
            if self.index_state == "ready" and not self._index_stale:
                if hasattr(self.index, "list_sessions_page"):
                    result = self.index.list_sessions_page(
                        limit=limit, cursor=cursor, query=query, status=status,
                        collection=collection, tag=tag, person=person, series=series,
                        date_from=date_from, date_to=date_to, offset=offset,
                    )
                    if isinstance(result, dict):
                        result.setdefault("cursor_reset", False)
                        result.setdefault("index_state", self.index_state)
                    return result
                if cursor is None and not any(
                    value not in (None, "", (), [], {})
                    for value in (collection, tag, person, series, date_from, date_to)
                ):
                    return {"items": self.index.list_sessions(
                        offset=offset, limit=limit, query=query, status=status,
                    ), "next_cursor": None, "cursor_reset": False,
                            "index_state": self.index_state}
        except ValueError:
            raise
        except Exception:
            self._mark_index_stale()
        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= 500:
            raise ValueError("O limite da biblioteca é inválido.")
        filters = self._catalog_filters(
            collection=collection, tag=tag, person=person, series=series,
            status=status, date_from=date_from, date_to=date_to,
        )
        if not isinstance(query, str) or len(query) > 512:
            raise ValueError("A busca da biblioteca é inválida.")
        needle = query.casefold()
        filter_digest = self._catalog_filter_digest(query, filters)
        decoded = _library_cursor_decode(cursor)
        cursor_reset = False
        if cursor is not None and decoded is None:
            # A cursor emitted by a disposable index cannot safely be applied
            # to a canonical directory scan.  Reset explicitly and let the
            # controller tell the user that the page changed underneath them.
            cursor_reset = True
            decoded = None
        elif decoded is not None and decoded.get("filters") != filter_digest:
            cursor_reset = True
            decoded = None
        rows = []
        for session_id in self._canonical_session_ids():
            try:
                metadata = self.store.get(session_id, include_events=False)
                annotations = self.read_annotations(session_id)
            except (OSError, ValueError, SchemaError):
                continue
            if not self._catalog_match(metadata, annotations, filters):
                continue
            haystack = "\n".join((
                str(annotations.get("title", metadata.get("title", ""))),
                str(annotations.get("notes", metadata.get("notes", ""))),
                " ".join(str(item) for item in annotations.get("tags", [])),
                " ".join(str(item) for item in annotations.get("people", [])),
                " ".join(str(item) for item in annotations.get("collection_ids", [])),
                str(annotations.get("reviewed_summary", "")),
            )).casefold()
            if needle and needle not in haystack:
                revision_ids = [item.get("id") for item in metadata.get("revisions", []) if isinstance(item, dict)]
                if not any(needle in str(segment.get("text", "")).casefold()
                           for revision_id in revision_ids for segment in self.store.get_transcript(session_id, revision_id)):
                    if not any(needle in json.dumps(report.get("generated", ""), ensure_ascii=False).casefold()
                               for report in self.list_reports(session_id)):
                        continue
            rows.append({"id": session_id, "title": annotations.get("title", metadata.get("title", "")),
                         "status": metadata.get("status"), "created_at": metadata.get("created_at"),
                         "duration": metadata.get("duration", 0.0), "error": metadata.get("error")})
        rows.sort(key=lambda item: (str(item.get("created_at") or ""), item["id"]), reverse=True)
        if decoded is not None:
            created_at, session_id = decoded["position"]
            rows = [
                item for item in rows
                if (str(item.get("created_at") or ""), item["id"]) < (created_at, session_id)
            ]
        page = rows[offset:offset + limit]
        next_cursor = None
        if len(rows) > offset + limit and page:
            last = page[-1]
            next_cursor = _library_cursor_encode({
                "v": 1, "kind": "canonical", "filters": filter_digest,
                "position": [str(last.get("created_at") or ""), last["id"]],
            })
        return {
            "items": page, "next_cursor": next_cursor,
            "cursor_reset": cursor_reset,
            "index_state": self.index_state,
        }

    def list_sessions(self, offset=0, limit=50, query="", status="", **filters):
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("O deslocamento da biblioteca é inválido.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= 500:
            raise ValueError("O limite da biblioteca é inválido.")
        page = self.list_sessions_page(
            limit=limit, offset=offset, query=query, status=status, **filters,
        )
        return page["items"]

    list_sessions_cursor = list_sessions_page

    def search(self, query, *, limit=50, offset=0, **filters):
        if filters.get("collection") is None and "collection_id" in filters:
            filters["collection"] = filters.pop("collection_id")
        if filters.get("series") is None and "series_id" in filters:
            filters["series"] = filters.pop("series_id")
        try:
            if self.index_state == "ready" and not self._index_stale:
                results = self.index.search(query, limit=limit, offset=offset, **filters)
                return self._decorate_search_results(results)
        except ValueError:
            raise
        except Exception:
            self._mark_index_stale()
        if not isinstance(query, str) or len(query) > 512:
            raise ValueError("A busca da biblioteca é inválida.")
        if not query.strip():
            return []
        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= 500:
            raise ValueError("O limite da busca é inválido.")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("O deslocamento da busca é inválido.")
        from meeting_index import _bounded_text, _EVIDENCE_WEIGHTS, _query_parts

        query_parts = _query_parts(query)
        if not query_parts:
            return []

        def matches(text):
            normalized = " ".join(str(text).split()).casefold()
            return all(
                " ".join(content.split()).casefold() in normalized
                for content, _is_phrase in query_parts
            )

        catalog_filters = self._catalog_filters(**filters)
        if limit == 0:
            return []
        matched = 0
        returned = []
        for session_id in self._canonical_session_ids():
            try:
                metadata = self.store.get(session_id, include_events=False)
                annotations = self.read_annotations(session_id)
            except (OSError, ValueError, SchemaError):
                continue
            if not self._catalog_match(metadata, annotations, catalog_filters):
                continue
            sources = [("session", None, None, None, " ".join((
                str(annotations.get("title", metadata.get("title", ""))),
                str(annotations.get("notes", metadata.get("notes", ""))),
                " ".join(str(item) for item in annotations.get("tags", [])),
                " ".join(str(item) for item in annotations.get("people", [])),
            )))]
            def source_stream():
                yield sources[0]
                for revision in metadata.get("revisions", []):
                    revision_id = revision.get("id") if isinstance(revision, dict) else None
                    if isinstance(revision_id, str):
                        for segment in self.store.get_transcript(session_id, revision_id):
                            yield ("transcript", revision_id, segment.get("id"), None,
                                   str(segment.get("text", "")), segment.get("start"),
                                   segment.get("end"))
                for report in self.list_reports(session_id):
                    generated = json.dumps(report.get("generated", ""), ensure_ascii=False)
                    yield ("report", report.get("transcript_revision"), None,
                           report.get("id"), generated, None, None)
                    reviewed = report.get("reviewed_artifact")
                    if isinstance(reviewed, dict):
                        yield ("reviewed_artifact", report.get("transcript_revision"), None,
                               report.get("id"), json.dumps(reviewed, ensure_ascii=False), None, None)

            for source in source_stream():
                kind, revision_id, segment_id, report_id, text = source[:5]
                if matches(text):
                    if matched < offset:
                        matched += 1
                        continue
                    returned.append({"source_kind": kind, "session_id": session_id,
                                     "revision_id": revision_id, "segment_id": segment_id,
                                     "report_id": report_id, "snippet": _bounded_text(text),
                                     "evidence_weight": _EVIDENCE_WEIGHTS.get(kind, 0.25),
                                     "primary": kind == "transcript",
                                     **({"start": source[5], "end": source[6],
                                         "timestamp": {"start": source[5], "end": source[6]}}
                                        if kind == "transcript" else {})})
                    matched += 1
                    if len(returned) >= limit:
                        return returned
        return returned

    def _decorate_search_results(self, results):
        """Attach bounded canonical provenance to disposable-index hits."""
        decorated = []
        for raw in results or ():
            if not isinstance(raw, dict):
                continue
            result = copy.deepcopy(raw)
            try:
                resolved = self.resolve_search_result(result)
            except (AttributeError, KeyError, OSError, RuntimeError, ValueError, SchemaError):
                # A stale disposable row must not become a broken navigation
                # target.  Keep the bounded hit visible; the UI will show the
                # canonical resolution error if the user opens it.
                resolved = None
            if isinstance(resolved, dict):
                for key in ("title", "start", "end", "timestamp", "revision_id", "segment_id", "report_id"):
                    if key in resolved:
                        result[key] = copy.deepcopy(resolved[key])
            decorated.append(result)
        return decorated

    def resolve_search_result(self, result):
        """Resolve one bounded search hit to canonical navigation evidence.

        The returned object contains no filesystem paths.  Transcript hits are
        looked up by session, revision, and segment; report hits are resolved
        by report revision.  Deleted sessions, stale revisions, and missing
        segments fail closed so callers cannot display a citation that merely
        existed in the disposable index.
        """
        if not isinstance(result, dict):
            raise ValueError("O resultado de busca é inválido.")
        session_id = result.get("session_id")
        kind = result.get("source_kind")
        if not _valid_id(session_id):
            raise ValueError("A reunião da busca é inválida.")
        metadata = self.store.get(session_id, include_events=False)
        title = metadata.get("title", "")
        if kind == "transcript":
            revision_id = result.get("revision_id")
            segment_id = result.get("segment_id")
            if not isinstance(revision_id, str) or not isinstance(segment_id, str):
                raise ValueError("A fonte de transcrição é incompleta.")
            revision = next(
                (item for item in metadata.get("revisions", ())
                 if isinstance(item, dict) and item.get("id") == revision_id),
                None,
            )
            if revision is None:
                raise ValueError("A revisão citada não existe mais.")
            if revision.get("status") != "completed":
                raise ValueError("A revisão citada não está concluída.")
            segment = next(
                (item for item in self.store.get_transcript(session_id, revision_id)
                 if isinstance(item, dict) and item.get("id") == segment_id),
                None,
            )
            if segment is None:
                raise ValueError("O trecho citado não existe mais na revisão selecionada.")
            start, end = segment.get("start"), segment.get("end")
            return {
                "source_kind": kind, "session_id": session_id, "title": title,
                "revision_id": revision_id, "segment_id": segment_id,
                "start": start, "end": end,
                "timestamp": {"start": start, "end": end},
                "text": str(segment.get("text", ""))[:8000],
            }
        if kind in {"report", "reviewed_artifact"}:
            report_id = result.get("report_id")
            if not isinstance(report_id, str):
                raise ValueError("A fonte de relatório é incompleta.")
            report = self.get_report(session_id, report_id)
            reviewed = report.get("reviewed_artifact")
            if kind == "reviewed_artifact" and not isinstance(reviewed, dict):
                raise ValueError("O artefato revisado atual não está disponível.")
            resolved = self._report_history_projection(
                report, reviewed if kind == "reviewed_artifact" else None,
            )
            resolved.update({
                "source_kind": kind, "session_id": session_id, "title": title,
                "revision_id": report.get("transcript_revision"),
                "report_id": report_id,
            })
            return resolved
        if kind in {"session", "annotation"}:
            return {
                "source_kind": kind, "session_id": session_id, "title": title,
                "revision_id": result.get("revision_id"),
            }
        raise ValueError("A fonte de busca não é suportada.")

    resolve_search_hit = resolve_search_result

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
                if (not isinstance(value, list) or len(value) > MAX_REPORT_CITATIONS
                        or any(not isinstance(item, str) for item in value)):
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
        if len(set(citations)) > MAX_REPORT_CITATIONS:
            raise SchemaError("O relatório excede o limite de citações únicas.")
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
        workspace_guard = (
            _writer_lock(self._workspace_path())
            if normalized.get("kind") == "qa"
            else contextlib.nullcontext()
        )
        with workspace_guard:
            if normalized.get("kind") == "qa" and self.read_privacy_defaults().get(
                "qa_mode", "explicit_save",
            ) == "memory_only":
                raise ValueError(
                    "O modo de Q&A memory_only não permite salvar respostas."
                )
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

    @staticmethod
    def _report_history_projection(report, reviewed_artifact=None):
        """Return only bounded metadata suitable for a report history list.

        The report body and reviewed sections deliberately never cross this
        seam.  A caller that needs either must select the report id and call
        ``get_report`` explicitly.
        """
        if not isinstance(report, dict):
            raise SchemaError("O relatório não tem metadados utilizáveis.")
        identifier = report.get("id", report.get("report_id"))
        if not _valid_id(identifier, reference=True):
            raise SchemaError("O identificador do relatório é inválido.")
        result = {"id": identifier}
        for key in (
            "schema_version", "kind", "profile_id", "profile_version",
            "session_id", "transcript_revision", "status", "created_at",
            "completed_at", "virtual",
        ):
            if key in report:
                value = report[key]
                if isinstance(value, (str, int, float, bool)) or value is None:
                    result[key] = value
        model = report.get("model")
        if isinstance(model, dict):
            result["model"] = {
                key: model[key]
                for key in ("id", "sha256", "runtime", "context_limit")
                if key in model and isinstance(model[key], (str, int, float, bool))
            }
        if reviewed_artifact is not None:
            result["reviewed"] = True
            if isinstance(reviewed_artifact, dict):
                generation = reviewed_artifact.get("generation")
                if isinstance(generation, int) and not isinstance(generation, bool):
                    result["review_generation"] = generation
        else:
            result["reviewed"] = False
        return result

    @staticmethod
    def _validate_report_history_envelope(session_id, report_id, value):
        """Validate only fields needed to expose one report's metadata.

        The complete envelope remains validated by ``get_report`` on select.
        This path intentionally avoids transcript and generated-payload work.
        """
        if not isinstance(value, dict):
            raise SchemaError("O relatório deve ser um objeto.")
        if value.get("schema_version") != REPORT_SCHEMA_VERSION:
            raise SchemaError("A versão do relatório é incompatível.")
        if value.get("id", value.get("report_id")) != report_id:
            raise SchemaError("O identificador do relatório não corresponde ao arquivo.")
        if value.get("session_id") != session_id:
            raise SchemaError("O relatório referencia outra reunião.")
        if value.get("kind") not in {"report", "qa"}:
            raise SchemaError("O tipo do relatório é inválido.")
        if not _valid_id(value.get("profile_id")):
            raise SchemaError("O perfil do relatório é inválido.")
        profile_version = value.get("profile_version")
        if (isinstance(profile_version, bool) or not isinstance(profile_version, int)
                or profile_version < 1):
            raise SchemaError("A versão do perfil é inválida.")
        if not isinstance(value.get("transcript_revision"), str):
            raise SchemaError("A revisão do relatório é inválida.")
        model = value.get("model")
        if not isinstance(model, dict) or not _valid_id(model.get("id"), reference=True):
            raise SchemaError("A proveniência do modelo é inválida.")
        if not _valid_timestamp(value.get("created_at")):
            raise SchemaError("A data do relatório é inválida.")

    def list_report_metadata(self, session_id, *, include_legacy=True,
                             limit=REPORT_HISTORY_LIMIT, cancel_event=None):
        """List a bounded history projection without retaining report bodies.

        Each report file is read and reduced one at a time.  Only the selected
        metadata rows remain in memory; generated sections and reviewed text
        are available through ``get_report`` after an explicit selection.
        """
        try:
            requested = int(limit)
        except (TypeError, ValueError):
            requested = REPORT_HISTORY_LIMIT
        if isinstance(limit, bool):
            requested = REPORT_HISTORY_LIMIT
        requested = max(1, min(REPORT_HISTORY_LIMIT, requested))
        annotations = self.read_annotations(session_id)
        reviewed_values = annotations.get("reviewed_artifacts", {})
        reviewed = {}
        if isinstance(reviewed_values, dict):
            for report_id, artifact in reviewed_values.items():
                if isinstance(artifact, dict):
                    generation = artifact.get("generation")
                    reviewed[report_id] = {
                        "generation": generation,
                    } if isinstance(generation, int) and not isinstance(generation, bool) else {}
                else:
                    reviewed[report_id] = {}
        has_reviewed_summary = bool(annotations.get("reviewed_summary"))
        del reviewed_values, annotations
        metadata = self.store.get(session_id, include_events=False)
        has_legacy = bool(metadata.get("summary") or has_reviewed_summary)
        legacy_created = metadata.get("updated_at") or metadata.get("created_at")
        del metadata
        structured_limit = requested - 1 if include_legacy and has_legacy else requested
        structured = []
        directory = self._reports_dir(session_id)
        if os.path.isdir(directory):
            with os.scandir(directory) as entries:
                names = sorted(
                    entry.name for entry in entries
                    if entry.is_file(follow_symlinks=False) and entry.name.endswith(".json")
                )
            if len(names) > MAX_REPORTS:
                raise SchemaError("Há relatórios demais nesta reunião.")
            # ceiling: exact created_at ordering currently requires reading at
            # most MAX_REPORTS canonical envelopes; a manifest would be needed
            # before reducing this I/O bound without changing ordering.
            for name in names:
                if cancel_event is not None and cancel_event.is_set():
                    raise RuntimeError("A leitura do histórico de relatórios foi cancelada.")
                report_id = name[:-5]
                path = self._report_path(session_id, report_id)
                value = self._load_versioned(
                    path, REPORT_SCHEMA_VERSION, f"reports/{name}", MAX_REPORT_BYTES,
                )
                self._validate_report_history_envelope(session_id, report_id, value)
                row = self._report_history_projection(
                    value, reviewed.get(report_id) if isinstance(reviewed, dict) else None,
                )
                del value
                structured.append(row)
                structured.sort(key=lambda item: (str(item.get("created_at") or ""), item["id"]))
                if len(structured) > max(1, structured_limit):
                    del structured[:-max(1, structured_limit)]
        structured.sort(key=lambda item: (str(item.get("created_at") or ""), item["id"]))
        result = []
        if include_legacy and has_legacy:
            result.append({
                "schema_version": 0,
                "id": "legacy-summary",
                "kind": "legacy-summary",
                "virtual": True,
                "session_id": session_id,
                "created_at": legacy_created,
                "reviewed": has_reviewed_summary,
            })
        return (result + structured)[-requested:]

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
        """Refuse unjournaled canonical deletion.

        User-facing deletion belongs to ``MeetingRetention`` through
        ``MeetingController.delete_session`` so the exact target inventory,
        trash move, rollback journal, and purge confirmation remain mandatory.
        Keeping this compatibility method fail-closed prevents older callers
        from silently bypassing those guarantees.
        """
        self._session_dir(session_id)  # validate without reading or mutating
        raise RuntimeError(
            "A exclusão direta foi desativada. Use MeetingController.delete_session "
            "para mover a reunião à lixeira recuperável."
        )

    delete_session = delete

    @staticmethod
    def _index_annotation_view(metadata, annotations):
        """Keep canonical history intact while projecting only active overlays."""
        if annotations is None:
            return None
        active = MeetingLibrary._active_revision_id(metadata)
        return MeetingLibrary._filter_annotation_revision(annotations, active) if active else copy.deepcopy(annotations)

    def _index_annotations(self, metadata, annotations):
        """Add disposable display labels without changing canonical sidecars."""
        value = self._index_annotation_view(metadata, annotations)
        if value is None:
            return None
        try:
            workspace = self.read_workspace()
            labels = {
                item.get("id"): item.get("name")
                for item in workspace.get("collections", [])
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            }
            value["_collection_labels"] = [
                labels[item] for item in value.get("collection_ids", [])
                if item in labels
            ]
        except (OSError, SchemaError, ValueError):
            # Workspace corruption is handled by canonical reads; the index
            # remains useful with stable collection IDs until repair.
            value["_collection_labels"] = []
        return value

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
        projected = dict(transcripts)
        values = projected.get(active, ())

        def overlay():
            for segment in values:
                if not isinstance(segment, dict):
                    yield segment
                    continue
                value = copy.deepcopy(segment)
                if value.get("id") in by_segment:
                    value["speaker"] = by_segment[value["id"]]
                yield value

        projected[active] = overlay()
        return projected

    def _project_after_canonical_write(self, session_id):
        with self._catalog_lock:
            with _writer_lock(self._session_dir(session_id)):
                try:
                    path = self._annotations_path(session_id)
                    metadata = self.store.get(session_id, include_events=False)
                    annotations = self.read_annotations(session_id) if os.path.lexists(path) else None
                    projected_annotations = self._index_annotations(metadata, annotations)
                    reports = self._read_report_files(session_id)
                    if annotations and annotations.get("speaker_labels") and hasattr(self.index, "index_session"):
                        transcripts = {
                            revision.get("id"): self.store.get_transcript(session_id, revision.get("id"))
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

    def on_session_trashed(self, session_id):
        """Remove a moved bundle from the disposable catalog, if present.

        Retention has already committed the same-root move before this hook is
        called.  A missing catalog is therefore a successful no-op; an opened
        catalog failure is reported to the retention journal as stale.
        """
        with self._catalog_lock:
            index = self._index
            if index is None and os.path.lexists(os.path.join(self.home_root, "library.sqlite")):
                index = self.index
            if index is None:
                return True
            try:
                result = bool(index.remove_session(session_id))
            except Exception:
                self._mark_index_stale_with_reason("canonical retention move was not projected")
                return False
            if not result:
                self._mark_index_stale_with_reason("canonical retention move was not projected")
            return result

    def on_session_restored(self, session_id):
        """Reproject a bundle after a successful trash restore."""
        return self.project_session(session_id)

    def on_raw_tracks_removed(self, session_id):
        """Reproject canonical purged-track availability markers."""
        return self.project_session(session_id)

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
            def iter_sessions():
                try:
                    entries = os.scandir(self.meetings_root)
                except OSError as error:
                    self._mark_index_stale_with_reason("meeting directory scan failed")
                    raise SchemaError(
                        "A biblioteca não pôde ser lida completamente; o índice não foi publicado."
                    ) from error
                with entries:
                    for entry in entries:
                        if not _valid_id(entry.name):
                            continue
                        path = os.path.join(self.meetings_root, entry.name)
                        if _is_link_or_junction(path) or not entry.is_dir(follow_symlinks=False):
                            continue
                        session_id = entry.name
                        try:
                            metadata = self.store.get(session_id, include_events=False)
                        except (OSError, ValueError) as error:
                            self._mark_index_stale_with_reason("meeting metadata is unreadable")
                            raise SchemaError(
                                "Os metadados da reunião não puderam ser lidos; o índice não foi publicado."
                            ) from error
                        annotations_path = self._annotations_path(session_id)
                        # A malformed/future sidecar is actionable corruption,
                        # not an invitation to silently rebuild from legacy metadata.
                        annotations = (
                            self.read_annotations(session_id)
                            if os.path.lexists(annotations_path) else None
                        )
                        revisions = metadata.get("revisions", [])
                        if not isinstance(revisions, list):
                            self._mark_index_stale_with_reason("meeting revisions are malformed")
                            raise SchemaError(
                                "As revisões da reunião são inválidas; o índice não foi publicado."
                            )
                        transcripts = {}
                        for revision in revisions:
                            if not isinstance(revision, dict) or not isinstance(revision.get("id"), str):
                                self._mark_index_stale_with_reason("meeting revision identity is malformed")
                                raise SchemaError(
                                    "A identidade da revisão é inválida; o índice não foi publicado."
                                )
                            revision_id = revision["id"]
                            expected = revision.get("segments")

                            def checked_values(
                                session_id=session_id,
                                revision_id=revision_id,
                                expected=expected,
                            ):
                                count = 0
                                try:
                                    for value in self.store.get_transcript(session_id, revision_id):
                                        count += 1
                                        yield value
                                except (OSError, ValueError) as error:
                                    self._mark_index_stale_with_reason(
                                        "transcript corruption prevents a complete rebuild"
                                    )
                                    raise SchemaError(
                                        "A transcrição não pôde ser lida; o índice não foi marcado como íntegro."
                                    ) from error
                                if (isinstance(expected, int) and not isinstance(expected, bool)
                                        and expected != count):
                                    self._mark_index_stale_with_reason(
                                        "transcript corruption prevents a complete rebuild"
                                    )
                                    raise SchemaError(
                                        "A transcrição está incompleta; o índice não foi marcado como íntegro."
                                    )

                            transcripts[revision_id] = checked_values()
                        projected_annotations = self._index_annotations(metadata, annotations)
                        projected_transcripts = self._index_transcripts(
                            metadata, transcripts, annotations,
                        )
                        yield (metadata, projected_annotations, projected_transcripts)

            result = self.index.rebuild(iter_sessions(), cancel_event=cancel_event, progress=progress)
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
    "BatchOrganizationRollbackError",
    "MeetingLibrary",
    "MeetingLibraryError",
    "PathSafetyError",
    "SchemaError",
    "UnsupportedSchemaError",
    "WORKSPACE_FILENAME",
    "WORKSPACE_SCHEMA_VERSION",
    "WorkspaceConflict",
]
