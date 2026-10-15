"""Recoverable MP3-first capture compaction.

This module intentionally sits beside the retention implementation.  Capture
compaction is a short, canonical migration of native tracks after a verified
derived file exists; it is not a retention operation and never removes the
meeting or the final audio.
"""

import contextlib
import hashlib
import json
import math
import os
import shutil
import uuid

from meeting_files import _final_audio_chunks


class CaptureCompactionError(RuntimeError):
    """A compaction precondition, integrity, or recovery error."""


class CaptureVerificationError(CaptureCompactionError):
    """The saved final audio cannot be proven complete."""


_TRACKS = ("microphone", "system")
_JOURNAL_NAME = "capture-compaction.json"
_LOCK_NAME = ".capture-compaction.lock"
_CHUNK_BYTES = 1024 * 1024


def _cancel(event):
    if event is not None and event.is_set():
        raise CaptureCompactionError("Capture compaction was cancelled; no source was removed.")


def _session_path(store, session_id):
    getter = getattr(store, "_session_dir", None)
    if not callable(getter):
        raise CaptureCompactionError("The meeting store does not expose an app-owned session path.")
    path = os.path.abspath(os.fspath(getter(session_id)))
    if not os.path.isdir(path) or os.path.islink(path) or getattr(os.path, "isjunction", lambda _: False)(path):
        raise CaptureCompactionError("The meeting capture folder is not an app-owned directory.")
    return path


def _metadata(store, session_id):
    return store.get(session_id, include_events=False)


def _final_snapshot(metadata):
    value = metadata.get("final_audio")
    path = value.get("path") if isinstance(value, dict) else None
    if not isinstance(path, str) or not path:
        raise CaptureVerificationError("A saved final audio file is required before compaction.")
    path = os.path.abspath(path)
    if not os.path.isfile(path) or os.path.islink(path):
        raise CaptureVerificationError("The saved final audio file is missing or linked.")
    return path


def _sha256(path, cancel_event=None):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            _cancel(cancel_event)
            block = handle.read(_CHUNK_BYTES)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _stat(path):
    value = os.stat(path)
    return {"size": value.st_size, "mtime_ns": value.st_mtime_ns}


def _source_end(metadata):
    end = 0.0
    tracks = metadata.get("tracks", {})
    if not isinstance(tracks, dict):
        return end
    for name in _TRACKS:
        item = tracks.get(name, {})
        for segment in item.get("segments", ()) if isinstance(item, dict) else ():
            if not isinstance(segment, dict):
                continue
            try:
                candidate = float(segment.get("end", float(segment.get("start", 0.0)) + float(segment.get("duration", 0.0))))
            except (TypeError, ValueError):
                continue
            if math.isfinite(candidate):
                end = max(end, candidate)
    return end


def _verify_path(path, chunks, expected_duration=None, source_end=0.0, cancel_event=None):
    before = _stat(path)
    duration = 0.0
    frames = 0
    decoded = False
    try:
        for rate, channels, payload in chunks:
            _cancel(cancel_event)
            if not isinstance(rate, int) or rate <= 0 or channels not in (1, 2):
                raise CaptureVerificationError("The final decoder returned an invalid format.")
            if len(payload) % (channels * 4):
                raise CaptureVerificationError("The final decoder returned an incomplete frame.")
            count = len(payload) // (channels * 4)
            if count:
                decoded = True
                frames += count
                duration += count / rate
        if not decoded:
            raise CaptureVerificationError("The final audio contains no decodable frames.")
    except CaptureCompactionError:
        raise
    except Exception as error:
        raise CaptureVerificationError("The final audio could not be decoded to EOF.") from error
    after = _stat(path)
    if before != after:
        raise CaptureVerificationError("The final audio changed while it was being verified.")
    digest_hex = _sha256(path, cancel_event)
    if _stat(path) != after:
        raise CaptureVerificationError("The final audio changed while its hash was being computed.")
    # MP3's encoder delay/padding is bounded to two 1152-sample frames at
    # the normalized 48 kHz mix rate; never let a low-rate test fixture turn
    # this into a multi-minute acceptance window.
    tolerance = max(0.05, 2 * 1152 / 48000)
    target = expected_duration if expected_duration is not None else source_end
    if target and abs(duration - target) > tolerance:
        raise CaptureVerificationError("The final audio duration does not cover the captured source.")
    return {
        "path": path, "sha256": digest_hex, **after,
        "duration": duration, "frames": frames,
        "source_end": source_end,
        "tolerance": tolerance,
    }


def verify_mp3_file(path, expected_duration=None, cancel_event=None, decoder=None):
    """Verify an MP3 path, including temporary paths without an ``.mp3`` suffix."""
    path = os.path.abspath(os.fspath(path))
    if not os.path.isfile(path) or os.path.islink(path):
        raise CaptureVerificationError("The MP3 file is missing or linked.")
    if decoder is None:
        from meeting_files import _pyav_final_audio_chunks
        decoder = _pyav_final_audio_chunks(path, 0.0, cancel_event)
    else:
        decoder = decoder(path, cancel_event) if callable(decoder) else decoder
    return _verify_path(path, decoder, expected_duration, cancel_event=cancel_event)


def verify_final_audio(store, session_id, cancel_event=None, decoder=None):
    """Decode the saved final audio to EOF and return immutable evidence."""
    metadata = _metadata(store, session_id)
    if metadata.get("status") == "recording":
        raise CaptureVerificationError("The meeting is still recording.")
    path = _final_snapshot(metadata)
    chunks = (_final_audio_chunks(metadata, 0.0, cancel_event)
              if decoder is None else decoder(metadata, 0.0, cancel_event))
    return _verify_path(path, chunks, source_end=_source_end(metadata), cancel_event=cancel_event)


@contextlib.contextmanager
def _session_lock(session_path):
    path = os.path.join(session_path, _LOCK_NAME)
    with open(path, "a+b") as handle:
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                handle.seek(0)
                handle.write(b"0")
                handle.flush()
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, ImportError) as error:
            raise CaptureCompactionError("Another capture compaction is already running.") from error
        try:
            yield
        finally:
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except (OSError, ImportError):
                pass


def _reject_links(root):
    if os.path.islink(root) or getattr(os.path, "isjunction", lambda _: False)(root):
        raise CaptureCompactionError("The capture tree contains a link or junction.")
    for folder, dirs, files in os.walk(root, followlinks=False):
        for name in list(dirs) + list(files):
            path = os.path.join(folder, name)
            if os.path.islink(path) or getattr(os.path, "isjunction", lambda _: False)(path):
                raise CaptureCompactionError("The capture tree contains a link or junction.")


def _journal_path(session_path):
    return os.path.join(session_path, _JOURNAL_NAME)


def _operation_id(value):
    if not isinstance(value, str) or not value or any(char not in "0123456789abcdef" for char in value) or len(value) != 32:
        raise CaptureCompactionError("The compaction journal operation id is invalid.")
    return value


def _staging_path(session_path, operation_id):
    operation_id = _operation_id(operation_id)
    root = os.path.abspath(session_path)
    path = os.path.abspath(os.path.join(root, ".capture-compaction-" + operation_id))
    if os.path.dirname(path) != root or os.path.basename(path) != ".capture-compaction-" + operation_id:
        raise CaptureCompactionError("The compaction staging path is invalid.")
    if os.path.lexists(path) and (os.path.islink(path) or getattr(os.path, "isjunction", lambda _: False)(path)):
        raise CaptureCompactionError("The compaction staging path is linked.")
    return path


def _write_journal(path, value):
    temporary = path + ".tmp-" + uuid.uuid4().hex
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        if os.path.lexists(temporary):
            os.unlink(temporary)
        raise


def _remove_staged(staged):
    for path in staged:
        if os.path.lexists(path):
            if os.path.islink(path) or getattr(os.path, "isjunction", lambda _: False)(path):
                raise CaptureCompactionError("The staged capture path is linked.")
            _reject_links(path)
            shutil.rmtree(path)


def compact_capture(store, session_id, verified, cancel_event=None, fault_injector=None):
    """Atomically stage native tracks, commit canonical metadata, then clean up."""
    if not isinstance(verified, dict) or not verified.get("sha256"):
        raise CaptureCompactionError("Compaction requires verification evidence from verify_final_audio.")
    session_path = _session_path(store, session_id)
    with _session_lock(session_path):
        metadata = _metadata(store, session_id)
        if metadata.get("status") == "recording":
            raise CaptureCompactionError("The meeting is still recording.")
        final_path = _final_snapshot(metadata)
        current = _stat(final_path)
        if current.get("size") != verified.get("size") or current.get("mtime_ns") != verified.get("mtime_ns") or _sha256(final_path, cancel_event) != verified.get("sha256"):
            raise CaptureCompactionError("The saved final audio changed after verification.")
        existing = metadata.get("capture_storage")
        if (isinstance(existing, dict) and existing.get("state") == "compacted"
                and existing.get("sha256") == verified.get("sha256")):
            return {"operation_id": existing.get("operation_id"), "state": "compacted", "evidence": dict(existing)}
        _reject_links(session_path)
        journal_path = _journal_path(session_path)
        if os.path.lexists(journal_path):
            with open(journal_path, "r", encoding="utf-8") as handle:
                prior = json.load(handle)
            if prior.get("state") not in {"complete", "rolled_back"}:
                raise CaptureCompactionError("An unfinished compaction must be recovered first.")
        if not any(os.path.lexists(os.path.join(session_path, track)) for track in _TRACKS):
            return {"state": "no_sources", "evidence": dict(verified)}
        operation_id = uuid.uuid4().hex
        staging = _staging_path(session_path, operation_id)
        os.makedirs(staging)
        staged = []
        journal = {"operation_id": operation_id, "session_id": session_id, "state": "prepared", "staged": staged}
        _write_journal(journal_path, journal)
        try:
            for track in _TRACKS:
                source = os.path.join(session_path, track)
                if not os.path.lexists(source):
                    continue
                destination = os.path.join(staging, track)
                os.replace(source, destination)
                staged.append(destination)
                journal["staged"] = staged
                journal["state"] = "staged"
                _write_journal(journal_path, journal)
                if fault_injector:
                    fault_injector("after_stage_" + track)
            _cancel(cancel_event)
            journal["state"] = "ready_to_commit"
            _write_journal(journal_path, journal)
            if fault_injector:
                fault_injector("before_commit")
            commit = getattr(store, "commit_capture_compaction", None)
            if not callable(commit):
                raise CaptureCompactionError("The meeting store does not support canonical capture compaction.")
            evidence = dict(verified)
            evidence["path"] = final_path
            commit(session_id, [os.path.basename(path) for path in staged], evidence, operation_id)
            journal["state"] = "canonical_committed"
            _write_journal(journal_path, journal)
            if fault_injector:
                fault_injector("after_commit")
            _remove_staged(staged)
            shutil.rmtree(staging, ignore_errors=False)
            journal["state"] = "complete"
            _write_journal(journal_path, journal)
            return {"operation_id": operation_id, "state": "compacted", "evidence": evidence}
        except Exception:
            committed_now = False
            try:
                current_metadata = _metadata(store, session_id)
                marker = current_metadata.get("capture_storage")
                committed_now = (isinstance(marker, dict)
                                 and marker.get("state") == "compacted"
                                 and marker.get("operation_id") == operation_id)
            except Exception:
                pass
            if journal.get("state") != "canonical_committed" and not committed_now:
                for destination in reversed(staged):
                    if os.path.lexists(destination):
                        original = os.path.join(session_path, os.path.basename(destination))
                        if os.path.lexists(original):
                            raise CaptureCompactionError("The original capture path reappeared; recovery is required.")
                        os.replace(destination, original)
                if os.path.lexists(staging):
                    _remove_staged([os.path.join(staging, track) for track in _TRACKS])
                    if os.listdir(staging):
                        raise CaptureCompactionError("Ambiguous compaction staging remains; recovery is required.")
                    os.rmdir(staging)
                journal["state"] = "rolled_back"
                _write_journal(journal_path, journal)
            raise


def recover_capture_compaction(store, session_id):
    """Reconcile one interrupted compaction without deleting final audio."""
    session_path = _session_path(store, session_id)
    path = _journal_path(session_path)
    if not os.path.isfile(path):
        return None
    with _session_lock(session_path):
        with open(path, "r", encoding="utf-8") as handle:
            journal = json.load(handle)
        if journal.get("session_id") != session_id:
            raise CaptureCompactionError("The compaction journal belongs to another meeting.")
        state = journal.get("state")
        staging = _staging_path(session_path, journal.get("operation_id"))
        # A process may have committed canonical metadata and crashed before
        # recording the journal state.  The store's operation id is the
        # durable commit marker in that window.
        metadata = _metadata(store, session_id)
        canonical = metadata.get("capture_storage")
        committed = (
            isinstance(canonical, dict)
            and canonical.get("state") == "compacted"
            and canonical.get("operation_id") == journal.get("operation_id")
            and isinstance(canonical.get("sha256"), str)
            and len(canonical.get("sha256")) == 64
        )
        if committed:
            final = _final_snapshot(metadata)
            evidence = canonical if isinstance(canonical, dict) else {}
            if evidence.get("sha256") and _sha256(final) != evidence.get("sha256"):
                raise CaptureCompactionError("The final audio changed; staged capture was preserved.")
            if os.path.lexists(staging):
                _remove_staged([os.path.join(staging, track) for track in _TRACKS])
                if os.listdir(staging):
                    raise CaptureCompactionError("Ambiguous compaction staging remains; staged capture was preserved.")
                os.rmdir(staging)
            journal["state"] = "complete"
        elif state not in {"complete", "rolled_back"}:
            # Reconcile a crash between rename and the journal checkpoint by
            # discovering the known track names in the operation directory.
            for track in _TRACKS:
                staged = os.path.join(staging, track)
                destination = os.path.join(session_path, track)
                if os.path.lexists(staged) and not os.path.lexists(destination):
                    _reject_links(staged)
                    os.replace(staged, destination)
                elif os.path.lexists(staged) and os.path.lexists(destination):
                    raise CaptureCompactionError("Both staged and original capture paths exist; recovery is required.")
            if os.path.lexists(staging):
                _remove_staged([os.path.join(staging, track) for track in _TRACKS])
                if os.listdir(staging):
                    raise CaptureCompactionError("Ambiguous compaction staging remains; recovery is required.")
                os.rmdir(staging)
            journal["state"] = "rolled_back"
        _write_journal(path, journal)
        return journal
