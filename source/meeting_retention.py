"""Deterministic, recoverable retention operations for local meeting bundles.

The capture store remains the source of truth.  This module deliberately talks
to it through a small duck-typed seam so the retention policy can be exercised
without making the store, index, or GUI know about filesystem operations.  A
plan is read-only and immutable; every mutating method revalidates that plan,
resolves the same app-owned roots, and records a recoverable state transition.
"""

import contextlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
import time
import uuid

from snippet_utils import write_json_atomic


RETENTION_SCHEMA_VERSION = 1
TRASH_TOMBSTONE_SUFFIX = ".tombstone.json"
OPERATION_JOURNAL_SUFFIX = ".jsonl"
DEFAULT_TRASH_RETENTION_DAYS = 30.0
DEFAULT_LOCK_TIMEOUT_SECONDS = 5.0
MAX_JOURNAL_LINE_BYTES = 2 * 1024 * 1024
MAX_TRANSCRIPT_SEGMENTS = 100_000
MAX_TRANSCRIPT_BYTES = 128 * 1024 * 1024
_TRACKS = frozenset(("microphone", "system"))
_TRACK_ORDER = ("microphone", "system")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_TERMINAL_STATUSES = frozenset(("completed", "partial", "failed", "cancelled", "interrupted"))
_RAW_ELIGIBLE_STATUSES = frozenset(("completed", "partial"))


class RetentionError(ValueError):
    """Base error for invalid, stale, or incomplete retention operations."""


class RetentionSafetyError(RetentionError):
    """A requested target is not provably app-owned and link-free."""


class ActiveLeaseError(RetentionError):
    """Capture, processing, or playback still holds the meeting."""


class ConfirmationRequired(RetentionError):
    """A destructive operation needs an explicit caller confirmation."""


class PlanConflict(RetentionError):
    """The canonical target changed after a plan was created."""


class OperationRecoveryError(RetentionError):
    """An interrupted operation cannot be recovered without risking data."""


class RetentionLockTimeout(RetentionError, TimeoutError):
    """Another process holds the retention writer lock past the deadline."""


@dataclass(frozen=True)
class RetentionPolicy:
    """A bounded retention rule.

    ``mode`` is one of ``keep``, ``whole_meeting``, or ``raw_tracks``.  The
    mapping parser also accepts the longer names used by workspace settings,
    which lets the GUI and a future adapter evolve without changing this
    module's public seam.
    """

    mode: str = "keep"
    after_days: float | None = None
    tracks: tuple[str, ...] = ()
    purge_after_days: float = DEFAULT_TRASH_RETENTION_DAYS
    override: object = None

    def __post_init__(self):
        mode = str(self.mode).strip().casefold().replace("-", "_")
        aliases = {
            "keep_indefinitely": "keep",
            "indefinite": "keep",
            "whole": "whole_meeting",
            "whole_meeting_age": "whole_meeting",
            "raw": "raw_tracks",
            "raw_track": "raw_tracks",
            "raw_track_age": "raw_tracks",
        }
        mode = aliases.get(mode, mode)
        if mode not in {"keep", "whole_meeting", "raw_tracks"}:
            raise ValueError("A política de retenção não é reconhecida.")
        object.__setattr__(self, "mode", mode)
        if self.after_days is not None:
            value = float(self.after_days)
            if not math.isfinite(value) or value < 0:
                raise ValueError("A idade da política de retenção é inválida.")
            object.__setattr__(self, "after_days", value)
        purge = float(self.purge_after_days)
        if not math.isfinite(purge) or purge < 0:
            raise ValueError("O prazo da lixeira é inválido.")
        object.__setattr__(self, "purge_after_days", purge)
        values = (self.tracks,) if isinstance(self.tracks, str) else tuple(self.tracks or ())
        if any(track not in _TRACKS for track in values) or len(set(values)) != len(values):
            raise ValueError("As fontes da política de retenção são inválidas.")
        object.__setattr__(self, "tracks", values)

    @classmethod
    def keep_indefinitely(cls, **kwargs):
        return cls(mode="keep", **kwargs)

    @classmethod
    def whole_meeting(cls, after_days, **kwargs):
        return cls(mode="whole_meeting", after_days=after_days, **kwargs)

    @classmethod
    def raw_tracks(cls, after_days, tracks=(), **kwargs):
        selected = (tracks,) if isinstance(tracks, str) else tuple(tracks)
        return cls(mode="raw_tracks", after_days=after_days, tracks=selected, **kwargs)

    @classmethod
    def from_value(cls, value):
        if value is None:
            return cls.keep_indefinitely()
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            return cls(mode=value)
        if not isinstance(value, dict):
            raise ValueError("A política de retenção deve ser um objeto.")
        data = dict(value)
        mode = data.pop("mode", data.pop("action", data.pop("kind", data.pop("policy", None))))
        whole_age = data.pop("whole_meeting_after_days", data.pop("whole_after_days", None))
        raw_age = data.pop("raw_track_after_days", data.pop("raw_after_days", None))
        age = data.pop("after_days", data.pop("age_days", None))
        if mode is None:
            if whole_age is not None:
                mode, age = "whole_meeting", whole_age
            elif raw_age is not None:
                mode, age = "raw_tracks", raw_age
            else:
                mode = "keep"
        normalized = {
            "mode": mode,
            "after_days": age if age is not None else raw_age if str(mode).casefold() in {"raw", "raw_tracks", "raw_track"} else whole_age,
            "tracks": data.pop("tracks", data.pop("track", ())),
            "purge_after_days": data.pop("purge_after_days", data.pop("trash_after_days", DEFAULT_TRASH_RETENTION_DAYS)),
            "override": data.pop("override", None),
        }
        if data:
            # Unknown policy keys are retained only by the caller's workspace;
            # silently ignoring one here would make a destructive rule look
            # active when it is not.  Keep the accepted compatibility fields
            # narrow and fail closed.
            raise ValueError("A política de retenção contém campos desconhecidos.")
        return cls(**normalized)

    def as_dict(self):
        return {
            "mode": self.mode,
            "after_days": self.after_days,
            "tracks": list(self.tracks),
            "purge_after_days": self.purge_after_days,
        }


@dataclass(frozen=True)
class RetentionTarget:
    path: str
    kind: str
    bytes: int = 0
    relative: str = ""
    track: str | None = None

    def as_dict(self):
        return {
            "path": self.path,
            "kind": self.kind,
            "bytes": self.bytes,
            "relative": self.relative,
            "track": self.track,
        }


@dataclass(frozen=True)
class RetentionPlan:
    """Immutable dry-run output and approval token for one operation."""

    session_id: str
    operation: str
    planned_at: str
    policy: RetentionPolicy
    eligible: bool
    reasons: tuple[str, ...] = ()
    targets: tuple[RetentionTarget, ...] = ()
    byte_estimate: int = 0
    canonical_changes: tuple[str, ...] = ()
    lost_capabilities: tuple[str, ...] = ()
    excluded_external_exports: tuple[str, ...] = ()
    missing_paths: tuple[str, ...] = ()
    recovery_mode: str = "none"
    inventory_fingerprint: str = ""
    source_path: str = ""
    meetings_root: str = ""
    trash_root: str = ""
    raw_tracks: tuple[str, ...] = ()
    dry_run: bool = True
    eligibility_fingerprint: str = ""

    @property
    def target_paths(self):
        return tuple(target.path for target in self.targets)

    def as_dict(self):
        return {
            "session_id": self.session_id,
            "operation": self.operation,
            "planned_at": self.planned_at,
            "policy": self.policy.as_dict(),
            "eligible": self.eligible,
            "reasons": list(self.reasons),
            "targets": [target.as_dict() for target in self.targets],
            "target_paths": list(self.target_paths),
            "byte_estimate": self.byte_estimate,
            "canonical_changes": list(self.canonical_changes),
            "lost_capabilities": list(self.lost_capabilities),
            "excluded_external_exports": list(self.excluded_external_exports),
            "missing_paths": list(self.missing_paths),
            "recovery_mode": self.recovery_mode,
            "inventory_fingerprint": self.inventory_fingerprint,
            "eligibility_fingerprint": self.eligibility_fingerprint,
            "source_path": self.source_path,
            "meetings_root": self.meetings_root,
            "trash_root": self.trash_root,
            "raw_tracks": list(self.raw_tracks),
            "dry_run": self.dry_run,
        }

    to_dict = as_dict

    def __getitem__(self, key):
        return self.as_dict()[key]


@dataclass(frozen=True)
class RetentionResult:
    operation_id: str
    session_id: str
    state: str
    byte_estimate: int = 0
    target_paths: tuple[str, ...] = ()
    lost_capabilities: tuple[str, ...] = ()
    index_updated: bool | None = None
    message: str = ""

    def as_dict(self):
        return {
            "operation_id": self.operation_id,
            "session_id": self.session_id,
            "state": self.state,
            "byte_estimate": self.byte_estimate,
            "target_paths": list(self.target_paths),
            "lost_capabilities": list(self.lost_capabilities),
            "index_updated": self.index_updated,
            "message": self.message,
        }

    def __getitem__(self, key):
        return self.as_dict()[key]


@dataclass(frozen=True)
class TrashEntry:
    session_id: str
    operation_id: str
    path: str
    tombstone_path: str
    deleted_at: str
    purge_after: str
    byte_estimate: int

    def as_dict(self):
        return {
            "session_id": self.session_id,
            "operation_id": self.operation_id,
            "path": self.path,
            "tombstone_path": self.tombstone_path,
            "deleted_at": self.deleted_at,
            "purge_after": self.purge_after,
            "byte_estimate": self.byte_estimate,
        }


def _utc(value):
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("O relógio da retenção deve informar o fuso horário.")
        return value.astimezone(timezone.utc)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(float(value)):
            raise ValueError("O relógio da retenção é inválido.")
        return datetime.fromtimestamp(float(value), timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("O relógio da retenção é inválido.") from error
        return _utc(parsed)
    raise ValueError("O relógio da retenção é inválido.")


def _stamp(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_stamp(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        return _utc(value)
    except ValueError:
        return None


def _is_link(path):
    return os.path.islink(path) or getattr(os.path, "isjunction", lambda _path: False)(path)


def _common(root, path):
    try:
        return os.path.normcase(os.path.commonpath((os.path.abspath(root), os.path.abspath(path)))) == os.path.normcase(os.path.abspath(root))
    except (OSError, ValueError):
        return False


def _has_link_component(path):
    absolute = os.path.abspath(os.fspath(path))
    drive, tail = os.path.splitdrive(absolute)
    current = drive + os.sep if drive else os.sep
    for part in tail.replace("/", os.sep).replace("\\", os.sep).strip(os.sep).split(os.sep):
        if not part:
            continue
        current = os.path.join(current, part)
        if os.path.lexists(current) and _is_link(current):
            return True
    return False


@contextlib.contextmanager
def _cross_process_lock(path, timeout):
    """Acquire one app-owned byte with a bounded cross-process wait."""
    path = os.path.abspath(os.fspath(path))
    if _has_link_component(path):
        raise RetentionSafetyError("A trava de retenção contém um link ou junction.")
    flags = os.O_CREAT | os.O_RDWR
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    deadline = time.monotonic() + float(timeout)
    fd = None
    handle = None
    acquired = False
    try:
        fd = os.open(path, flags | no_follow, 0o600)
        if _is_link(path) or not os.path.isfile(path):
            raise RetentionSafetyError("A trava de retenção não é um arquivo regular.")
        handle = os.fdopen(fd, "a+b", buffering=0)
        fd = None
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            lock = getattr(msvcrt, "LK_NBLCK", None)
            unlock = msvcrt.LK_UNLCK
            if lock is None:
                raise RetentionError("O runtime Windows não oferece uma trava não bloqueante segura.")
            while True:
                try:
                    msvcrt.locking(handle.fileno(), lock, 1)
                    acquired = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RetentionLockTimeout("A operação de retenção está ocupada por outro processo.")
                    time.sleep(min(0.05, max(0.001, deadline - time.monotonic())))
            try:
                yield
            finally:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), unlock, 1)
                finally:
                    handle.close()
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except (BlockingIOError, OSError) as error:
                    if isinstance(error, OSError) and getattr(error, "errno", None) not in {11, 13, 35}:
                        raise
                    if time.monotonic() >= deadline:
                        raise RetentionLockTimeout("A operação de retenção está ocupada por outro processo.")
                    time.sleep(min(0.05, max(0.001, deadline - time.monotonic())))
            try:
                yield
            finally:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                finally:
                    handle.close()
    finally:
        if handle is not None and not acquired:
            handle.close()
        if fd is not None:
            os.close(fd)


def _valid_id(value):
    return isinstance(value, str) and bool(_ID_RE.fullmatch(value))


def _valid_relative(value):
    if not isinstance(value, str) or not value or os.path.isabs(value) or ":" in value:
        return False
    normalized = value.replace("\\", "/")
    return all(part not in {"", ".", ".."} for part in normalized.split("/"))


def _valid_segment_id(value):
    return isinstance(value, str) and 1 <= len(value) <= 128 and not any(
        char in value for char in ("/", "\\", "\x00")
    )


def _json_size(value):
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8"))


def _json_fingerprint(value):
    """Hash canonical JSON state without retaining user text in journals."""
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise RetentionError("O estado canônico contém valores que não podem ser validados.") from error
    return hashlib.sha256(encoded).hexdigest()


class MeetingRetention:
    """Retention planning and recoverable operations for one meeting root."""

    def __init__(
        self,
        store,
        library=None,
        *,
        workspace_root=None,
        meetings_root=None,
        trash_root=None,
        retention_ops_root=None,
        clock=None,
        lease_checker=None,
        trash_retention_days=DEFAULT_TRASH_RETENTION_DAYS,
        lock_timeout=DEFAULT_LOCK_TIMEOUT_SECONDS,
        failure_injector=None,
    ):
        if store is None and library is None:
            raise ValueError("MeetingRetention precisa de um MeetingStore ou MeetingLibrary.")
        self.store = store or getattr(library, "store", None)
        self.library = library
        raw_meetings = meetings_root or getattr(library, "meetings_root", None) or getattr(self.store, "root", None)
        if raw_meetings is None:
            raise ValueError("A raiz de reuniões é obrigatória.")
        self.meetings_root = os.path.abspath(os.fspath(raw_meetings))
        raw_home = workspace_root or getattr(library, "home_root", None)
        if raw_home is None:
            raw_home = (
                os.path.dirname(self.meetings_root)
                if os.path.basename(self.meetings_root).casefold() == "meetings"
                else self.meetings_root
            )
        self.home_root = os.path.abspath(os.fspath(raw_home))
        self.trash_root = os.path.abspath(os.fspath(trash_root or os.path.join(self.home_root, "trash")))
        self.retention_ops_root = os.path.abspath(
            os.fspath(retention_ops_root or os.path.join(self.home_root, "retention-ops"))
        )
        self.lock_path = os.path.abspath(os.path.join(self.home_root, "retention.lock"))
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lease_checker = lease_checker
        self.trash_retention_days = float(trash_retention_days)
        if not math.isfinite(self.trash_retention_days) or self.trash_retention_days < 0:
            raise ValueError("O prazo padrão da lixeira é inválido.")
        self.lock_timeout = float(lock_timeout)
        if not math.isfinite(self.lock_timeout) or self.lock_timeout < 0:
            raise ValueError("O prazo da trava de retenção é inválido.")
        self.failure_injector = failure_injector
        self._lock = threading.RLock()
        self._validate_roots()

    # -- Planning -------------------------------------------------------

    def _validate_roots(self):
        for root, label in (
            (self.home_root, "workspace"),
            (self.meetings_root, "meetings"),
            (self.trash_root, "trash"),
            (self.retention_ops_root, "retention operations"),
        ):
            if _has_link_component(root):
                raise RetentionSafetyError(f"A raiz {label} contém um link ou junction.")
        if not _common(self.home_root, self.meetings_root):
            raise RetentionSafetyError("A raiz de reuniões está fora do workspace.")
        if not _common(self.home_root, self.trash_root) or not _common(self.home_root, self.retention_ops_root):
            raise RetentionSafetyError("As raízes de retenção devem permanecer no workspace.")
        if os.path.lexists(self.lock_path) and (_is_link(self.lock_path) or not os.path.isfile(self.lock_path)):
            raise RetentionSafetyError("A trava de retenção não é um arquivo app-owned regular.")
        # A same-root move must not cross to another volume.  Missing roots
        # are created only by an approved mutating operation.
        existing = [path for path in (self.home_root, self.meetings_root) if os.path.isdir(path)]
        if existing and os.path.abspath(self.home_root) != os.path.commonpath(existing):
            raise RetentionSafetyError("As raízes de retenção não compartilham uma raiz segura.")

    def _now(self):
        try:
            return _utc(self.clock())
        except (TypeError, ValueError, OSError) as error:
            raise RetentionError(str(error)) from error

    @contextlib.contextmanager
    def _writer_lock(self):
        """Serialize destructive retention work across processes."""
        if not os.path.lexists(self.home_root):
            os.makedirs(self.home_root, exist_ok=True)
        if _has_link_component(self.home_root) or not os.path.isdir(self.home_root):
            raise RetentionSafetyError("O workspace da retenção não é uma pasta segura.")
        lock_path = self._owned_path(self.lock_path, self.home_root, "retention lock")
        with _cross_process_lock(lock_path, self.lock_timeout):
            yield

    def _session_path(self, session_id):
        if not _valid_id(session_id):
            raise ValueError("Identificador de reunião inválido.")
        path = os.path.abspath(os.path.join(self.meetings_root, session_id))
        if not _common(self.meetings_root, path) or _has_link_component(path):
            raise RetentionSafetyError("A pasta da reunião aponta para um link ou junction.")
        return path

    def _owned_path(self, path, root, label, *, must_exist=False):
        absolute = os.path.abspath(os.fspath(path))
        if not _common(root, absolute) or _has_link_component(absolute):
            raise RetentionSafetyError(f"O alvo {label} não é app-owned ou contém um link.")
        if must_exist and not os.path.lexists(absolute):
            raise FileNotFoundError(absolute)
        return absolute

    def _metadata(self, session_id):
        if self.library is not None:
            getter = getattr(self.library, "get_session", None) or getattr(self.library, "get", None)
        else:
            getter = None
        if getter is None:
            getter = getattr(self.store, "get", None)
        if getter is None:
            raise ValueError("O colaborador de reuniões não oferece leitura de metadados.")
        try:
            return copy.deepcopy(getter(session_id, include_events=False))
        except TypeError:
            return copy.deepcopy(getter(session_id))

    def _annotations(self, session_id, metadata):
        if self.library is not None:
            reader = getattr(self.library, "read_annotations", None)
            if reader is not None:
                return copy.deepcopy(reader(session_id))
        return {
            "title": metadata.get("title", ""),
            "notes": metadata.get("notes", ""),
            "reviewed_summary": metadata.get("reviewed_summary", ""),
            "reviewed_artifacts": {},
            "retention_override": None,
        }

    def _workspace_policy(self):
        if self.library is None:
            return None
        reader = getattr(self.library, "read_workspace", None)
        if reader is None:
            return None
        value = reader()
        if not isinstance(value, dict):
            return None
        defaults = value.get("retention_defaults")
        if not isinstance(defaults, dict):
            return None
        # Workspace settings contain separate policies for whole meetings and
        # raw audio.  The legacy RetentionPolicy seam accepts one policy at a
        # time, so the default destructive action is the whole-meeting rule;
        # callers explicitly planning raw removal pass the raw policy.
        whole = defaults.get("whole_meeting", defaults.get("whole_meeting_policy"))
        if whole is not None:
            return whole
        if any(key in defaults for key in (
            "raw_audio", "raw_audio_policy", "raw_tracks", "raw_audio_tracks", "trash_days",
        )):
            # A legacy workspace may first persist only raw-audio or trash
            # preferences.  Their absence of a whole-meeting rule means keep,
            # not "feed the whole settings object to RetentionPolicy".
            return {"mode": "keep"}
        # Compatibility with the original single-policy workspace shape;
        # ``trash_days`` belongs to the workspace adapter, not RetentionPolicy.
        if any(key in defaults for key in (
            "mode", "action", "kind", "policy", "after_days", "age_days",
            "whole_meeting_after_days", "whole_after_days",
            "raw_track_after_days", "raw_after_days", "tracks", "track",
            "purge_after_days", "trash_after_days", "override",
        )):
            return {key: value for key, value in defaults.items() if key != "trash_days"}
        return defaults

    def _resolve_policy(self, policy, session_id, metadata, override):
        annotations = None
        if override is None:
            try:
                annotations = self._annotations(session_id, metadata)
                override = annotations.get("retention_override")
            except Exception as error:
                raise RetentionError(f"Não foi possível ler a substituição de retenção: {error}") from error
        if override is not None:
            policy = override
        if policy is None:
            policy = self._workspace_policy()
        return RetentionPolicy.from_value(policy)

    def _meeting_age(self, metadata, now):
        raw_stamp = metadata.get("updated_at")
        if raw_stamp is None:
            raw_stamp = metadata.get("created_at")
        stamp = _parse_stamp(raw_stamp)
        if stamp is None:
            return None, "A reunião não tem um horário UTC válido."
        age = (now - stamp).total_seconds() / 86400.0
        if age < 0:
            return age, "O relógio voltou antes da reunião; a política foi mantida."
        return age, ""

    def _active_lease(self, session_id, metadata):
        if metadata.get("status") == "recording":
            return True, "A reunião está em gravação; há um lease de captura ativo."
        active = getattr(self.store, "_active", None)
        if isinstance(active, dict) and session_id in active:
            return True, "A reunião mantém um lease de captura ativo."
        if not isinstance(active, (str, bytes, dict)):
            try:
                if active is not None and session_id in active:
                    return True, "A reunião mantém um lease de captura ativo."
            except TypeError:
                return True, "Não foi possível provar que os leases de captura terminaram."
        checker = self.lease_checker
        if checker is None:
            for owner in (self.library, self.store):
                for name in ("is_lease_active", "has_active_lease", "is_active", "active_for", "has_active_operation"):
                    candidate = getattr(owner, name, None) if owner is not None else None
                    if callable(candidate):
                        checker = candidate
                        break
                if checker is not None:
                    break
                leases = None
                if owner is not None:
                    leases = getattr(owner, "active_leases", None)
                    if leases is None:
                        leases = getattr(owner, "active_operations", None)
                if leases is not None:
                    checker = leases
                    break
        if checker is not None:
            if callable(checker):
                try:
                    value = checker(session_id)
                except TypeError:
                    try:
                        value = checker()
                    except Exception as error:
                        return True, f"Não foi possível provar que os leases terminaram: {error}"
                except Exception as error:
                    return True, f"Não foi possível provar que os leases terminaram: {error}"
            else:
                try:
                    value = session_id in checker
                except TypeError as error:
                    return True, f"Não foi possível provar que os leases terminaram: {error}"
            if isinstance(value, dict):
                recognized = ("active", "processing", "playing", "capturing", "recording")
                value = bool(value.get(session_id)) or any(bool(value.get(key)) for key in recognized)
            if value:
                return True, "A reunião tem um lease ativo de captura, processamento ou reprodução."
        return False, ""

    def _external_exports(self, metadata):
        values = []
        final_audio = metadata.get("final_audio")
        if isinstance(final_audio, dict):
            value = final_audio.get("path")
            if isinstance(value, (str, os.PathLike)):
                path = os.path.abspath(os.path.expanduser(os.fspath(value)))
                # Exports outside the canonical meeting root never become a
                # deletion target.  Do not resolve or inspect their contents.
                if not _common(self.meetings_root, path):
                    values.append(path)
        extra = metadata.get("exports")
        if isinstance(extra, list):
            for value in extra:
                if isinstance(value, (str, os.PathLike)):
                    path = os.path.abspath(os.path.expanduser(os.fspath(value)))
                    if not _common(self.meetings_root, path):
                        values.append(path)
        return tuple(dict.fromkeys(values))

    def _tree_inventory(self, root, *, kind, track=None):
        root = os.path.abspath(root)
        if _has_link_component(root):
            raise RetentionSafetyError("O inventário contém um link ou junction.")
        if not os.path.lexists(root):
            return (), 0
        if _is_link(root) or not os.path.isdir(root):
            raise RetentionSafetyError("O alvo de retenção não é uma pasta app-owned.")
        values = [RetentionTarget(root, kind, 0, "", track)]
        total = 0
        stack = [root]
        while stack:
            current = stack.pop()
            try:
                entries = sorted(os.scandir(current), key=lambda item: item.name.casefold())
            except OSError as error:
                raise RetentionError(f"Não foi possível ler o inventário de retenção: {error}") from error
            for entry in entries:
                path = os.path.abspath(entry.path)
                if _is_link(path):
                    raise RetentionSafetyError("O inventário contém um link ou junction.")
                relative = os.path.relpath(path, root).replace(os.sep, "/")
                if entry.is_dir(follow_symlinks=False):
                    values.append(RetentionTarget(path, "directory", 0, relative, track))
                    stack.append(path)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    raise RetentionSafetyError("O inventário contém um arquivo especial não suportado.")
                try:
                    file_stat = entry.stat(follow_symlinks=False)
                    if getattr(file_stat, "st_nlink", 1) > 1:
                        raise RetentionSafetyError("O inventário contém um hard link sem ownership comprovada.")
                    size = int(file_stat.st_size)
                except OSError as error:
                    raise RetentionError(f"Não foi possível estimar o alvo de retenção: {error}") from error
                if size < 0:
                    raise RetentionSafetyError("O inventário contém um tamanho inválido.")
                values.append(RetentionTarget(path, "file", size, relative, track))
                total += size
        values.sort(key=lambda value: (value.path.count(os.sep), value.path.casefold()))
        return tuple(values), total

    def _fingerprint(self, targets):
        digest = hashlib.sha256()
        for target in targets:
            digest.update(target.kind.encode("ascii"))
            digest.update(b"\0")
            digest.update(os.path.normcase(target.path).encode("utf-8", "surrogatepass"))
            digest.update(b"\0")
            digest.update(str(target.bytes).encode("ascii"))
            digest.update(b"\n")
        return digest.hexdigest()

    def _transcript_state(self, session_id, metadata):
        revisions = metadata.get("revisions")
        if not isinstance(revisions, list):
            return False, False, {}, {}, "As revisões de transcrição são inválidas."
        completed = [item for item in revisions if isinstance(item, dict) and item.get("status") == "completed"]
        pending = [item for item in revisions if isinstance(item, dict) and item.get("status") in {"pending", "processing"}]
        if pending:
            return False, bool(completed), {}, {}, "Há uma revisão de transcrição ainda em processamento."
        if not completed:
            return False, False, {}, {}, "A reunião ainda não tem uma revisão de transcrição concluída."
        revision_ids = {}
        revision_ranges = {}
        total_bytes = 0
        for revision in completed:
            revision_id = revision.get("id")
            if not _valid_id(revision_id):
                return False, True, {}, {}, "Uma revisão concluída tem um identificador inválido."
            reader = getattr(self.library, "get_transcript", None) if self.library is not None else None
            if reader is None:
                reader = getattr(self.store, "get_transcript", None)
            if reader is None:
                return False, True, {}, {}, "O colaborador não oferece leitura de transcrições."
            try:
                segments = list(reader(session_id, revision_id))
            except Exception as error:
                return False, True, {}, {}, f"A revisão de transcrição não pôde ser lida: {error}"
            if len(segments) > MAX_TRANSCRIPT_SEGMENTS:
                return False, True, {}, {}, "A revisão de transcrição excede o limite de retenção."
            expected = revision.get("segments")
            if isinstance(expected, int) and not isinstance(expected, bool) and expected != len(segments):
                return False, True, {}, {}, "A revisão de transcrição está incompleta."
            ids = set()
            ranges = []
            for segment in segments:
                if not isinstance(segment, dict) or not _valid_segment_id(segment.get("id")):
                    return False, True, {}, {}, "A revisão de transcrição contém um segmento inválido."
                ids.add(segment["id"])
                track = segment.get("track")
                start, end = segment.get("start"), segment.get("end")
                if (
                    track in _TRACK_ORDER
                    and isinstance(start, (int, float)) and not isinstance(start, bool)
                    and isinstance(end, (int, float)) and not isinstance(end, bool)
                    and math.isfinite(float(start)) and math.isfinite(float(end))
                    and 0 <= float(start) <= float(end)
                ):
                    ranges.append((track, float(start), float(end)))
                total_bytes += _json_size(segment)
                if total_bytes > MAX_TRANSCRIPT_BYTES:
                    return False, True, {}, {}, "As transcrições excedem o limite de retenção."
            # Keep every revision's citation scope separate.  Segment IDs and
            # timestamps are only stable within their transcript revision.
            revision_ids[revision_id] = ids
            revision_ranges[revision_id] = tuple(ranges)
        return True, True, revision_ids, revision_ranges, ""

    def _transcript_snapshot(self, session_id, metadata):
        """Read the completed revisions used by the raw-retention gate."""
        revisions = metadata.get("revisions")
        if not isinstance(revisions, list):
            raise RetentionError("As revisões de transcrição são inválidas.")
        reader = getattr(self.library, "get_transcript", None) if self.library is not None else None
        if reader is None:
            reader = getattr(self.store, "get_transcript", None)
        if reader is None:
            raise RetentionError("O colaborador não oferece leitura de transcrições.")
        snapshot = []
        for revision in revisions:
            if not isinstance(revision, dict) or revision.get("status") != "completed":
                continue
            revision_id = revision.get("id")
            if not _valid_id(revision_id):
                raise RetentionError("Uma revisão concluída tem um identificador inválido.")
            try:
                segments = list(reader(session_id, revision_id))
            except Exception as error:
                raise RetentionError(f"A revisão de transcrição não pôde ser lida: {error}") from error
            snapshot.append({"id": revision_id, "segments": segments})
        return snapshot

    def _eligibility_fingerprint(self, session_id, metadata, annotations):
        """Digest every canonical input that can make raw removal eligible."""
        return _json_fingerprint(
            {
                "metadata": metadata,
                "annotations": annotations,
                "reports": list(self._report_sources(session_id)),
                "transcripts": self._transcript_snapshot(session_id, metadata),
            }
        )

    def _review_exists(self, metadata, annotations):
        if isinstance(metadata.get("reviewed_summary"), str) and metadata["reviewed_summary"].strip():
            return True
        if isinstance(annotations.get("reviewed_summary"), str) and annotations["reviewed_summary"].strip():
            return True
        if isinstance(annotations.get("reviewed_artifacts"), dict) and annotations["reviewed_artifacts"]:
            return True
        if self.library is not None:
            lister = getattr(self.library, "list_reports", None)
            if callable(lister):
                try:
                    reports = lister(metadata.get("id"))
                except Exception:
                    reports = ()
                if any(
                    isinstance(report, dict)
                    and (report.get("reviewed") or report.get("reviewed_artifact") or report.get("reviewed_sections"))
                    for report in reports
                ):
                    return True
        return False

    def _report_sources(self, session_id):
        if self.library is None:
            return ()
        lister = getattr(self.library, "list_reports", None)
        getter = getattr(self.library, "get_report", None)
        if not callable(lister):
            return ()
        try:
            reports = lister(session_id)
        except Exception:
            return ()
        sources = []
        for report in reports or ():
            if not isinstance(report, dict):
                continue
            if callable(getter) and report.get("id") and not report.get("virtual"):
                try:
                    report = getter(session_id, report["id"])
                except Exception:
                    pass
            sources.append(report)
        return tuple(sources)

    @staticmethod
    def _iter_citations(value, key=""):
        if isinstance(value, dict):
            for name, child in value.items():
                if name.casefold() in {"citations", "citation", "segment_id", "segment_ids", "source_segment", "source_segments"}:
                    yield child
                yield from MeetingRetention._iter_citations(child, name)
        elif isinstance(value, list):
            for child in value:
                yield from MeetingRetention._iter_citations(child, key)

    def _citations_resolve(self, metadata, annotations, revision_ids, revision_ranges):
        sources = []
        summary = metadata.get("summary")
        if summary is not None:
            sources.append((summary, "legacy"))
        report_sources = self._report_sources(metadata.get("id"))
        report_ids = set()
        for report in report_sources:
            if not isinstance(report, dict) or report.get("virtual"):
                # The virtual legacy report is represented by metadata.summary,
                # which is handled with its legacy revision rules above.
                continue
            report_ids.add(report.get("id"))
            sources.append((report, "report"))

        # A detached reviewed artifact has no provenance of its own.  Only
        # report-attached artifacts can be resolved; an orphan with citations
        # must fail closed instead of falling back to a union of revisions.
        reviewed_artifacts = annotations.get("reviewed_artifacts")
        if isinstance(reviewed_artifacts, dict):
            for report_id, artifact in reviewed_artifacts.items():
                if report_id not in report_ids and any(self._iter_citations(artifact)):
                    return False, "Uma citação revisada não tem uma revisão de transcrição resolvível."

        if not revision_ids:
            return False, "Não há segmentos para resolver as citações preservadas."
        for source, source_kind in sources:
            citations = list(self._iter_citations(source))
            if not citations:
                continue
            if source_kind == "legacy":
                if not isinstance(source, dict):
                    return False, "Uma citação legada não tem uma revisão de transcrição resolvível."
                revision_id = source.get("transcript_revision", source.get("revision"))
                if revision_id is None:
                    # A legacy summary can be safely inferred only while the
                    # meeting has exactly one completed transcript revision.
                    if len(revision_ids) != 1:
                        return False, "Uma citação legada não informa a revisão de transcrição."
                    revision_id = next(iter(revision_ids))
            else:
                revision_id = source.get("transcript_revision") if isinstance(source, dict) else None
                if revision_id not in revision_ids:
                    return False, "Uma citação do relatório não informa uma revisão de transcrição concluída."
            segment_ids = revision_ids.get(revision_id)
            segment_ranges = revision_ranges.get(revision_id)
            if segment_ids is None or segment_ranges is None:
                return False, "Uma citação referencia uma revisão de transcrição inexistente ou não concluída."
            for citation in citations:
                values = citation if isinstance(citation, list) else [citation]
                for item in values:
                    if isinstance(item, dict):
                        cited_revision = item.get("transcript_revision", item.get("revision"))
                        if cited_revision is not None and cited_revision != revision_id:
                            return False, "Uma citação do relatório referencia outra revisão de transcrição."
                        candidate = item.get("segment_id", item.get("id"))
                        if candidate is None or candidate not in segment_ids:
                            return False, "Uma citação do relatório não pode mais ser resolvida."
                    elif isinstance(item, str) and item and item not in segment_ids:
                        # Existing v1 summaries used track:start:end citations.
                        # They remain resolvable as provenance while the
                        # transcript revision survives, even if raw audio does not.
                        pieces = item.split(":")
                        if len(pieces) != 3 or pieces[0] not in _TRACK_ORDER:
                            return False, "Uma citação do relatório não pode mais ser resolvida."
                        try:
                            start, end = float(pieces[1]), float(pieces[2])
                        except ValueError:
                            return False, "Uma citação do relatório não pode mais ser resolvida."
                        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start:
                            return False, "Uma citação do relatório não pode mais ser resolvida."
                        if not any(
                            track == pieces[0] and start <= segment_end and end >= segment_start
                            for track, segment_start, segment_end in segment_ranges
                        ):
                            return False, "Uma citação do relatório não pode mais ser resolvida."
        return True, ""

    def plan(self, session_id, policy=None, *, tracks=None, override=None):
        """Return an immutable, non-mutating plan for one meeting."""
        source = self._session_path(session_id)
        metadata = self._metadata(session_id)
        now = self._now()
        resolved = self._resolve_policy(policy, session_id, metadata, override)
        if tracks is not None:
            resolved = RetentionPolicy(
                resolved.mode,
                resolved.after_days,
                tuple(tracks),
                resolved.purge_after_days,
                resolved.override,
            )
        reasons = []
        missing = []
        targets = []
        raw_tracks = ()
        operation = resolved.mode
        byte_estimate = 0
        recovery_mode = "none"
        canonical = ()
        capabilities = ()
        eligibility_fingerprint = ""
        eligible = True
        active, active_reason = self._active_lease(session_id, metadata)
        if active:
            reasons.append(active_reason)
            eligible = False
        age, age_reason = self._meeting_age(metadata, now)
        if age_reason and resolved.mode != "keep":
            reasons.append(age_reason)
            eligible = False
        if resolved.mode == "keep":
            operation = "keep"
        elif metadata.get("status") == "recording":
            reasons.append("A reunião está em gravação e não pode entrar na retenção.")
            eligible = False
        elif age is None or resolved.after_days is None or age < resolved.after_days:
            if not age_reason:
                reasons.append("A idade da reunião ainda não atingiu a política de retenção.")
            eligible = False
        elif resolved.mode == "whole_meeting":
            targets, byte_estimate = self._tree_inventory(source, kind="meeting")
            operation = "whole_meeting"
            recovery_mode = "same-root-trash"
            canonical = ("move the canonical meeting bundle to same-root app trash", "remove the session from the disposable index")
            capabilities = ("meeting access until restore",)
        else:
            operation = "raw_tracks"
            recovery_mode = "operation-journal-rollback-before-canonical-commit"
            canonical = ("mark selected raw tracks unavailable in canonical metadata", "reconcile the disposable index after the canonical commit")
            capabilities = ("playback of removed tracks", "retranscription from removed tracks", "new clips from removed tracks", "audio re-export of removed tracks")
            if metadata.get("status") not in _RAW_ELIGIBLE_STATUSES:
                reasons.append("Raw-audio removal requires a completed or partial meeting, not an interrupted/failed state.")
                eligible = False
            annotations = self._annotations(session_id, metadata)
            complete, _has_revision, revision_ids, revision_ranges, transcript_reason = self._transcript_state(
                session_id, metadata,
            )
            if not complete:
                reasons.append(transcript_reason)
                eligible = False
            if not self._review_exists(metadata, annotations):
                reasons.append("Raw-audio removal waits for a reviewed transcript or report.")
                eligible = False
            citations_ok, citation_reason = self._citations_resolve(
                metadata, annotations, revision_ids, revision_ranges,
            )
            if not citations_ok:
                reasons.append(citation_reason)
                eligible = False
            try:
                eligibility_fingerprint = self._eligibility_fingerprint(session_id, metadata, annotations)
            except RetentionError as error:
                reasons.append(str(error))
                eligible = False
            requested = tuple(resolved.tracks) or tuple(track for track in _TRACK_ORDER if track in metadata.get("tracks", {}))
            requested = tuple(dict.fromkeys(requested))
            if not requested:
                reasons.append("A reunião não contém nenhuma fonte raw selecionável.")
                eligible = False
            present_tracks = []
            for track in requested:
                track_dir = os.path.join(source, track)
                if not os.path.lexists(track_dir):
                    missing.append(track_dir)
                    continue
                track_targets, track_bytes = self._tree_inventory(track_dir, kind="raw_track", track=track)
                if metadata.get("tracks", {}).get(track, {}).get("available") is False:
                    reasons.append(f"A fonte {track} já está indisponível.")
                    continue
                targets.extend(track_targets)
                present_tracks.append(track)
            raw_tracks = tuple(present_tracks)
            if not targets:
                reasons.append("Nenhum alvo raw existente foi encontrado.")
                eligible = False
            byte_estimate = sum(target.bytes for target in targets)
        if missing:
            # A missing selected track is informational when another selected
            # track is present, but never claim that its bytes were removed.
            if not targets:
                eligible = False
            reasons.append("Alguns alvos raw já não existem; eles foram excluídos do inventário.")
        else:
            byte_estimate = sum(target.bytes for target in targets)
        if operation == "whole_meeting" and not targets:
            eligible = False
            reasons.append("O inventário da reunião não contém um alvo seguro.")
        if operation == "whole_meeting" and metadata.get("status") not in _TERMINAL_STATUSES:
            eligible = False
            reasons.append("A reunião não está em um estado final.")
        external = self._external_exports(metadata)
        if operation == "keep":
            eligible = True
            reasons = []
        fingerprint = self._fingerprint(targets)
        return RetentionPlan(
            session_id=session_id,
            operation=operation,
            planned_at=_stamp(now),
            policy=resolved,
            eligible=bool(eligible),
            reasons=tuple(dict.fromkeys(reasons)),
            targets=tuple(targets),
            byte_estimate=byte_estimate,
            canonical_changes=canonical,
            lost_capabilities=capabilities,
            excluded_external_exports=external,
            missing_paths=tuple(missing),
            recovery_mode=recovery_mode,
            inventory_fingerprint=fingerprint,
            eligibility_fingerprint=eligibility_fingerprint,
            source_path=source,
            meetings_root=self.meetings_root,
            trash_root=self.trash_root,
            raw_tracks=raw_tracks,
        )

    # -- Journal and common mutation helpers ----------------------------

    def _operation_id(self):
        return self._now().strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:16]

    def _journal_path(self, operation_id):
        if not _valid_id(operation_id):
            raise RetentionSafetyError("O identificador da operação é inválido.")
        return self._owned_path(
            os.path.join(self.retention_ops_root, operation_id + OPERATION_JOURNAL_SUFFIX),
            self.retention_ops_root,
            "journal",
        )

    def _append_journal(self, path, record):
        path = self._owned_path(path, self.retention_ops_root, "journal")
        os.makedirs(self.retention_ops_root, exist_ok=True)
        if _has_link_component(self.retention_ops_root) or (_is_link(path) if os.path.lexists(path) else False):
            raise RetentionSafetyError("O journal aponta para um link ou junction.")
        encoded = json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(encoded) > MAX_JOURNAL_LINE_BYTES:
            raise RetentionError("O journal de retenção excede o limite permitido.")
        with open(path, "ab", buffering=0) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())

    def _read_journal(self, path):
        records = []
        try:
            with open(path, "rb") as handle:
                for line in handle:
                    if len(line) > MAX_JOURNAL_LINE_BYTES:
                        break
                    try:
                        value = json.loads(line.decode("utf-8"))
                    except (UnicodeError, ValueError, TypeError):
                        break
                    if isinstance(value, dict) and isinstance(value.get("state"), str):
                        records.append(value)
        except OSError:
            return ()
        return tuple(records)

    def _fail(self, stage):
        if self.failure_injector is not None:
            self.failure_injector(stage)

    def _mkdir_owned(self, path, root):
        path = self._owned_path(path, root, "directory")
        os.makedirs(path, exist_ok=True)
        if _has_link_component(path) or not os.path.isdir(path):
            raise RetentionSafetyError("A pasta de retenção não é segura.")
        return path

    def _ensure_plan(self, plan):
        if not isinstance(plan, RetentionPlan):
            raise TypeError("A operação de retenção exige um RetentionPlan.")
        if not plan.dry_run or plan.meetings_root != self.meetings_root or plan.trash_root != self.trash_root:
            raise PlanConflict("O plano não pertence a esta biblioteca.")
        if not plan.eligible:
            joined = " ".join(plan.reasons)
            if "lease" in joined.casefold() or "gravação" in joined.casefold():
                raise ActiveLeaseError(joined)
            if "link" in joined.casefold() or "app-owned" in joined.casefold():
                raise RetentionSafetyError(joined)
            raise RetentionError(joined or "O plano de retenção não é elegível.")
        source = self._session_path(plan.session_id)
        current_targets, _ = self._reinventory_plan(plan, source)
        if self._fingerprint(current_targets) != plan.inventory_fingerprint:
            raise PlanConflict("O inventário mudou depois da prévia de retenção.")
        current_metadata = self._metadata(plan.session_id)
        active, reason = self._active_lease(plan.session_id, current_metadata)
        if active:
            raise ActiveLeaseError(reason)
        # Inventory equality alone does not preserve the approved eligibility
        # boundary: metadata can change status or age without changing file
        # lengths.  Re-run the exact approved policy before any mutation.
        current = self.plan(
            plan.session_id,
            plan.policy,
            tracks=plan.raw_tracks if plan.operation == "raw_tracks" else None,
        )
        if (
            not current.eligible
            or current.operation != plan.operation
            or (plan.operation == "whole_meeting" and current.policy != plan.policy)
        ):
            joined = " ".join(current.reasons)
            if "lease" in joined.casefold() or "gravação" in joined.casefold():
                raise ActiveLeaseError(joined)
            raise PlanConflict("A elegibilidade mudou depois da prévia de retenção.")
        if plan.operation == "raw_tracks":
            # Eligibility is a compound promise over annotations, completed
            # transcript revisions, reviewed report provenance, and citations.
            # Re-run the gate immediately before moving bytes; an inventory
            # fingerprint alone cannot detect a stale review or citation.
            if (
                current.operation != plan.operation
                or current.raw_tracks != plan.raw_tracks
                or not plan.eligibility_fingerprint
                or current.eligibility_fingerprint != plan.eligibility_fingerprint
            ):
                raise PlanConflict("A revisão, a proveniência ou as citações mudaram depois da prévia de retenção.")
        return source

    def _reinventory_plan(self, plan, source):
        if plan.operation == "whole_meeting":
            return self._tree_inventory(source, kind="meeting")
        if plan.operation == "raw_tracks":
            values = []
            for track in plan.raw_tracks:
                path = os.path.join(source, track)
                if os.path.lexists(path):
                    targets, _ = self._tree_inventory(path, kind="raw_track", track=track)
                    values.extend(targets)
            return tuple(values), sum(target.bytes for target in values)
        return (), 0

    @staticmethod
    def _confirmation(confirm, *, permanent=False):
        if not confirm:
            raise ConfirmationRequired("Confirme explicitamente a operação de retenção.")
        if permanent and confirm not in {True, "permanent", "purge", "permanente"}:
            raise ConfirmationRequired("A exclusão permanente exige uma segunda confirmação explícita.")

    def _mark_index_stale(self, reason="retention projection failed"):
        """Leave a durable stale/rebuild signal after a projection failure."""
        if self.library is None:
            return
        for name, args in (
            ("_mark_index_stale_with_reason", (reason,)),
            ("_mark_index_stale", ()),
        ):
            marker = getattr(self.library, name, None)
            if not callable(marker):
                continue
            try:
                marker(*args)
            except TypeError:
                if args:
                    try:
                        marker()
                    except Exception:
                        pass
            except Exception:
                pass
            return
        index = getattr(self.library, "_index", None)
        if index is None and os.path.lexists(os.path.join(self.home_root, "library.sqlite")):
            try:
                index = getattr(self.library, "index", None)
            except Exception:
                index = None
        marker = getattr(index, "mark_stale", None) if index is not None else None
        if callable(marker):
            try:
                marker(reason)
            except Exception:
                pass

    def _project(self, session_id, *, removed=False, restored=False):
        """Best-effort disposable-index update after a canonical commit."""
        if self.library is None:
            return None
        candidates = (
            ("on_session_trashed", (session_id,)),
            ("on_session_deleted", (session_id,)),
            ("project_deleted", (session_id,)),
        ) if removed else (
            ("on_session_restored", (session_id,)),
            ("on_session_added", (session_id,)),
            ("project_session", (session_id,)),
        ) if restored else (
            ("on_raw_tracks_removed", (session_id,)),
            ("project_session", (session_id,)),
        )
        for name, args in candidates:
            method = getattr(self.library, name, None)
            if not callable(method):
                continue
            try:
                result = bool(method(*args))
            except Exception:
                self._mark_index_stale()
                return False
            if not result:
                self._mark_index_stale()
            return result
        # Do not create a disposable SQLite catalog merely to delete a
        # session that was never indexed.  Existing adapters can expose an
        # already-open ``_index`` or an explicit deletion hook above.
        index = getattr(self.library, "_index", None)
        if index is None and os.path.lexists(os.path.join(self.home_root, "library.sqlite")):
            index = getattr(self.library, "index", None)
        if removed and index is not None:
            remover = getattr(index, "remove_session", None)
            if callable(remover):
                try:
                    result = bool(remover(session_id))
                except Exception:
                    self._mark_index_stale()
                    return False
                if not result:
                    self._mark_index_stale()
                return result
        return None

    # -- Whole meeting trash --------------------------------------------

    def _tombstone_path(self, entry_path):
        return os.path.abspath(os.fspath(entry_path)) + TRASH_TOMBSTONE_SUFFIX

    def _tombstone_payload(self, operation_id, session_id, entry_path, byte_estimate, now, purge_after):
        relative = os.path.relpath(entry_path, self.trash_root).replace(os.sep, "/")
        return {
            "schema_version": RETENTION_SCHEMA_VERSION,
            "kind": "whole_meeting_trash",
            "operation_id": operation_id,
            "session_id": session_id,
            "original_session_id": session_id,
            "original_relative": os.path.relpath(self._session_path(session_id), self.home_root).replace(os.sep, "/"),
            "trash_relative": relative,
            "deleted_at": _stamp(now),
            "purge_after": _stamp(purge_after),
            "byte_estimate": int(byte_estimate),
        }

    def _write_tombstone(self, path, value):
        path = self._owned_path(path, self.trash_root, "tombstone")
        write_json_atomic(path, value)

    def _validate_tombstone(self, value, path):
        if not isinstance(value, dict) or value.get("schema_version") != RETENTION_SCHEMA_VERSION or value.get("kind") != "whole_meeting_trash":
            raise RetentionError("O tombstone da lixeira não é reconhecido.")
        session_id = value.get("session_id")
        operation_id = value.get("operation_id")
        if not _valid_id(session_id) or not _valid_id(operation_id):
            raise RetentionSafetyError("O tombstone tem identificadores inválidos.")
        relative = value.get("trash_relative")
        if not _valid_relative(relative):
            raise RetentionSafetyError("O tombstone não tem um caminho relativo seguro.")
        expected = os.path.abspath(os.path.join(self.trash_root, relative))
        if expected != os.path.abspath(path) or not _common(self.trash_root, expected):
            raise RetentionSafetyError("O tombstone aponta para fora da lixeira.")
        if _has_link_component(expected):
            raise RetentionSafetyError("O alvo da lixeira contém um link ou junction.")
        original_relative = value.get("original_relative")
        if not _valid_relative(original_relative):
            raise RetentionSafetyError("O tombstone não identifica uma origem relativa segura.")
        expected_original = os.path.relpath(self._session_path(session_id), self.home_root).replace(os.sep, "/")
        if original_relative != expected_original:
            raise RetentionSafetyError("O tombstone aponta para uma origem inesperada.")
        if _parse_stamp(value.get("deleted_at")) is None or _parse_stamp(value.get("purge_after")) is None:
            raise RetentionError("O tombstone não contém prazos UTC válidos.")
        byte_estimate = value.get("byte_estimate")
        if isinstance(byte_estimate, bool) or not isinstance(byte_estimate, int) or byte_estimate < 0:
            raise RetentionError("O tombstone contém um tamanho inválido.")
        return value

    def _entry_from_tombstone(self, tombstone_path):
        tombstone_path = self._owned_path(tombstone_path, self.trash_root, "tombstone", must_exist=True)
        if not tombstone_path.endswith(TRASH_TOMBSTONE_SUFFIX):
            raise RetentionSafetyError("O tombstone tem um nome inválido.")
        with open(tombstone_path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
        entry_path = tombstone_path[: -len(TRASH_TOMBSTONE_SUFFIX)]
        self._validate_tombstone(value, entry_path)
        if not os.path.lexists(entry_path):
            raise RetentionError("O tombstone não tem um alvo de lixeira correspondente.")
        if _is_link(entry_path):
            raise RetentionSafetyError("O alvo da lixeira é um link ou junction.")
        return TrashEntry(
            value["session_id"], value["operation_id"], entry_path, tombstone_path,
            value.get("deleted_at", ""), value.get("purge_after", ""), int(value.get("byte_estimate", 0)),
        )

    def list_trash(self):
        if not os.path.lexists(self.trash_root):
            return ()
        if _has_link_component(self.trash_root):
            raise RetentionSafetyError("A lixeira contém um link ou junction.")
        entries = []
        try:
            values = sorted(os.scandir(self.trash_root), key=lambda item: item.name.casefold())
        except OSError as error:
            raise RetentionError(f"Não foi possível listar a lixeira: {error}") from error
        for item in values:
            if item.name == ".retention-ops":
                if _is_link(item.path) or not item.is_dir(follow_symlinks=False):
                    raise RetentionSafetyError("A área de staging raw da lixeira não é uma pasta segura.")
                continue
            if not item.name.endswith(TRASH_TOMBSTONE_SUFFIX):
                paired_tombstone = item.path + TRASH_TOMBSTONE_SUFFIX
                if item.is_dir(follow_symlinks=False) and os.path.lexists(paired_tombstone):
                    if _is_link(paired_tombstone) or not os.path.isfile(paired_tombstone):
                        raise RetentionSafetyError("O tombstone da lixeira não é um arquivo regular.")
                    self._entry_from_tombstone(paired_tombstone)
                    continue
                raise RetentionError(
                    "A lixeira contém um alvo sem tombstone; execute a reconciliação antes de continuar."
                )
            if not item.is_file(follow_symlinks=False):
                raise RetentionSafetyError("O tombstone da lixeira não é um arquivo regular.")
            if _is_link(item.path):
                raise RetentionSafetyError("A lixeira contém um tombstone linkado.")
            entries.append(self._entry_from_tombstone(item.path))
        return tuple(entries)

    def _find_trash(self, session_id):
        matches = [entry for entry in self.list_trash() if entry.session_id == session_id]
        if not matches:
            raise FileNotFoundError(f"A reunião {session_id} não está na lixeira.")
        if len(matches) > 1:
            raise RetentionError("Há mais de um tombstone para a mesma reunião; reconcilie a lixeira.")
        return matches[0]

    def _safe_tree(self, root):
        self._tree_inventory(root, kind="trash")

    def _remove_tree(self, root):
        root = self._owned_path(root, self.trash_root, "trash target", must_exist=True)
        self._safe_tree(root)
        for current, directories, files in os.walk(root, topdown=False, followlinks=False):
            for name in files:
                path = self._owned_path(os.path.join(current, name), self.trash_root, "trash file", must_exist=True)
                if _is_link(path) or not stat.S_ISREG(os.stat(path, follow_symlinks=False).st_mode):
                    raise RetentionSafetyError("A lixeira contém um arquivo especial ou link.")
                os.unlink(path)
            for name in directories:
                path = self._owned_path(os.path.join(current, name), self.trash_root, "trash directory", must_exist=True)
                if _is_link(path):
                    raise RetentionSafetyError("A lixeira contém um link ou junction.")
                os.rmdir(path)
        os.rmdir(root)

    def _trash(self, plan):
        source = self._ensure_plan(plan)
        operation_id = self._operation_id()
        entry_path = os.path.join(self.trash_root, f"{plan.session_id}--{operation_id}")
        tombstone_path = self._tombstone_path(entry_path)
        self._mkdir_owned(self.trash_root, self.home_root)
        if os.path.lexists(entry_path) or os.path.lexists(tombstone_path):
            raise PlanConflict("O destino da lixeira já existe; gere uma nova prévia.")
        journal_path = self._journal_path(operation_id)
        now = self._now()
        purge_after = now + timedelta(days=plan.policy.purge_after_days)
        base = {
            "schema_version": RETENTION_SCHEMA_VERSION,
            "operation": "whole_meeting",
            "operation_id": operation_id,
            "session_id": plan.session_id,
            "source_relative": os.path.relpath(source, self.home_root).replace(os.sep, "/"),
            "trash_relative": os.path.relpath(entry_path, self.trash_root).replace(os.sep, "/"),
            "tombstone_relative": os.path.relpath(tombstone_path, self.trash_root).replace(os.sep, "/"),
            "byte_estimate": plan.byte_estimate,
            "purge_after": _stamp(purge_after),
        }
        self._append_journal(journal_path, {**base, "state": "prepared", "updated_at": _stamp(now)})
        self._fail("whole.prepared")
        os.replace(source, entry_path)
        self._fail("whole.moved")
        tombstone = self._tombstone_payload(operation_id, plan.session_id, entry_path, plan.byte_estimate, now, purge_after)
        self._write_tombstone(tombstone_path, tombstone)
        self._append_journal(journal_path, {**base, "state": "tombstoned", "updated_at": _stamp(self._now())})
        self._fail("whole.tombstoned")
        index_updated = self._project(plan.session_id, removed=True)
        projection_state = "projected" if index_updated is not False else "tombstoned"
        self._append_journal(
            journal_path,
            {
                **base,
                "state": projection_state,
                "index_updated": index_updated,
                "projection_pending": index_updated is False,
                "updated_at": _stamp(self._now()),
            },
        )
        return RetentionResult(operation_id, plan.session_id, "trashed", plan.byte_estimate, plan.target_paths, plan.lost_capabilities, index_updated)

    def restore(self, session_id):
        with self._lock:
            with self._writer_lock():
                return self._restore_locked(session_id)

    def _restore_locked(self, session_id):
        if not _valid_id(session_id):
            raise ValueError("Identificador de reunião inválido.")
        entry = self._find_trash(session_id)
        active, reason = self._active_lease(session_id, {"status": "trashed"})
        if active:
            raise ActiveLeaseError(reason)
        source = self._session_path(session_id)
        if os.path.lexists(source):
            raise PlanConflict("A pasta original da reunião já existe; restauração não sobrescreve arquivos.")
        self._safe_tree(entry.path)
        journal_base = {
            "operation": "whole_meeting",
            "operation_id": entry.operation_id,
            "session_id": session_id,
            "source_relative": os.path.relpath(source, self.home_root).replace(os.sep, "/"),
            "trash_relative": os.path.relpath(entry.path, self.trash_root).replace(os.sep, "/"),
            "tombstone_relative": os.path.relpath(entry.tombstone_path, self.trash_root).replace(os.sep, "/"),
            "byte_estimate": entry.byte_estimate,
            "purge_after": entry.purge_after,
        }
        journal_path = self._journal_path(entry.operation_id)
        self._append_journal(journal_path, {**journal_base, "state": "restore_prepared", "updated_at": _stamp(self._now())})
        self._fail("whole.restore_prepared")
        self._mkdir_owned(self.meetings_root, self.home_root)
        os.replace(entry.path, source)
        self._fail("whole.restored")
        os.unlink(entry.tombstone_path)
        index_updated = self._project(session_id, restored=True)
        self._append_journal(journal_path, {**journal_base, "state": "restored", "index_updated": index_updated, "updated_at": _stamp(self._now())})
        return RetentionResult(entry.operation_id, session_id, "restored", entry.byte_estimate, (source,), (), index_updated)

    def purge(self, session_id, *, confirm=False):
        with self._lock:
            with self._writer_lock():
                return self._purge_locked(session_id, confirm=confirm)

    def _purge_locked(self, session_id, *, confirm=False):
        self._confirmation(confirm, permanent=True)
        entry = self._find_trash(session_id)
        active, reason = self._active_lease(session_id, {"status": "trashed"})
        if active:
            raise ActiveLeaseError(reason)
        journal_path = self._journal_path(entry.operation_id)
        journal_base = {
            "operation": "whole_meeting",
            "operation_id": entry.operation_id,
            "session_id": session_id,
            "source_relative": os.path.relpath(self._session_path(session_id), self.home_root).replace(os.sep, "/"),
            "trash_relative": os.path.relpath(entry.path, self.trash_root).replace(os.sep, "/"),
            "tombstone_relative": os.path.relpath(entry.tombstone_path, self.trash_root).replace(os.sep, "/"),
            "byte_estimate": entry.byte_estimate,
            "purge_after": entry.purge_after,
        }
        self._append_journal(journal_path, {**journal_base, "state": "purge_prepared", "updated_at": _stamp(self._now())})
        self._fail("whole.purge_prepared")
        self._remove_tree(entry.path)
        self._fail("whole.purged")
        if os.path.lexists(entry.tombstone_path):
            if _is_link(entry.tombstone_path):
                raise RetentionSafetyError("O tombstone da lixeira está linkado.")
            os.unlink(entry.tombstone_path)
        self._append_journal(journal_path, {**journal_base, "state": "purged", "updated_at": _stamp(self._now())})
        return RetentionResult(entry.operation_id, session_id, "purged", entry.byte_estimate, (), ("permanent deletion; restore is no longer available",), None)

    def empty_trash(self, *, confirm=False):
        with self._lock:
            with self._writer_lock():
                return self._empty_trash_locked(confirm=confirm)

    def _empty_trash_locked(self, *, confirm=False):
        self._confirmation(confirm, permanent=True)
        now = self._now()
        results = []
        for entry in self.list_trash():
            deadline = _parse_stamp(entry.purge_after)
            if deadline is not None and deadline <= now:
                results.append(self._purge_locked(entry.session_id, confirm=True))
        return tuple(results)

    # -- Raw-track operation journal ------------------------------------

    def _canonical_metadata_path(self, session_id):
        return self._owned_path(os.path.join(self._session_path(session_id), "metadata.json"), self.meetings_root, "metadata")

    def _read_canonical_metadata(self, session_id):
        path = self._canonical_metadata_path(session_id)
        if _is_link(path) or not os.path.isfile(path):
            raise RetentionSafetyError("Os metadados canônicos não estão seguros.")
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
        if not isinstance(value, dict) or value.get("id") != session_id:
            raise RetentionError("Os metadados canônicos não correspondem à reunião.")
        return value

    def _raw_journal_payload(self, plan, operation_id, staged_root):
        original = self._read_canonical_metadata(plan.session_id)
        tracks = {}
        source_relative = {}
        staged_relative = {}
        for track in plan.raw_tracks:
            value = copy.deepcopy(original.get("tracks", {}).get(track, {}))
            tracks[track] = value
            source_relative[track] = os.path.relpath(os.path.join(plan.source_path, track), self.home_root).replace(os.sep, "/")
            staged_relative[track] = os.path.relpath(os.path.join(staged_root, track), self.home_root).replace(os.sep, "/")
        return {
            "schema_version": RETENTION_SCHEMA_VERSION,
            "operation": "raw_tracks",
            "operation_id": operation_id,
            "session_id": plan.session_id,
            "tracks": list(plan.raw_tracks),
            "source_relative": source_relative,
            "staged_relative": staged_relative,
            "original_tracks": tracks,
            "original_retention_marker": copy.deepcopy(original.get("raw_retention_operation")),
            "metadata_before_fingerprint": _json_fingerprint(original),
            "eligibility_fingerprint": plan.eligibility_fingerprint,
            "inventory_fingerprint": plan.inventory_fingerprint,
            "byte_estimate": plan.byte_estimate,
            "state": "prepared",
            "updated_at": _stamp(self._now()),
        }

    def _path_from_relative(self, relative, root):
        path = os.path.abspath(os.path.join(root, relative))
        return self._owned_path(path, root, "operation target")

    def _mark_raw_unavailable(
        self,
        session_id,
        tracks,
        *,
        restore=False,
        original=None,
        original_marker=None,
        operation_id=None,
        eligibility_fingerprint=None,
        inventory_fingerprint=None,
        metadata_before_fingerprint=None,
    ):
        metadata = self._read_canonical_metadata(session_id)
        metadata_tracks = metadata.setdefault("tracks", {})
        if restore:
            for track in tracks:
                if original and track in original:
                    metadata_tracks[track] = copy.deepcopy(original[track])
                elif track in metadata_tracks:
                    value = metadata_tracks[track]
                    if isinstance(value, dict):
                        value.pop("available", None)
                        value.pop("raw_removed", None)
                        value.pop("purged_at", None)
                        for segment in value.get("segments", []):
                            if isinstance(segment, dict):
                                segment.pop("available", None)
                                segment.pop("raw_removed", None)
            removed = set(metadata.get("raw_tracks_removed", []))
            removed.difference_update(tracks)
            if removed:
                metadata["raw_tracks_removed"] = sorted(removed)
            else:
                metadata.pop("raw_tracks_removed", None)
            if original_marker is not None:
                metadata["raw_retention_operation"] = copy.deepcopy(original_marker)
            else:
                metadata.pop("raw_retention_operation", None)
        else:
            when = _stamp(self._now())
            for track in tracks:
                value = metadata_tracks.get(track)
                if not isinstance(value, dict):
                    value = {}
                    metadata_tracks[track] = value
                value["available"] = False
                value["raw_removed"] = True
                value["purged_at"] = when
                for segment in value.get("segments", []):
                    if isinstance(segment, dict):
                        segment["available"] = False
                        segment["raw_removed"] = True
            removed = set(metadata.get("raw_tracks_removed", []))
            removed.update(tracks)
            metadata["raw_tracks_removed"] = sorted(removed)
            if not _valid_id(operation_id):
                raise OperationRecoveryError("A marca da operação raw não tem um identificador seguro.")
            metadata["raw_retention_operation"] = {
                "operation_id": operation_id,
                "tracks": sorted(set(tracks)),
                "eligibility_fingerprint": eligibility_fingerprint or "",
                "inventory_fingerprint": inventory_fingerprint or "",
                "metadata_before_fingerprint": metadata_before_fingerprint or "",
                "state": "committed",
            }
        metadata["updated_at"] = _stamp(self._now())
        write_json_atomic(self._canonical_metadata_path(session_id), metadata)

    @staticmethod
    def _raw_commit_matches(metadata, latest):
        marker = metadata.get("raw_retention_operation")
        if not isinstance(marker, dict):
            return False
        expected_tracks = sorted(set(latest.get("staged_tracks", latest.get("tracks", ()))))
        return (
            marker.get("state") == "committed"
            and marker.get("operation_id") == latest.get("operation_id")
            and marker.get("tracks") == expected_tracks
            and marker.get("eligibility_fingerprint") == latest.get("eligibility_fingerprint")
            and marker.get("inventory_fingerprint") == latest.get("inventory_fingerprint")
            and marker.get("metadata_before_fingerprint") == latest.get("metadata_before_fingerprint")
        )

    def _rollback_raw(self, latest):
        session_id = latest["session_id"]
        operation_id = latest["operation_id"]
        journal_path = self._journal_path(operation_id)
        metadata = self._read_canonical_metadata(session_id)
        if self._raw_commit_matches(metadata, latest):
            raise OperationRecoveryError("O commit raw já foi provado; finalize a operação em vez de desfazê-la.")
        if _json_fingerprint(metadata) != latest.get("metadata_before_fingerprint"):
            raise OperationRecoveryError("O metadata mudou antes do commit raw; rollback automático bloqueado.")
        tracks = tuple(latest.get("tracks", ()))
        restored = []
        for track in tracks:
            source = self._path_from_relative(latest["source_relative"][track], self.home_root)
            staged = self._path_from_relative(latest["staged_relative"][track], self.home_root)
            if os.path.lexists(staged):
                if os.path.lexists(source):
                    raise OperationRecoveryError("Não é seguro desfazer o raw purge: o alvo original reapareceu.")
                self._safe_tree(staged)
                self._mkdir_owned(os.path.dirname(source), self.meetings_root)
                os.replace(staged, source)
                restored.append(track)
            elif not os.path.lexists(source):
                # A journal can be left between the rename and its state
                # record.  If neither side exists, recovery cannot prove
                # whether bytes were externally removed or only hidden from
                # the journal; never silently declare that operation safe.
                raise OperationRecoveryError(
                    "Não é seguro desfazer o raw purge: falta o alvo original e o staging."
                )
        if restored:
            self._mark_raw_unavailable(
                session_id,
                restored,
                restore=True,
                original=latest.get("original_tracks"),
                original_marker=latest.get("original_retention_marker"),
            )
        self._append_journal(journal_path, {**latest, "state": "rolled_back", "restored_tracks": restored, "updated_at": _stamp(self._now())})
        return RetentionResult(operation_id, session_id, "rolled_back", latest.get("byte_estimate", 0), (), (), None)

    def _finalize_raw(self, latest):
        operation_id = latest["operation_id"]
        session_id = latest["session_id"]
        journal_path = self._journal_path(operation_id)
        for track in latest.get("tracks", ()):
            staged = self._path_from_relative(latest["staged_relative"][track], self.home_root)
            if os.path.lexists(staged):
                self._remove_tree(staged)
        self._append_journal(journal_path, {**latest, "state": "finalized", "updated_at": _stamp(self._now())})
        return RetentionResult(operation_id, session_id, "finalized", latest.get("byte_estimate", 0), (), ("playback", "retranscription", "new clips", "audio re-export"), None)

    def _remove_raw(self, plan):
        source = self._ensure_plan(plan)
        operation_id = self._operation_id()
        staged_root = os.path.join(self.trash_root, ".retention-ops", operation_id, plan.session_id)
        self._mkdir_owned(os.path.dirname(staged_root), self.trash_root)
        self._mkdir_owned(staged_root, self.trash_root)
        journal_path = self._journal_path(operation_id)
        latest = self._raw_journal_payload(plan, operation_id, staged_root)
        self._append_journal(journal_path, latest)
        moved = []
        try:
            for track in plan.raw_tracks:
                source_track = self._path_from_relative(latest["source_relative"][track], self.home_root)
                staged_track = self._path_from_relative(latest["staged_relative"][track], self.home_root)
                if not os.path.lexists(source_track):
                    continue
                self._safe_tree(source_track)
                self._mkdir_owned(os.path.dirname(staged_track), self.trash_root)
                os.replace(source_track, staged_track)
                moved.append(track)
                self._append_journal(journal_path, {**latest, "state": "track_staged", "staged_tracks": moved, "updated_at": _stamp(self._now())})
                self._fail("raw.track_staged")
            latest = {**latest, "state": "staged", "staged_tracks": moved, "updated_at": _stamp(self._now())}
            self._append_journal(journal_path, latest)
            self._fail("raw.staged")
            self._mark_raw_unavailable(
                plan.session_id,
                moved,
                operation_id=operation_id,
                eligibility_fingerprint=plan.eligibility_fingerprint,
                inventory_fingerprint=plan.inventory_fingerprint,
                metadata_before_fingerprint=latest["metadata_before_fingerprint"],
            )
            latest = {**latest, "state": "metadata_committed", "committed_tracks": moved, "updated_at": _stamp(self._now())}
            self._append_journal(journal_path, latest)
            self._fail("raw.metadata_committed")
            index_updated = self._project(plan.session_id, removed=False)
            projection_state = "projected" if index_updated is not False else "metadata_committed"
            latest = {
                **latest,
                "state": projection_state,
                "index_updated": index_updated,
                "projection_pending": index_updated is False,
                "updated_at": _stamp(self._now()),
            }
            self._append_journal(journal_path, latest)
            self._fail("raw.projected")
            if index_updated is False:
                return RetentionResult(
                    operation_id,
                    plan.session_id,
                    "projection_pending",
                    plan.byte_estimate,
                    plan.target_paths,
                    plan.lost_capabilities,
                    index_updated,
                )
            result = self._finalize_raw(latest)
            return RetentionResult(result.operation_id, result.session_id, result.state, result.byte_estimate, plan.target_paths, plan.lost_capabilities, index_updated)
        except Exception:
            current = self._read_journal(journal_path)
            latest = current[-1] if current else latest
            if latest.get("state") in {"prepared", "track_staged", "staged"}:
                try:
                    self._rollback_raw(latest)
                except Exception:
                    # Leave the journal and staged paths for explicit restart
                    # recovery; never delete an ambiguous target.
                    pass
            raise

    def recover_operations(self):
        with self._lock:
            with self._writer_lock():
                return self._recover_operations_locked()

    def _recover_operations_locked(self):
        """Converge interrupted whole-trash and raw-track operations safely."""
        if not os.path.lexists(self.retention_ops_root):
            return ()
        if _has_link_component(self.retention_ops_root):
            raise RetentionSafetyError("A raiz de journals contém um link ou junction.")
        results = []
        try:
            paths = sorted(Path(self.retention_ops_root).glob("*" + OPERATION_JOURNAL_SUFFIX), key=lambda item: item.name)
        except OSError as error:
            raise RetentionError(f"Não foi possível enumerar os journals de retenção: {error}") from error
        for path_obj in paths:
            path = self._owned_path(path_obj, self.retention_ops_root, "journal", must_exist=True)
            if _is_link(path):
                raise RetentionSafetyError("Um journal de retenção é um link.")
            records = self._read_journal(path)
            if not records:
                continue
            latest = records[-1]
            if latest.get("state") in {"finalized", "rolled_back", "purged", "restored"}:
                # A projected raw operation may still have staged bytes when a
                # process died after the projection callback.
                continue
            if latest.get("state") == "projected" and latest.get("operation") == "raw_tracks":
                results.append(self._finalize_raw(latest))
                continue
            if latest.get("operation") == "whole_meeting":
                results.append(self._recover_whole(latest, path))
            elif latest.get("operation") == "raw_tracks":
                results.append(self._recover_raw(latest, path))
        # Do not silently leave a moved bundle that has lost its tombstone.
        # Journal recovery above gets first chance to recreate one.
        if os.path.lexists(self.trash_root):
            self.list_trash()
        return tuple(results)

    def _recover_whole(self, latest, journal_path):
        session_id = latest.get("session_id")
        if not _valid_id(session_id):
            raise OperationRecoveryError("Um journal de lixeira tem uma reunião inválida.")
        try:
            source_relative = latest["source_relative"]
            trash_relative = latest["trash_relative"]
            tombstone_relative = latest["tombstone_relative"]
        except KeyError as error:
            raise OperationRecoveryError("Um journal de lixeira não contém todos os alvos resolvidos.") from error
        source = self._path_from_relative(source_relative, self.home_root)
        entry = self._path_from_relative(trash_relative, self.trash_root)
        tombstone = self._path_from_relative(tombstone_relative, self.trash_root)
        source_exists = os.path.lexists(source)
        entry_exists = os.path.lexists(entry)
        if latest.get("state") == "purge_prepared":
            if source_exists:
                raise OperationRecoveryError("A purga encontrou a reunião canônica e o alvo da lixeira.")
            if entry_exists:
                self._remove_tree(entry)
            if os.path.lexists(tombstone):
                if _is_link(tombstone):
                    raise RetentionSafetyError("O tombstone da lixeira está linkado.")
                os.unlink(tombstone)
            self._append_journal(journal_path, {**latest, "state": "purged", "updated_at": _stamp(self._now())})
            return RetentionResult(latest["operation_id"], session_id, "purged", latest.get("byte_estimate", 0), (), ("permanent deletion; restore is no longer available",), None)
        if latest.get("state") == "restore_prepared":
            if source_exists and not entry_exists:
                if os.path.lexists(tombstone):
                    if _is_link(tombstone):
                        raise RetentionSafetyError("O tombstone restaurado está linkado.")
                    os.unlink(tombstone)
                index_updated = self._project(session_id, restored=True)
                self._append_journal(journal_path, {**latest, "state": "restored", "index_updated": index_updated, "updated_at": _stamp(self._now())})
                return RetentionResult(latest["operation_id"], session_id, "restored", latest.get("byte_estimate", 0), (source,), (), index_updated)
            if entry_exists and not source_exists:
                self._safe_tree(entry)
                self._mkdir_owned(self.meetings_root, self.home_root)
                os.replace(entry, source)
                if os.path.lexists(tombstone):
                    if _is_link(tombstone):
                        raise RetentionSafetyError("O tombstone restaurado está linkado.")
                    os.unlink(tombstone)
                index_updated = self._project(session_id, restored=True)
                self._append_journal(journal_path, {**latest, "state": "restored", "index_updated": index_updated, "updated_at": _stamp(self._now())})
                return RetentionResult(latest["operation_id"], session_id, "restored", latest.get("byte_estimate", 0), (source,), (), index_updated)
        if source_exists and not entry_exists:
            if latest.get("state") in {"tombstoned", "projected"}:
                try:
                    self._append_journal(
                        journal_path,
                        {**latest, "state": "ambiguous", "manual_reconciliation": True, "updated_at": _stamp(self._now())},
                    )
                finally:
                    raise OperationRecoveryError("A lixeira perdeu o alvo movido e a origem reapareceu; reconciliação manual necessária.")
            self._append_journal(journal_path, {**latest, "state": "aborted", "updated_at": _stamp(self._now())})
            return RetentionResult(latest["operation_id"], session_id, "aborted", latest.get("byte_estimate", 0), (), (), None)
        if entry_exists and not source_exists:
            self._safe_tree(entry)
            if not os.path.lexists(tombstone):
                now = self._now()
                planned_purge = _parse_stamp(latest.get("purge_after"))
                if planned_purge is None:
                    planned_purge = now + timedelta(days=self.trash_retention_days)
                value = self._tombstone_payload(latest["operation_id"], session_id, entry, latest.get("byte_estimate", 0), now, planned_purge)
                self._write_tombstone(tombstone, value)
            index_updated = self._project(session_id, removed=True)
            projection_state = "projected" if index_updated is not False else "tombstoned"
            self._append_journal(
                journal_path,
                {
                    **latest,
                    "state": projection_state,
                    "index_updated": index_updated,
                    "projection_pending": index_updated is False,
                    "updated_at": _stamp(self._now()),
                },
            )
            return RetentionResult(latest["operation_id"], session_id, "trashed", latest.get("byte_estimate", 0), (), (), index_updated)
        if not source_exists and not entry_exists:
            try:
                self._append_journal(
                    journal_path,
                    {**latest, "state": "ambiguous", "manual_reconciliation": True, "updated_at": _stamp(self._now())},
                )
            finally:
                raise OperationRecoveryError("A lixeira perdeu a origem e o alvo movido; reconciliação manual necessária.")
        raise OperationRecoveryError("O journal da lixeira encontrou dois alvos ao mesmo tempo.")

    def _recover_raw(self, latest, journal_path):
        state = latest.get("state")
        if state in {"metadata_committed", "projected"}:
            metadata = self._read_canonical_metadata(latest.get("session_id"))
            if not self._raw_commit_matches(metadata, latest):
                raise OperationRecoveryError("O commit raw não tem uma marca canônica correspondente.")
            if state == "metadata_committed":
                index_updated = self._project(latest["session_id"], removed=False)
                projection_state = "projected" if index_updated is not False else "metadata_committed"
                committed_latest = {
                    **latest,
                    "state": projection_state,
                    "index_updated": index_updated,
                    "projection_pending": index_updated is False,
                    "updated_at": _stamp(self._now()),
                }
                self._append_journal(journal_path, committed_latest)
                if index_updated is False:
                    return RetentionResult(
                        latest["operation_id"],
                        latest["session_id"],
                        "projection_pending",
                        latest.get("byte_estimate", 0),
                        (),
                        (),
                        index_updated,
                    )
                latest = committed_latest
            return self._finalize_raw(latest)
        session_id = latest.get("session_id")
        metadata = self._read_canonical_metadata(session_id)
        tracks = tuple(latest.get("tracks", ()))
        marker_commit = self._raw_commit_matches(metadata, latest)
        if marker_commit and latest.get("staged_tracks"):
            committed_latest = {**latest, "state": "metadata_committed", "committed_tracks": latest.get("staged_tracks", tracks)}
            self._append_journal(journal_path, committed_latest)
            return self._recover_raw(committed_latest, journal_path)
        if _json_fingerprint(metadata) != latest.get("metadata_before_fingerprint"):
            raise OperationRecoveryError("O metadata mudou antes do commit raw; recuperação manual necessária.")
        return self._rollback_raw(latest)

    # -- Public mutation aliases ---------------------------------------

    def apply(self, plan, *, confirm=False, permanent=False):
        with self._lock:
            with self._writer_lock():
                if not isinstance(plan, RetentionPlan):
                    raise TypeError("A operação de retenção exige um RetentionPlan.")
                if plan.operation == "keep":
                    return RetentionResult("", plan.session_id, "kept", 0, (), (), None)
                # Revalidate eligibility before asking for confirmation.  A lease
                # rejection must never be masked by a missing UI confirmation.
                self._ensure_plan(plan)
                self._confirmation(confirm, permanent=permanent)
                if plan.operation == "whole_meeting":
                    result = self._trash(plan)
                    if permanent:
                        self._purge_locked(plan.session_id, confirm=True)
                        return RetentionResult(result.operation_id, result.session_id, "purged", result.byte_estimate, (), result.lost_capabilities, result.index_updated)
                    return result
                if plan.operation == "raw_tracks":
                    return self._remove_raw(plan)
                raise RetentionError("A operação do plano não é reconhecida.")

    def trash_meeting(self, session_id, *, confirm=False, purge_after_days=None):
        policy = RetentionPolicy.whole_meeting(after_days=0, purge_after_days=self.trash_retention_days if purge_after_days is None else purge_after_days)
        plan = self.plan(session_id, policy)
        return self.apply(plan, confirm=confirm)

    delete = trash_meeting
    delete_session = trash_meeting
    remove_raw_tracks = apply
    reconcile = recover_operations
    startup_reconcile = recover_operations


__all__ = [
    "ActiveLeaseError",
    "ConfirmationRequired",
    "MeetingRetention",
    "OperationRecoveryError",
    "PlanConflict",
    "RetentionError",
    "RetentionLockTimeout",
    "RetentionPlan",
    "RetentionPolicy",
    "RetentionResult",
    "RetentionSafetyError",
    "RetentionTarget",
    "TrashEntry",
]
