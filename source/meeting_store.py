"""Durable, append only storage for offline meeting recordings.

The store deliberately keeps native audio in separate track segment files.  A
small CRC checked journal records the event and the byte range written to a
segment.  Recovery scans the valid journal prefix and never truncates segment
files; bytes left by a failed write remain harmless orphan bytes.
"""

import copy
import heapq
import json
import math
import os
import struct
import threading
import time
import uuid
import zlib

from snippet_utils import write_json_atomic


SCHEMA_VERSION = 1
METADATA_NAME = "metadata.json"
JOURNAL_NAME = "events.journal"
SEGMENT_SECONDS = 30.0
MAX_LIST_LIMIT = 500
# ceiling: library pagination retains at most 10,500 projected rows. Replace
# directory scanning with a rebuildable SQLite index when deeper pages are needed.
MAX_LIST_OFFSET = 10000
MAX_READ_BYTES = 1024 * 1024
MAX_EVENT_BYTES = 32 * 1024 * 1024
MAX_METADATA_EVENTS = 1000
METADATA_CHECKPOINT_SECONDS = 0.25
_JOURNAL_MAGIC = b"SVJ1"
_JOURNAL_HEADER = struct.Struct("<4sIII")

_TRACKS = {"microphone", "system"}
_AUDIO_BYTES_PER_SAMPLE = 4
_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")


def _utc_timestamp(value=None):
    """Return an ISO UTC timestamp suitable for sorting and display."""
    stamp = time.time() if value is None else float(value)
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stamp))


def _write_all(handle, data):
    """Write *data* even when the file object performs short writes."""
    offset = 0
    while offset < len(data):
        written = handle.write(data[offset:])
        if not isinstance(written, int) or written <= 0:
            raise OSError("Não foi possível avançar a gravação no disco.")
        offset += written


class MeetingStore:
    """Persistent meeting library rooted at a caller supplied directory."""

    def __init__(self, root):
        if not isinstance(root, (str, os.PathLike)):
            raise ValueError("A pasta das reuniões é inválida.")
        self.root = os.path.abspath(os.fspath(root))
        self.root_dir = self.root
        self._lock = threading.RLock()
        self._active = {}
        self._sequence_cache = {}
        self._last_checkpoint = {}
        os.makedirs(self.root, exist_ok=True)
        self._recover_unfinished()

    # -- Public lifecycle -------------------------------------------------

    def begin(self, settings, title=""):
        if not isinstance(settings, dict):
            raise ValueError("As configurações da reunião devem ser um objeto.")
        if not isinstance(title, str):
            raise ValueError("O título da reunião deve ser texto.")
        if len(title) > 400:
            raise ValueError("O título da reunião é muito longo.")
        # Validate JSON before creating a directory, so a bad snapshot cannot
        # leave an unopenable session behind.
        try:
            snapshot = copy.deepcopy(settings)
            json.dumps(snapshot, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("As configurações da reunião não são válidas.") from exc

        with self._lock:
            session_id = time.strftime("%Y%m%d-%H%M%S", time.localtime()) + "-" + uuid.uuid4().hex[:10]
            session_dir = self._session_dir(session_id)
            os.makedirs(session_dir)
            metadata = {
                "schema_version": SCHEMA_VERSION,
                "id": session_id,
                "title": title,
                "notes": "",
                "bookmarks": [],
                "status": "recording",
                "error": None,
                "created_at": _utc_timestamp(),
                "updated_at": _utc_timestamp(),
                "duration": 0.0,
                "settings": snapshot,
                "tracks": {},
                "events": [],
                "revisions": [],
                "summary": None,
            }
            self._write_metadata(session_id, metadata)
            # Create the journal eagerly.  Its existence is a useful marker
            # when inspecting a session interrupted before the first block.
            with open(os.path.join(session_dir, JOURNAL_NAME), "ab"):
                pass
            self._active[session_id] = metadata
            self._sequence_cache[session_id] = {}
            self._last_checkpoint[session_id] = time.monotonic()
            return session_id

    def append_audio(self, session_id, event, payload):
        """Append one native float32 interleaved audio block."""
        event = self._validate_audio_event(event)
        try:
            raw = bytes(payload)
        except (TypeError, ValueError) as exc:
            raise ValueError("O bloco de áudio é inválido.") from exc
        expected = event["frames"] * event["channels"] * _AUDIO_BYTES_PER_SAMPLE
        if len(raw) != expected:
            raise ValueError("O tamanho do bloco de áudio não corresponde aos frames informados.")
        if len(raw) > MAX_EVENT_BYTES:
            raise ValueError("O bloco de áudio excede o limite permitido.")

        with self._lock:
            metadata = self._active_metadata(session_id)
            self._ensure_writable(metadata)
            self._check_sequence(session_id, metadata, event)
            before_segments = len(metadata.get("tracks", {}).get(event["track"], {}).get("segments", []))
            segment = self._choose_segment(session_id, metadata, event)
            path = os.path.join(self._session_dir(session_id), segment["path"])
            os.makedirs(os.path.dirname(path), exist_ok=True)
            offset = self._append_segment(path, raw)
            record = {
                "event": event,
                "segment": segment["path"],
                "offset": offset,
                "length": len(raw),
            }
            try:
                self._append_journal(session_id, record)
            except Exception:
                # Keep raw bytes for forensic/recovery purposes.  Since there
                # is no journal frame, they are never returned as audio.
                raise
            self._remember_sequence(session_id, event)
            self._apply_audio_record(metadata, record)
            self._checkpoint_if_due(
                session_id,
                metadata,
                force=len(metadata["tracks"][event["track"]]["segments"]) != before_segments,
            )

    def add_event(self, session_id, event):
        """Append a non-audio event such as a source gap or device change."""
        if (not isinstance(event, dict) or event.get("type") == "audio"
                or not isinstance(event.get("type"), str) or not event.get("type")):
            raise ValueError("O evento da reunião é inválido.")
        clean = copy.deepcopy(event)
        self._validate_event_common(clean)
        if "track" in clean and clean["track"] not in _TRACKS:
            raise ValueError("A fonte do evento é inválida.")
        with self._lock:
            metadata = self._active_metadata(session_id)
            self._ensure_writable(metadata)
            self._check_sequence(session_id, metadata, clean)
            record = {"event": clean, "segment": None, "offset": 0, "length": 0}
            self._append_journal(session_id, record)
            self._remember_sequence(session_id, clean)
            metadata.setdefault("events", []).append(self._public_record(record))
            self._trim_events(metadata)
            self._update_duration(metadata, clean)
            self._checkpoint_session(session_id, metadata)

    def finish(self, session_id, status="completed", error=None):
        allowed = {"completed", "partial", "failed", "interrupted", "cancelled"}
        if status not in allowed:
            raise ValueError("O estado final da reunião é inválido.")
        if error is not None and not isinstance(error, str):
            error = str(error)
        with self._lock:
            metadata = self._active_metadata(session_id)
            metadata["status"] = status
            metadata["error"] = error
            metadata["updated_at"] = _utc_timestamp()
            self._checkpoint_session(session_id, metadata)
            self._active.pop(session_id, None)
            self._sequence_cache.pop(session_id, None)
            self._last_checkpoint.pop(session_id, None)
            return session_id

    def get(self, session_id, include_events=True):
        with self._lock:
            metadata = self._active.get(session_id) or self._load_metadata(session_id)
            if not include_events:
                metadata = dict(metadata)
                metadata.pop("events", None)
            return copy.deepcopy(metadata)

    def list_sessions(self, offset=0, limit=50, query="", status=""):
        if not isinstance(offset, int) or isinstance(offset, bool) or not 0 <= offset <= MAX_LIST_OFFSET:
            raise ValueError("O deslocamento da lista é inválido.")
        if not isinstance(limit, int) or limit < 0 or limit > MAX_LIST_LIMIT:
            raise ValueError("O limite da lista é inválido.")
        if not isinstance(query, str) or len(query) > 512:
            raise ValueError("A busca deve ser texto.")
        if not isinstance(status, str):
            raise ValueError("O filtro de estado é inválido.")
        if limit == 0:
            return []
        try:
            names = os.scandir(self.root)
        except OSError:
            return []
        try:
            return self._list_from_directory(names, offset, limit, query, status)
        finally:
            names.close()

    def _list_from_directory(self, names, offset, limit, query, status):
        entries = []
        for directory in names:
            name = directory.name
            if not self._valid_id(name):
                continue
            try:
                item = self._load_metadata(name)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if status and item.get("status") != status:
                continue
            haystack = (item.get("title", "") + "\n" + item.get("notes", "")).casefold()
            if query and query.casefold() not in haystack:
                revision_ids = [revision.get("id") for revision in item.get("revisions", []) if isinstance(revision, dict)]
                if not any(
                    query.casefold() in str(segment.get("text", "")).casefold()
                    for revision_id in revision_ids
                    for segment in self.get_transcript(name, revision_id)
                ):
                    continue
            projection = {key: item.get(key) for key in
                          ("id", "title", "status", "created_at", "duration", "error")}
            value = (str(item.get("created_at", "")), name, projection)
            if len(entries) < offset + limit:
                heapq.heappush(entries, value)
            elif value[:2] > entries[0][:2]:
                heapq.heapreplace(entries, value)
        entries.sort(reverse=True)
        return [copy.deepcopy(item[2]) for item in entries[offset : offset + limit]]

    def update(self, session_id, **fields):
        allowed = {"title", "notes", "bookmarks", "reviewed_summary"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError("Há campos de reunião não reconhecidos.")
        if "title" in fields and (not isinstance(fields["title"], str) or len(fields["title"]) > 400):
            raise ValueError("O título da reunião é inválido.")
        if "notes" in fields and not isinstance(fields["notes"], str):
            raise ValueError("As notas da reunião devem ser texto.")
        if "reviewed_summary" in fields and (not isinstance(fields["reviewed_summary"], str)
                                              or len(fields["reviewed_summary"]) > 65536):
            raise ValueError("O resumo revisado deve ter até 65536 caracteres.")
        if "bookmarks" in fields:
            if not isinstance(fields["bookmarks"], list):
                raise ValueError("Os marcadores da reunião devem ser uma lista.")
            try:
                json.dumps(fields["bookmarks"], ensure_ascii=False)
            except (TypeError, ValueError) as exc:
                raise ValueError("Os marcadores da reunião são inválidos.") from exc
        with self._lock:
            metadata = copy.deepcopy(self._active_metadata(session_id))
            metadata.update(copy.deepcopy(fields))
            metadata["updated_at"] = _utc_timestamp()
            self._checkpoint_session(session_id, metadata)
            if session_id in self._active:
                self._active[session_id] = metadata
            return True

    # -- Bounded readers and transcripts ---------------------------------

    def iter_audio(self, session_id, track=None, start=0.0):
        if track is not None and track not in _TRACKS:
            raise ValueError("A fonte de áudio é inválida.")
        try:
            start = float(start)
        except (TypeError, ValueError) as exc:
            raise ValueError("O início da leitura é inválido.") from exc
        if not math.isfinite(start) or start < 0:
            raise ValueError("O início da leitura é inválido.")
        self._session_dir(session_id)
        # Validate the session path before returning so malformed IDs fail at
        # the call site rather than only when a consumer starts iteration.
        def _read():
            # Snapshot journal records before yielding so callers can process
            # audio without holding the store lock or retaining a full meeting.
            for record in self._iter_journal_records(session_id):
                event = record["event"]
                if event.get("type") != "audio" or (track and event.get("track") != track):
                    continue
                end = event["timestamp"] + (event["frames"] / event["rate"])
                if end <= start:
                    continue
                payload = self._read_segment_record(session_id, record)
                if payload is None:
                    continue
                yield copy.deepcopy(event), payload

        return _read()

    def iter_events(self, session_id):
        """Stream public event metadata without retaining the whole journal."""
        self._session_dir(session_id)

        def _read():
            sizes = {}
            for record in self._iter_journal_records(session_id):
                if self._record_complete(session_id, record, sizes):
                    yield self._public_record(record)

        return _read()

    def save_summary(self, session_id, summary):
        """Atomically save an injected local summary while preserving prior data."""
        if not isinstance(summary, dict):
            raise ValueError("O resumo da reunião deve ser um objeto.")
        try:
            value = copy.deepcopy(summary)
            json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("O resumo da reunião não é válido.") from exc
        if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > 1024 * 1024:
            raise ValueError("O resumo da reunião excede o limite permitido.")
        with self._lock:
            current = self._active_metadata(session_id)
            metadata = copy.deepcopy(current)
            metadata["summary"] = value
            metadata["summary_updated_at"] = _utc_timestamp()
            self._checkpoint_session(session_id, metadata)
            if session_id in self._active:
                self._active[session_id] = metadata
            return copy.deepcopy(value)

    def begin_revision(self, session_id, profile, language, status="processing"):
        if not isinstance(profile, str) or not isinstance(language, str):
            raise ValueError("O perfil e o idioma da revisão são obrigatórios.")
        if status not in {"processing", "pending"}:
            raise ValueError("O estado inicial da revisão é inválido.")
        with self._lock:
            metadata = self._active_metadata(session_id)
            revision_id = time.strftime("%Y%m%d-%H%M%S", time.localtime()) + "-" + uuid.uuid4().hex[:8]
            revision = {
                "id": revision_id,
                "profile": profile,
                "language": language,
                "status": status,
                "segments": 0,
                "created_at": _utc_timestamp(),
                "error": None,
            }
            metadata.setdefault("revisions", []).append(revision)
            self._checkpoint_session(session_id, metadata)
            path = self._revision_path(session_id, revision_id)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "ab"):
                pass
            return revision_id

    def add_transcript(self, session_id, revision, segment):
        if not isinstance(segment, dict):
            raise ValueError("O segmento de transcrição é inválido.")
        clean = copy.deepcopy(segment)
        try:
            encoded = json.dumps(clean, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("O segmento de transcrição não é serializável.") from exc
        if len(encoded) > MAX_EVENT_BYTES:
            raise ValueError("O segmento de transcrição excede o limite permitido.")
        with self._lock:
            metadata = self._active_metadata(session_id)
            item = self._revision(metadata, revision)
            if item["status"] not in {"processing", "pending"}:
                raise ValueError("A revisão de transcrição já foi encerrada.")
            path = self._revision_path(session_id, revision)
            with open(path, "ab", buffering=0) as handle:
                _write_all(handle, encoded + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            item["segments"] = int(item.get("segments", 0)) + 1
            self._checkpoint_session(session_id, metadata)

    def finish_revision(self, session_id, revision, status="completed", error=None):
        if status not in {"completed", "failed", "cancelled", "superseded"}:
            raise ValueError("O estado da revisão é inválido.")
        with self._lock:
            metadata = self._active_metadata(session_id)
            item = self._revision(metadata, revision)
            item["status"] = status
            item["error"] = None if error is None else str(error)
            item["updated_at"] = _utc_timestamp()
            self._checkpoint_session(session_id, metadata)

    def get_transcript(self, session_id, revision=None):
        metadata = self._load_metadata(session_id)
        revisions = metadata.get("revisions", [])
        if revision is None:
            if not revisions:
                return iter(())
            revision = revisions[-1]["id"]
        self._revision(metadata, revision)
        path = self._revision_path(session_id, revision)

        def _read():
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    while True:
                        line = handle.readline(MAX_EVENT_BYTES + 1)
                        if not line:
                            break
                        if len(line) > MAX_EVENT_BYTES or not line.endswith("\n"):
                            break
                        try:
                            value = json.loads(line)
                        except (ValueError, TypeError):
                            break
                        if isinstance(value, dict):
                            yield value
            except OSError:
                return

        return _read()

    # -- Recovery and file format ----------------------------------------

    def _recover_unfinished(self):
        try:
            names = os.listdir(self.root)
        except OSError:
            return
        for name in names:
            if not self._valid_id(name):
                continue
            try:
                metadata = self._load_metadata(name)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if metadata.get("status") == "recording":
                metadata = self._rebuild_unfinished(name, metadata)
                metadata["status"] = "interrupted"
                metadata["error"] = "A reunião foi interrompida antes de terminar."
            else:
                continue
            try:
                self._write_metadata(name, metadata)
            except OSError:
                # The original metadata remains intact when atomic replacement
                # cannot complete; a later startup can retry recovery.
                continue

    def _rebuild_unfinished(self, session_id, metadata):
        """Rebuild one unfinished projection in a single journal pass."""
        events = []
        tracks = {}
        duration = 0.0
        sizes = {}
        for record in self._iter_journal_records(session_id):
            if not self._record_complete(session_id, record, sizes):
                continue
            event = self._public_record(record)
            events.append(event)
            if len(events) > MAX_METADATA_EVENTS:
                del events[:-MAX_METADATA_EVENTS]
            duration = max(duration, self._event_end(event))
            if event.get("type") != "audio":
                continue
            track = tracks.setdefault(
                event["track"],
                {"rate": event["rate"], "channels": event["channels"], "segments": []},
            )
            segments = track["segments"]
            if not segments or segments[-1]["path"] != event.get("segment"):
                segments.append(
                    {
                        "index": len(segments),
                        "path": event.get("segment"),
                        "start": event["timestamp"],
                        "duration": 0.0,
                        "bytes": 0,
                        "frames": 0,
                        "events": 0,
                        "rate": event["rate"],
                        "channels": event["channels"],
                    }
                )
            segment = segments[-1]
            segment["bytes"] += event.get("bytes", 0)
            segment["frames"] += event["frames"]
            segment["duration"] += event["frames"] / event["rate"]
            segment["end"] = self._event_end(event)
            segment["events"] += 1
            track["rate"] = event["rate"]
            track["channels"] = event["channels"]
        rebuilt = dict(metadata)
        rebuilt["events"] = events
        rebuilt["tracks"] = tracks
        rebuilt["duration"] = duration
        return rebuilt

    def _append_journal(self, session_id, record):
        body = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        header = _JOURNAL_HEADER.pack(_JOURNAL_MAGIC, len(body), 0, zlib.crc32(body) & 0xFFFFFFFF)
        path = os.path.join(self._session_dir(session_id), JOURNAL_NAME)
        with open(path, "ab", buffering=0) as handle:
            _write_all(handle, header + body)
            handle.flush()
            os.fsync(handle.fileno())

    def _journal_records(self, session_id):
        return list(self._iter_journal_records(session_id))

    def _iter_journal_records(self, session_id):
        path = os.path.join(self._session_dir(session_id), JOURNAL_NAME)
        try:
            with open(path, "rb") as handle:
                while True:
                    header = handle.read(_JOURNAL_HEADER.size)
                    if not header:
                        break
                    if len(header) != _JOURNAL_HEADER.size:
                        break
                    magic, body_len, payload_len, checksum = _JOURNAL_HEADER.unpack(header)
                    if magic != _JOURNAL_MAGIC or payload_len or body_len > MAX_EVENT_BYTES:
                        break
                    body = self._read_bounded(handle, body_len)
                    if body is None or zlib.crc32(body) & 0xFFFFFFFF != checksum:
                        break
                    try:
                        record = json.loads(body.decode("utf-8"))
                    except (UnicodeError, ValueError, TypeError):
                        break
                    if not isinstance(record, dict) or not isinstance(record.get("event"), dict):
                        break
                    yield record
        except OSError:
            return

    @staticmethod
    def _read_bounded(handle, length):
        chunks = []
        remaining = length
        while remaining:
            chunk = handle.read(min(remaining, MAX_READ_BYTES))
            if not chunk:
                return None
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _append_segment(self, path, payload):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "ab", buffering=0) as handle:
            offset = handle.tell()
            _write_all(handle, payload)
            handle.flush()
            os.fsync(handle.fileno())
            return offset

    def _read_segment_record(self, session_id, record):
        relative = record.get("segment")
        if not self._valid_relative_path(relative):
            return None
        try:
            offset = int(record["offset"])
            length = int(record["length"])
        except (KeyError, TypeError, ValueError):
            return None
        if offset < 0 or length < 0 or length > MAX_EVENT_BYTES:
            return None
        path = os.path.abspath(os.path.join(self._session_dir(session_id), relative))
        if os.path.commonpath((self._session_dir(session_id), path)) != self._session_dir(session_id):
            return None
        try:
            with open(path, "rb") as handle:
                handle.seek(offset)
                chunks = []
                remaining = length
                while remaining:
                    chunk = handle.read(min(remaining, MAX_READ_BYTES))
                    if not chunk:
                        return None
                    chunks.append(chunk)
                    remaining -= len(chunk)
                return b"".join(chunks)
        except OSError:
            return None

    def _record_complete(self, session_id, record, sizes=None):
        """Return whether a journaled audio range still has a full prefix."""
        if record.get("event", {}).get("type") != "audio":
            return True
        relative = record.get("segment")
        try:
            offset = int(record["offset"])
            length = int(record["length"])
        except (KeyError, TypeError, ValueError):
            return False
        if offset < 0 or length < 0 or not self._valid_relative_path(relative):
            return False
        path = os.path.abspath(os.path.join(self._session_dir(session_id), relative))
        if os.path.commonpath((self._session_dir(session_id), path)) != self._session_dir(session_id):
            return False
        try:
            if sizes is not None:
                relative = relative.replace("\\", "/")
                if relative not in sizes:
                    sizes[relative] = os.path.getsize(path)
                return sizes[relative] >= offset + length
            return os.path.getsize(path) >= offset + length
        except OSError:
            return False

    # -- Metadata helpers -------------------------------------------------

    def _active_metadata(self, session_id):
        if not self._valid_id(session_id):
            raise ValueError("Identificador de reunião inválido.")
        metadata = self._active.get(session_id)
        if metadata is not None:
            return metadata
        metadata = self._load_metadata(session_id)
        if metadata.get("status") == "recording":
            self._active[session_id] = metadata
            self._sequence_cache[session_id] = {}
            self._last_checkpoint[session_id] = time.monotonic()
        return metadata

    @staticmethod
    def _trim_events(metadata):
        events = metadata.setdefault("events", [])
        if len(events) > MAX_METADATA_EVENTS:
            del events[:-MAX_METADATA_EVENTS]

    def _checkpoint_session(self, session_id, metadata):
        metadata["updated_at"] = _utc_timestamp()
        self._write_metadata(session_id, metadata)
        self._last_checkpoint[session_id] = time.monotonic()

    def _checkpoint_if_due(self, session_id, metadata, force=False):
        self._trim_events(metadata)
        if force or time.monotonic() - self._last_checkpoint.get(session_id, 0.0) >= METADATA_CHECKPOINT_SECONDS:
            self._checkpoint_session(session_id, metadata)

    def _session_dir(self, session_id):
        if not self._valid_id(session_id):
            raise ValueError("Identificador de reunião inválido.")
        result = os.path.join(self.root, str(session_id))
        if os.path.commonpath((os.path.realpath(self.root), os.path.realpath(result))) != os.path.realpath(self.root):
            raise ValueError("A pasta da reunião aponta para fora da biblioteca.")
        return result

    @staticmethod
    def _valid_id(value):
        return isinstance(value, str) and 1 <= len(value) <= 80 and all(char in _ID_CHARS for char in value)

    @staticmethod
    def _valid_relative_path(value):
        if not isinstance(value, str) or not value or os.path.isabs(value) or ":" in value:
            return False
        normalized = value.replace("\\", "/")
        return all(part not in {"", ".", ".."} for part in normalized.split("/"))

    def _load_metadata(self, session_id):
        path = os.path.join(self._session_dir(session_id), METADATA_NAME)
        with open(path, "r", encoding="utf-8") as handle:
            metadata = json.load(handle)
        if not isinstance(metadata, dict) or metadata.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("O formato da reunião não é reconhecido.")
        return metadata

    def _write_metadata(self, session_id, metadata):
        write_json_atomic(os.path.join(self._session_dir(session_id), METADATA_NAME), metadata)

    @staticmethod
    def _ensure_writable(metadata):
        if metadata.get("status") not in {"recording", "interrupted"}:
            raise ValueError("A reunião já foi encerrada.")

    def _validate_audio_event(self, event):
        if not isinstance(event, dict) or event.get("type") != "audio":
            raise ValueError("O evento de áudio é inválido.")
        clean = copy.deepcopy(event)
        self._validate_event_common(clean)
        if clean.get("track") not in _TRACKS:
            raise ValueError("A fonte de áudio é inválida.")
        if not isinstance(clean.get("generation"), int) or clean["generation"] < 0:
            raise ValueError("A geração do evento de áudio é inválida.")
        for key in ("rate", "channels", "frames"):
            if not isinstance(clean.get(key), int) or clean[key] <= 0:
                raise ValueError("O formato do evento de áudio é inválido.")
        if not isinstance(clean.get("sequence"), int) or clean["sequence"] < 0:
            raise ValueError("O formato do evento de áudio é inválido.")
        if clean["channels"] > 32 or clean["rate"] > 384000:
            raise ValueError("O formato do evento de áudio não é suportado.")
        return clean

    @staticmethod
    def _validate_event_common(event):
        timestamp = event.get("timestamp")
        if (
            not isinstance(timestamp, (int, float))
            or isinstance(timestamp, bool)
            or not math.isfinite(float(timestamp))
            or timestamp < 0
        ):
            raise ValueError("O instante do evento é inválido.")
        if "sequence" in event and (not isinstance(event["sequence"], int) or event["sequence"] < 0):
            raise ValueError("A sequência do evento é inválida.")

    def _check_sequence(self, session_id, metadata, event):
        if "sequence" not in event:
            return
        track = event.get("track")
        generation = event.get("generation")
        key = (track, generation)
        prior = self._sequence_cache.setdefault(session_id, {}).get(key)
        if prior is None:
            for item in metadata.get("events", []):
                if item.get("track") == track and item.get("generation") == generation and "sequence" in item:
                    prior = item["sequence"]
        if prior is not None and event["sequence"] <= prior:
            raise ValueError("A sequência do evento não é crescente.")

    def _remember_sequence(self, session_id, event):
        if "sequence" in event:
            self._sequence_cache.setdefault(session_id, {})[(event.get("track"), event.get("generation"))] = event["sequence"]

    def _choose_segment(self, session_id, metadata, event):
        track_name = event["track"]
        track = metadata.setdefault("tracks", {}).setdefault(
            track_name,
            {"rate": event["rate"], "channels": event["channels"], "segments": []},
        )
        segments = track.setdefault("segments", [])
        frames_duration = event["frames"] / event["rate"]
        if (
            not segments
            or track.get("rate") != event["rate"]
            or track.get("channels") != event["channels"]
            or segments[-1].get("duration", 0.0) >= SEGMENT_SECONDS
            or segments[-1].get("duration", 0.0) + frames_duration > SEGMENT_SECONDS
        ):
            index = len(segments)
            segment = {
                "index": index,
                "path": track_name + "/segment-%06d.pcm" % index,
                "start": event["timestamp"],
                "duration": 0.0,
                "bytes": 0,
                "frames": 0,
                "events": 0,
                "rate": event["rate"],
                "channels": event["channels"],
            }
            segments.append(segment)
            track["rate"] = event["rate"]
            track["channels"] = event["channels"]
        return segments[-1]

    def _apply_audio_record(self, metadata, record):
        metadata.setdefault("events", []).append(self._public_record(record))
        self._trim_events(metadata)
        event = record["event"]
        track = metadata["tracks"][event["track"]]
        segment = track["segments"][-1]
        segment["bytes"] += record["length"]
        segment["frames"] += event["frames"]
        segment["duration"] += event["frames"] / event["rate"]
        segment["end"] = event["timestamp"] + event["frames"] / event["rate"]
        segment["events"] += 1
        self._update_duration(metadata, event)

    @staticmethod
    def _public_record(record):
        event = copy.deepcopy(record["event"])
        if event.get("type") == "audio":
            event.update(
                {
                    "segment": record.get("segment"),
                    "offset": record.get("offset", 0),
                    "bytes": record.get("length", 0),
                }
            )
        return event

    @staticmethod
    def _update_duration(metadata, event):
        end = float(event.get("timestamp", 0.0))
        if event.get("type") == "audio":
            end += event["frames"] / event["rate"]
        elif isinstance(event.get("duration"), (int, float)):
            end += max(0.0, float(event["duration"]))
        metadata["duration"] = max(float(metadata.get("duration", 0.0)), end)

    @staticmethod
    def _duration_from_events(events):
        duration = 0.0
        for event in events:
            try:
                end = float(event.get("timestamp", 0.0))
                if event.get("type") == "audio":
                    end += event["frames"] / event["rate"]
                elif isinstance(event.get("duration"), (int, float)):
                    end += max(0.0, float(event["duration"]))
                duration = max(duration, end)
            except (TypeError, ValueError, ZeroDivisionError):
                continue
        return duration

    @staticmethod
    def _event_end(event):
        end = float(event.get("timestamp", 0.0))
        if event.get("type") == "audio":
            end += event["frames"] / event["rate"]
        elif isinstance(event.get("duration"), (int, float)):
            end += max(0.0, float(event["duration"]))
        return end

    def _tracks_from_records(self, records):
        tracks = {}
        for record in records:
            event = record["event"]
            if event.get("type") != "audio":
                continue
            track = tracks.setdefault(
                event["track"],
                {"rate": event["rate"], "channels": event["channels"], "segments": []},
            )
            segment_path = record.get("segment")
            segments = track["segments"]
            if not segments or segments[-1]["path"] != segment_path:
                segments.append(
                    {
                        "index": len(segments),
                        "path": segment_path,
                        "start": event["timestamp"],
                        "duration": 0.0,
                        "bytes": 0,
                        "frames": 0,
                        "events": 0,
                        "rate": event["rate"],
                        "channels": event["channels"],
                    }
                )
            segment = segments[-1]
            segment["bytes"] += int(record.get("length", 0))
            segment["frames"] += event["frames"]
            segment["duration"] += event["frames"] / event["rate"]
            segment["end"] = event["timestamp"] + event["frames"] / event["rate"]
            segment["events"] += 1
            track["rate"] = event["rate"]
            track["channels"] = event["channels"]
        return tracks

    @staticmethod
    def _revision(metadata, revision_id):
        if not isinstance(revision_id, str):
            raise ValueError("Identificador de revisão inválido.")
        for item in metadata.get("revisions", []):
            if isinstance(item, dict) and item.get("id") == revision_id:
                return item
        raise ValueError("A revisão de transcrição não foi encontrada.")

    def _revision_path(self, session_id, revision):
        if not self._valid_id(revision):
            raise ValueError("Identificador de revisão inválido.")
        return os.path.join(self._session_dir(session_id), "transcripts", revision + ".jsonl")


__all__ = ["MeetingStore", "SCHEMA_VERSION", "SEGMENT_SECONDS", "MAX_READ_BYTES"]
