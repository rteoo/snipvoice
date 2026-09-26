"""Worker-owned, bounded WAV import, provenance exports, and native playback."""

import array
import copy
import itertools
import json
import math
import ntpath
import os
from pathlib import Path
import re
import struct
import sys
import tempfile
import wave

from i18n import tr


# ceiling: 1,024 frames per playback write and 8,192 per import; raise only after cancellation/memory profiling.
PLAY_FRAMES = 1024
IMPORT_FRAMES = 8192
# ceiling: 250 ms preroll is enough for MP3 bit-reservoir reconstruction; raise only with codec evidence.
MP3_SEEK_PREROLL_SECONDS = 0.25
RIFF_LIMIT = 0xFFFFFFFF
SUPPORTED_AUDIO_EXTENSIONS = frozenset({".wav", ".mp3", ".aac", ".m4a", ".flac", ".ogg", ".opus"})
MAX_EXPORT_REPORTS = 64
MAX_EXPORT_CITATIONS = 256

# Native capture timestamps can jitter around packet boundaries. Keep this
# bounded so an actual pause is still represented as silence.
TIMESTAMP_JITTER_SECONDS = 0.02


def _cancel(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise RuntimeError(tr("A operação foi cancelada; o áudio salvo e os arquivos anteriores foram preservados."))


def _pcm_float(raw, width):
    result = array.array("f")
    midpoint = 1 << (width * 8 - 1)
    for offset in range(0, len(raw), width):
        value = int.from_bytes(raw[offset:offset + width], "little", signed=width != 1)
        result.append((value - (midpoint if width == 1 else 0)) / midpoint)
    if sys.byteorder != "little":
        result.byteswap()
    return result.tobytes()


def _check_wav(path, cancel_event=None):
    """Validate ordinary RIFF PCM before wave opens or a session is created."""
    with open(path, "rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        header = handle.read(12)
        if len(header) != 12 or header[:4] != b"RIFF" or header[8:] != b"WAVE":
            raise ValueError(tr("Escolha um arquivo WAV RIFF com PCM de 8, 16, 24 ou 32 bits."))
        end = struct.unpack_from("<I", header, 4)[0] + 8
        if end > size or end < 12:
            raise ValueError(tr("O arquivo WAV está incompleto; preserve o original e escolha um arquivo válido."))
        seen_format = False
        seen_data = False
        chunks = 0
        while handle.tell() + 8 <= end:
            _cancel(cancel_event)
            chunks += 1
            # ceiling: 4096 RIFF chunks; unusually fragmented containers need a separately reviewed parser.
            if chunks > 4096:
                raise ValueError(tr("O WAV contém blocos demais. Converta para PCM padrão antes de importar."))
            name, length = struct.unpack("<4sI", handle.read(8))
            position = handle.tell()
            if position + length > end:
                raise ValueError(tr("O arquivo WAV contém um bloco incompleto."))
            if name == b"fmt ":
                if seen_format or length != 16:
                    raise ValueError(tr("Use WAV PCM padrão, sem compressão ou formato extensível."))
                tag, channels, rate, byte_rate, alignment, bits = struct.unpack("<HHIIHH", handle.read(16))
                if (tag != 1 or channels not in range(1, 9) or not 8000 <= rate <= 192000
                        or bits not in (8, 16, 24, 32) or alignment != channels * (bits // 8)
                        or byte_rate != rate * alignment):
                    raise ValueError(tr("Use WAV PCM de 8–192 kHz, 1–8 canais e 8, 16, 24 ou 32 bits."))
                seen_format = True
            elif name == b"data":
                if not seen_format or seen_data or not length or length % alignment:
                    raise ValueError(tr("O arquivo WAV não contém frames PCM completos."))
                seen_data = True
            handle.seek(position + length + (length & 1))
        if not seen_format or not seen_data:
            raise ValueError(tr("O arquivo WAV não contém áudio PCM válido."))


def import_wav(store, path, settings, cancel_event=None):
    _cancel(cancel_event)
    path = Path(path)
    _check_wav(path, cancel_event)
    snapshot = settings.payload() if hasattr(settings, "payload") else dict(settings)
    session = None
    try:
        with wave.open(str(path), "rb") as reader:
            width, channels, rate = reader.getsampwidth(), reader.getnchannels(), reader.getframerate()
            if width not in (1, 2, 3, 4) or channels not in range(1, 9) or not 8000 <= rate <= 192000:
                raise ValueError(tr("O formato WAV mudou durante a leitura. Escolha novamente um arquivo PCM válido."))
            total = reader.getnframes()
            session = store.begin(snapshot, path.stem[:400])
            store.add_event(session, {"type": "imported_wav", "timestamp": 0.0,
                                      "track": "microphone", "filename": path.name,
                                      "pcm_bits": width * 8, "timing_precision": "audio_frames"})
            frames_read = 0
            sequence = 0
            while frames_read < total:
                _cancel(cancel_event)
                expected = min(IMPORT_FRAMES, total - frames_read)
                raw = reader.readframes(expected)
                if len(raw) != expected * channels * width:
                    raise ValueError(tr("A leitura do WAV foi interrompida; o áudio já importado foi preservado."))
                store.append_audio(session, {
                    "type": "audio", "generation": 0, "track": "microphone", "sequence": sequence,
                    "rate": rate, "channels": channels, "frames": expected, "timestamp": frames_read / rate,
                }, _pcm_float(raw, width))
                frames_read += expected
                sequence += 1
            _cancel(cancel_event)
            store.finish(session)
            return session
    except Exception as error:
        if session is not None:
            try:
                status = "cancelled" if cancel_event is not None and cancel_event.is_set() else "failed"
                store.finish(session, status, str(error))
            except Exception as persistence_error:
                raise OSError(tr("A importação falhou e não foi possível registrar o estado final. Preserve a reunião para recuperação.")) from persistence_error
        raise


def _pyav_chunks(path, cancel_event=None):
    try:
        import av
    except ImportError as error:
        raise RuntimeError(
            tr("A importação deste formato exige o decodificador de áudio incluído na instalação completa do SnipVoice.")
        ) from error
    try:
        with av.open(str(path), mode="r") as container:
            stream = container.streams.best("audio")
            if stream is None:
                raise ValueError(tr("O arquivo não contém uma faixa de áudio compatível."))
            resampler = None
            rate = channels = None
            decoded = False
            for source_frame in container.decode(stream):
                _cancel(cancel_event)
                if resampler is None:
                    rate = source_frame.sample_rate
                    channels = source_frame.layout.nb_channels
                    if channels not in range(1, 9) or not isinstance(rate, int) or not 8000 <= rate <= 192000:
                        raise ValueError(tr("Use áudio de 8–192 kHz e 1–8 canais."))
                    resampler = av.AudioResampler(
                        format="flt", layout=source_frame.layout.name, rate=rate,
                        frame_size=IMPORT_FRAMES,
                    )
                for frame in resampler.resample(source_frame):
                    decoded = True
                    yield rate, channels, _packed_float_frame(frame, rate, channels)
            if resampler is not None:
                for frame in resampler.resample(None):
                    decoded = True
                    yield rate, channels, _packed_float_frame(frame, rate, channels)
            if not decoded:
                raise ValueError(tr("O arquivo não contém áudio decodificável."))
    except Exception as error:
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError(
                tr("A operação foi cancelada; o áudio salvo e os arquivos anteriores foram preservados.")
            ) from error
        ffmpeg_error = getattr(av, "FFmpegError", ())
        if isinstance(error, ffmpeg_error):
            raise ValueError(tr("Não foi possível decodificar o arquivo de áudio selecionado.")) from error
        if isinstance(error, (OSError, ValueError)):
            raise
        raise ValueError(tr("Não foi possível decodificar o arquivo de áudio selecionado.")) from error


def _packed_float_frame(frame, rate, channels):
    if (frame.sample_rate != rate or frame.layout.nb_channels != channels
            or frame.format.name != "flt" or len(frame.planes) != 1):
        raise ValueError(tr("O formato do áudio mudou durante a decodificação."))
    if not isinstance(frame.samples, int) or frame.samples < 1 or frame.samples > IMPORT_FRAMES:
        raise ValueError(tr("O decodificador retornou um bloco de áudio inválido."))
    expected = frame.samples * channels * 4
    plane = memoryview(frame.planes[0])
    if len(plane) < expected:
        raise ValueError(tr("O decodificador retornou um bloco de áudio incompleto."))
    payload = bytes(plane[:expected])
    if sys.byteorder != "little":
        values = array.array("f")
        values.frombytes(payload)
        values.byteswap()
        payload = values.tobytes()
    return payload


def import_audio(store, path, settings, cancel_event=None):
    """Import a supported local audio file into the append-only meeting store."""
    _cancel(cancel_event)
    path = Path(path)
    suffix = path.suffix.casefold()
    if suffix not in SUPPORTED_AUDIO_EXTENSIONS:
        raise ValueError(tr("Escolha um arquivo WAV, MP3, AAC/M4A, FLAC, OGG ou Opus."))
    if suffix == ".wav":
        return import_wav(store, path, settings, cancel_event)
    snapshot = settings.payload() if hasattr(settings, "payload") else dict(settings)
    chunks = iter(_pyav_chunks(path, cancel_event))
    first = next(chunks, None)
    if first is None:
        raise ValueError(tr("O arquivo não contém áudio decodificável."))
    session = None
    try:
        session = store.begin(snapshot, path.stem[:400])
        store.add_event(session, {
            "type": "imported_audio", "timestamp": 0.0, "track": "microphone",
            "filename": path.name, "source_format": suffix[1:],
            "decoder": "PyAV", "timing_precision": "audio_frames",
        })
        frames_read = 0
        sequence = 0
        for rate, channels, payload in itertools.chain((first,), chunks):
            _cancel(cancel_event)
            frames = len(payload) // (channels * 4)
            if frames < 1 or frames > IMPORT_FRAMES or len(payload) != frames * channels * 4:
                raise ValueError(tr("O decodificador retornou um bloco de áudio inválido."))
            store.append_audio(session, {
                "type": "audio", "generation": 0, "track": "microphone", "sequence": sequence,
                "rate": rate, "channels": channels, "frames": frames, "timestamp": frames_read / rate,
            }, payload)
            frames_read += frames
            sequence += 1
        _cancel(cancel_event)
        store.finish(session)
        return session
    except Exception as error:
        if session is not None:
            try:
                status = "cancelled" if cancel_event is not None and cancel_event.is_set() else "failed"
                store.finish(session, status, str(error))
            except Exception as persistence_error:
                raise OSError(
                    tr("A importação falhou e não foi possível registrar o estado final. Preserve a reunião para recuperação.")
                ) from persistence_error
        raise
    finally:
        close = getattr(chunks, "close", None)
        if close is not None:
            close()


def _events(store, session, metadata):
    if hasattr(store, "iter_events"):
        return store.iter_events(session)
    return iter(metadata.get("events", ()))


def _absolute_path(value):
    """Recognize native absolute paths even when exporting cross-platform data."""
    return isinstance(value, str) and (
        os.path.isabs(value) or ntpath.isabs(value) or value.startswith("/")
    )


def _export_value(value):
    """Detach export data and replace absolute local paths with a marker."""
    if isinstance(value, dict):
        result = {}
        for child_key, child in value.items():
            if isinstance(child_key, str) and child_key.casefold() == "meeting_destination":
                continue
            result[str(child_key)] = _export_value(child)
        return result
    if isinstance(value, list):
        return [_export_value(child) for child in value]
    if _absolute_path(value):
        return "[redacted]"
    return copy.deepcopy(value)


_TEXT_PATH_RE = re.compile(
    r"""
    (?<![\w:/])
    (?P<path>
        (?:[A-Za-z]:[\\/](?=[^\\/\s])|(?:\\\\|//)(?=[^\\/\s])|/(?![/\s]))
        (?:(?![<>"|?*\r\n,;!?)]|\.(?=\s|$)).)*?
    )
    (?P<terminal>
        [,;!?)]|\.(?=\s|$)|(?=\r?\n)|
        (?=\s+[a-z][\w'-]*(?=\s|[,;!?)]|\.(?=\s|$)|$))|
        (?=\s+(?:and|or|then|but|while|with|without|for|to|from|about|because|after|before|during|where|when|which|that|this|these|those|into|onto|through|over|under|near|beside|inside|outside|remains?|is|are|was|were|exists?|stays?|keeps?)\b)|
        (?=$)
    )
    """,
    re.VERBOSE,
)


def _redact_absolute_paths(value):
    """Redact embedded native absolute paths without touching URLs or prose."""
    if not isinstance(value, str):
        return value

    def replace(match):
        return "[redacted]" + match.group("terminal")

    return _TEXT_PATH_RE.sub(replace, value)


def _redact_free_form_export(value):
    """Copy an exported free-form value while redacting embedded paths."""
    if isinstance(value, dict):
        return {str(key): _redact_free_form_export(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_redact_free_form_export(child) for child in value]
    if isinstance(value, tuple):
        return tuple(_redact_free_form_export(child) for child in value)
    if isinstance(value, str):
        return _redact_absolute_paths(value)
    return copy.deepcopy(value)


def _annotation_export_projection(store, session, metadata):
    """Return selected canonical annotation state when the view exposes it."""
    value = metadata.get("annotations")
    if not isinstance(value, dict):
        reader = getattr(store, "read_annotations", None)
        if not callable(reader):
            reader = getattr(getattr(store, "_library", None), "read_annotations", None)
        if callable(reader):
            value = reader(session)
    if not isinstance(value, dict):
        return None
    selected = {
        key: value[key]
        for key in (
            "schema_version", "generation", "revision_filter", "title", "notes",
            "bookmarks", "highlights", "speaker_labels", "collection_ids", "tags",
            "people", "series_id", "reviewed_summary", "reviewed_artifacts",
            "active_report_id",
        )
        if key in value
    }
    return _redact_free_form_export(_export_value(selected))


def _report_citations(value, output, *, budget):
    """Collect only bounded citation identifiers, never report body text."""
    if budget <= 0:
        return
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(key, str) and key.casefold() in {"question", "answer", "text", "body"}:
                continue
            if isinstance(key, str) and key.casefold() in {"citations", "segment_ids", "source_ids"}:
                values = child if isinstance(child, list) else [child]
                for item in values:
                    if isinstance(item, str) and item not in output:
                        output.append(item)
                        if len(output) >= budget:
                            return
                continue
            _report_citations(child, output, budget=budget - len(output))
            if len(output) >= budget:
                return
    elif isinstance(value, list):
        for child in value:
            _report_citations(child, output, budget=budget - len(output))
            if len(output) >= budget:
                return


def _report_history_projection(report):
    """Project bounded report metadata without carrying generated bodies."""
    if not isinstance(report, dict):
        return None
    report_id = report.get("id", report.get("report_id"))
    if not isinstance(report_id, str) or not report_id:
        return None
    result = {"id": report_id}
    for key in (
        "schema_version", "kind", "profile_id", "profile_version", "session_id",
        "transcript_revision", "status", "created_at", "completed_at", "virtual",
    ):
        value = report.get(key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            if key in report:
                result[key] = value
    reviewed = report.get("reviewed_artifact")
    result["reviewed"] = reviewed is not None or bool(report.get("reviewed"))
    if isinstance(reviewed, dict) and isinstance(reviewed.get("generation"), int):
        result["review_generation"] = reviewed["generation"]
    elif isinstance(report.get("review_generation"), int):
        result["review_generation"] = report["review_generation"]
    model = report.get("model")
    if isinstance(model, dict):
        result["model"] = {
            key: _export_value(model[key])
            for key in ("id", "sha256", "runtime", "context_limit")
            if key in model and isinstance(model[key], (str, int, float, bool))
        }
    citations = []
    _report_citations(report.get("generated", report.get("payload", report)), citations,
                      budget=MAX_EXPORT_CITATIONS)
    if citations:
        result["citations"] = citations
    return _export_value(result)


def _report_history_for_export(store, session, cancel_event=None):
    """Read an optional bounded report-history seam from a store/view."""
    reader = getattr(store, "list_report_metadata", None)
    if not callable(reader):
        reader = getattr(getattr(store, "_library", None), "list_report_metadata", None)
    if callable(reader):
        reports = reader(
            session,
            include_legacy=True,
            limit=MAX_EXPORT_REPORTS,
            cancel_event=cancel_event,
        )
    else:
        reader = getattr(store, "list_reports", None)
        if not callable(reader):
            reader = getattr(getattr(store, "_library", None), "list_reports", None)
        if not callable(reader):
            return []
        reports = reader(session, include_legacy=True)
    detail_reader = getattr(store, "get_report", None)
    if not callable(detail_reader):
        detail_reader = getattr(getattr(store, "_library", None), "get_report", None)
    result = []
    for report in reports or ():
        _cancel(cancel_event)
        if len(result) >= MAX_EXPORT_REPORTS:
            break
        source = report
        report_id = report.get("id") if isinstance(report, dict) else None
        # The metadata seam intentionally excludes generated bodies.  Resolve
        # only the bounded rows selected for export so citation identifiers can
        # be projected without ever retaining report prose in the result.
        if callable(detail_reader) and isinstance(report_id, str) and report_id != "legacy-summary":
            source = detail_reader(session, report_id)
        projection = _report_history_projection(source)
        if projection is not None:
            result.append(projection)
    return result


def _metadata_export_projection(store, session, metadata, cancel_event=None):
    """Build additive, path-free metadata for whole-meeting exports."""
    public_metadata = _redact_free_form_export(_export_value({
        key: value for key, value in metadata.items() if key not in {"events", "annotations"}
    }))
    settings = public_metadata.get("settings")
    if isinstance(settings, dict):
        settings.pop("meeting_destination", None)
        public_metadata["settings"] = settings
    final_audio = public_metadata.get("final_audio")
    if isinstance(final_audio, dict) and final_audio.get("path"):
        final_audio["path"] = ntpath.basename(os.fspath(metadata["final_audio"]["path"]))
        public_metadata["final_audio"] = final_audio
    annotations = _annotation_export_projection(store, session, metadata)
    report_history = _report_history_for_export(store, session, cancel_event)
    return public_metadata, annotations, report_history


def _json(handle, value):
    for piece in json.JSONEncoder(ensure_ascii=False, allow_nan=False).iterencode(value):
        handle.write(piece)


def _json_export(handle, store, session, metadata, cancel_event=None):
    _cancel(cancel_event)
    public_metadata, annotations, report_history = _metadata_export_projection(
        store, session, metadata, cancel_event,
    )
    _cancel(cancel_event)
    handle.write('{"metadata":')
    _json(handle, public_metadata)
    handle.write(',"annotations":')
    _json(handle, annotations or {})
    handle.write(',"report_history":')
    _json(handle, report_history)
    handle.write(',"events":[')
    separator = ""
    for event in _events(store, session, metadata):
        _cancel(cancel_event)
        handle.write(separator)
        _json(handle, _redact_free_form_export(_export_value(event)))
        separator = ","
    handle.write('],"transcripts":[')
    separator = ""
    for revision in metadata.get("revisions", []):
        _cancel(cancel_event)
        handle.write(separator + '{"revision":')
        _json(handle, _redact_free_form_export(_export_value(revision)))
        handle.write(',"segments":[')
        segment_separator = ""
        for segment in store.get_transcript(session, revision["id"]):
            _cancel(cancel_event)
            handle.write(segment_separator)
            _json(handle, _redact_free_form_export(_export_value(segment)))
            segment_separator = ","
        handle.write("]}")
        separator = ","
    handle.write("]}")


def _text_export(handle, store, session, metadata, markdown, cancel_event=None):
    def line(value=""):
        handle.write(str(value) + "\n")
    title = _redact_absolute_paths(metadata.get("title") or session)
    line(("# " if markdown else "") + title)
    line(tr("ID: {session}; estado: {status}; duração: {duration:.3f} s", session=session,
            status=metadata.get('status'), duration=metadata.get('duration', 0)))
    line(tr("Fontes: microfone/sistema; rótulos não identificam pessoas. Tempos da transcrição representam blocos de áudio."))
    line(tr("\nDisponibilidade das fontes:"))
    for track, value in metadata.get("tracks", {}).items():
        _cancel(cancel_event)
        if isinstance(value, dict):
            unavailable = (
                value.get("available") is False
                or value.get("raw_removed") is True
                or value.get("purged") is True
                or value.get("purged_at") is not None
                or value.get("state") == "purged"
                or any(
                    isinstance(segment, dict)
                    and (
                        segment.get("available") is False
                        or segment.get("raw_removed") is True
                        or segment.get("purged") is True
                        or segment.get("purged_at") is not None
                    )
                    for segment in value.get("segments", ())
                )
            )
            state = tr("removida pela retenção") if unavailable else tr("disponível")
            line(f"{track}: {state}")
    line(tr("\nNotas:"))
    line(_redact_absolute_paths(metadata.get("notes", "")))
    line(tr("\nMarcadores:"))
    for bookmark in metadata.get("bookmarks", []):
        _cancel(cancel_event)
        line(json.dumps(_redact_free_form_export(_export_value(bookmark)), ensure_ascii=False))
    line(tr("\nProveniência e lacunas:"))
    for event in _events(store, session, metadata):
        _cancel(cancel_event)
        if event.get("type") != "audio":
            line(json.dumps(_redact_free_form_export(_export_value(event)), ensure_ascii=False))
    for revision in metadata.get("revisions", []):
        _cancel(cancel_event)
        line(tr("\nRevisão: ") + json.dumps(
            _redact_free_form_export(_export_value(revision)), ensure_ascii=False,
        ))
        for segment in store.get_transcript(session, revision["id"]):
            _cancel(cancel_event)
            text = _redact_absolute_paths(segment.get("text", ""))
            line(f"[{segment.get('start', 0):.3f}–{segment.get('end', 0):.3f} s | {segment.get('track', 'unknown')} | {segment.get('id', '')}] {text}")
    if metadata.get("summary"):
        line(tr("\nResumo local editável:"))
        line(json.dumps(_redact_free_form_export(metadata["summary"]), ensure_ascii=False))
    if metadata.get("reviewed_summary"):
        line(tr("\nResumo revisado manualmente:"))
        line(_redact_absolute_paths(metadata["reviewed_summary"]))
    annotations = _annotation_export_projection(store, session, metadata)
    _cancel(cancel_event)
    if annotations:
        line(tr("\nAnotações canônicas:"))
        line(json.dumps(_redact_free_form_export(annotations), ensure_ascii=False))
    report_history = _report_history_for_export(store, session, cancel_event)
    _cancel(cancel_event)
    if report_history:
        line(tr("\nHistórico de relatórios (metadados e citações):"))
        for report in report_history:
            _cancel(cancel_event)
            line(json.dumps(report, ensure_ascii=False))


def _audio_chunks(store, session, track, start=0.0, cancel_event=None, duration=None):
    """Trim overlapping blocks and seeks, preserving elapsed-time gaps."""
    cursor = start
    rate = channels = None
    previous_format = None
    events = iter(store.iter_audio(session, track, start))
    _cancel(cancel_event)
    pending = next(events, None)
    while pending is not None:
        event, payload = pending
        _cancel(cancel_event)
        rate, channels = event["rate"], event["channels"]
        frame_bytes = channels * 4
        if len(payload) != event["frames"] * frame_bytes:
            raise ValueError(tr("O bloco de áudio salvo está incompleto. Preserve a reunião e tente recuperá-la."))
        _cancel(cancel_event)
        pending = next(events, None)
        event_timestamp = float(event["timestamp"])
        overlap = cursor - event_timestamp
        jitter = max(TIMESTAMP_JITTER_SECONDS, 2.0 / rate)
        current_end = event_timestamp + event["frames"] / rate
        same_previous_format = (
            previous_format is not None
            and previous_format == (rate, channels, event.get("generation"))
        )
        next_closes_gap = False
        if pending is not None and -jitter <= overlap < 0:
            next_event = pending[0]
            next_timestamp = float(next_event["timestamp"])
            next_overlap = current_end - next_timestamp
            gap = -overlap
            same_format = (
                next_event.get("rate") == rate
                and next_event.get("channels") == channels
                and next_event.get("generation") == event.get("generation")
            )
            next_closes_gap = (
                same_format
                and 0 < next_overlap <= jitter
                and abs(next_overlap - gap) <= 2.0 / rate
            )
        if same_previous_format and (0 < overlap <= jitter or next_closes_gap):
            # Preserve the complete packet when its timestamp lands just
            # before the previous packet's frame boundary.  If a small gap is
            # followed by a matching overlap, both timestamps are clock
            # jitter; an isolated forward gap remains explicit silence.
            event_timestamp = cursor
        skip = min(event["frames"], max(0, math.ceil((cursor - event_timestamp) * rate - 1e-9)))
        timestamp = event_timestamp + skip / rate
        if skip == event["frames"]:
            continue
        gap = max(0, round((timestamp - cursor) * rate))
        while gap:
            _cancel(cancel_event)
            count = min(gap, PLAY_FRAMES)
            yield rate, channels, b"\0" * (count * frame_bytes)
            gap -= count
        for frame in range(skip, event["frames"], PLAY_FRAMES):
            _cancel(cancel_event)
            end = min(event["frames"], frame + PLAY_FRAMES)
            yield rate, channels, payload[frame * frame_bytes:end * frame_bytes]
        cursor = event_timestamp + event["frames"] / rate
        previous_format = (rate, channels, event.get("generation"))
    if rate is not None and isinstance(duration, (int, float)) and math.isfinite(duration):
        gap = max(0, round((duration - cursor) * rate))
        while gap:
            _cancel(cancel_event)
            count = min(gap, PLAY_FRAMES)
            yield rate, channels, b"\0" * (count * channels * 4)
            gap -= count


def _clip_audio_chunks(store, session, track, start, end, cancel_event=None, gaps=None):
    """Yield only ``[start, end)`` from one native source track.

    Audio is kept in bounded chunks, and missing intervals are represented by
    explicit silence.  The caller supplies ``gaps`` when it needs provenance;
    the list is deliberately metadata-only and never retains audio bytes.
    """
    cursor = float(start)
    output_format = None
    saw_audio = False
    for event, payload in store.iter_audio(session, track, start):
        _cancel(cancel_event)
        rate, channels = event["rate"], event["channels"]
        frame_bytes = channels * 4
        frames = event["frames"]
        if len(payload) != frames * frame_bytes:
            raise ValueError(tr("O bloco de áudio salvo está incompleto. Preserve a reunião e tente recuperá-la."))
        event_start = float(event["timestamp"])
        event_end = event_start + frames / rate
        if event_end <= start:
            continue
        if event_start >= end:
            break
        native_format = (rate, channels)
        if output_format is not None and native_format != output_format:
            raise ValueError(
                tr("A fonte mudou de formato durante o destaque. Exporte os segmentos originais separadamente.")
            )
        if output_format is None:
            output_format = native_format

        first_frame = max(0, math.ceil((start - event_start) * rate - 1e-9))
        last_frame = min(frames, math.ceil((end - event_start) * rate - 1e-9))
        if last_frame <= first_frame:
            continue
        overlap_start = event_start + first_frame / rate
        if overlap_start > cursor:
            gap_frames = max(0, math.ceil((overlap_start - cursor) * rate - 1e-9))
            if gap_frames:
                gap_end = cursor + gap_frames / rate
                if gaps is not None:
                    gaps.append({"start": cursor, "end": min(gap_end, end)})
                while gap_frames:
                    _cancel(cancel_event)
                    count = min(gap_frames, PLAY_FRAMES)
                    yield rate, channels, b"\0" * (count * frame_bytes)
                    gap_frames -= count
                cursor = gap_end

        for frame in range(first_frame, last_frame, PLAY_FRAMES):
            _cancel(cancel_event)
            frame_end = min(last_frame, frame + PLAY_FRAMES)
            yield rate, channels, payload[frame * frame_bytes:frame_end * frame_bytes]
        cursor = event_start + last_frame / rate
        saw_audio = True

    if not saw_audio or output_format is None:
        raise ValueError(tr("A fonte escolhida não contém áudio no intervalo do destaque."))
    rate, channels = output_format
    if cursor < end:
        gap_frames = max(0, math.ceil((end - cursor) * rate - 1e-9))
        if gap_frames:
            gap_end = cursor + gap_frames / rate
            if gaps is not None:
                gaps.append({"start": cursor, "end": min(gap_end, end)})
            while gap_frames:
                _cancel(cancel_event)
                count = min(gap_frames, PLAY_FRAMES)
                yield rate, channels, b"\0" * (count * channels * 4)
                gap_frames -= count


def _pcm16(raw):
    result = bytearray(len(raw) // 2)
    for index, (value,) in enumerate(struct.iter_unpack("<f", raw)):
        value = 0 if not math.isfinite(value) else max(-1, min(1, value))
        struct.pack_into("<h", result, index * 2, -32768 if value <= -1 else round(value * 32767))
    return result


def _wav_export(handle, store, session, metadata, track, cancel_event=None):
    chunks = iter(_audio_chunks(store, session, track, cancel_event=cancel_event, duration=metadata.get("duration")))
    first = next(chunks, None)
    if first is None:
        raise ValueError(tr("A fonte escolhida não contém áudio para exportar."))
    rate, channels, _ = first
    handle.write(b"RIFF\0\0\0\0WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, channels, rate, rate * channels * 2, channels * 2, 16))
    provenance_header = handle.tell()
    handle.write(b"svpr\0\0\0\0")
    provenance_start = handle.tell()

    class TextWriter:
        def write(self, text):
            handle.write(text.encode("utf-8"))
    writer = TextWriter()
    provenance = dict(metadata, export={"source_track": track, "format": "PCM16",
                                       "conversion": "clipped PCM16; native rate/channels unchanged; gaps filled with silence"})
    _json_export(writer, store, session, provenance, cancel_event)
    provenance_length = handle.tell() - provenance_start
    if provenance_length > RIFF_LIMIT:
        raise ValueError(tr("A proveniência excede o limite do formato WAV. Exporte JSON."))
    if provenance_length & 1:
        handle.write(b"\0")
    data_header = handle.tell()
    handle.write(b"data\0\0\0\0")
    data_start = handle.tell()

    def write_audio(chunk):
        _cancel(cancel_event)
        native_rate, native_channels, raw = chunk
        if (native_rate, native_channels) != (rate, channels):
            raise ValueError(tr("A fonte mudou de formato durante a gravação. Exporte JSON ou texto; os segmentos originais permanecem separados."))
        if handle.tell() + len(raw) // 2 - 8 > RIFF_LIMIT:
            raise ValueError(tr("O áudio excede o limite de 4 GiB do WAV. Exporte os segmentos separadamente."))
        handle.write(_pcm16(raw))
    write_audio(first)
    for chunk in chunks:
        write_audio(chunk)
    file_end = handle.tell()
    for position, length in ((4, file_end - 8), (provenance_header + 4, provenance_length), (data_header + 4, file_end - data_start)):
        handle.seek(position)
        handle.write(struct.pack("<I", length))
    handle.seek(file_end)


def export_meeting(store, session_id, path, format="markdown", cancel_event=None):
    _cancel(cancel_event)
    if format not in {"markdown", "plain", "text", "json", "wav", "wav-microphone", "wav-system"}:
        raise ValueError(tr("Escolha Markdown, texto, JSON ou WAV de uma fonte."))
    destination = Path(path).absolute()
    if not destination.parent.is_dir() or destination.is_dir():
        raise ValueError(tr("Escolha um arquivo em uma pasta existente para exportar."))
    library = os.path.realpath(store.root)
    try:
        inside_library = os.path.commonpath((library, os.path.realpath(destination))) == library
    except ValueError:
        inside_library = False
    if inside_library:
        raise ValueError(tr("Escolha uma pasta fora da biblioteca de reuniões para preservar os arquivos originais."))
    metadata = store.get(session_id, include_events=False)
    if format == "wav":
        tracks = list(metadata.get("tracks", {}))
        if len(tracks) != 1:
            raise ValueError(tr("Selecione WAV do microfone ou WAV do sistema para exportar uma fonte por arquivo."))
        format = "wav-" + tracks[0]
    descriptor, temporary = tempfile.mkstemp(prefix="." + destination.name + "-", suffix=".tmp", dir=destination.parent)
    try:
        mode = "wb" if format.startswith("wav-") else "w"
        kwargs = {} if mode == "wb" else {"encoding": "utf-8", "newline": "\n"}
        with os.fdopen(descriptor, mode, **kwargs) as handle:
            if format.startswith("wav-"):
                _wav_export(handle, store, session_id, metadata, format[4:], cancel_event)
            elif format == "json":
                _json_export(handle, store, session_id, metadata, cancel_event)
            else:
                _text_export(handle, store, session_id, metadata, format == "markdown", cancel_event)
            handle.flush()
            os.fsync(handle.fileno())
        _cancel(cancel_event)
        os.replace(temporary, destination)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return str(destination)


def export_transcript(store, session_id, path, *, style="full_text", revision=None,
                      cancel_event=None):
    """Export the complete selected revision in the chosen presentation format."""
    from meeting_text import iter_transcript_text

    _cancel(cancel_event)
    if style not in {"full_text", "timestamped"}:
        raise ValueError(tr("Escolha texto completo ou texto com horários."))
    destination = Path(path).absolute()
    if not destination.parent.is_dir() or destination.is_dir():
        raise ValueError(tr("Escolha um arquivo em uma pasta existente para exportar."))
    library = os.path.realpath(store.root)
    try:
        inside_library = os.path.commonpath((library, os.path.realpath(destination))) == library
    except ValueError:
        inside_library = False
    if inside_library:
        raise ValueError(tr("Escolha uma pasta fora da biblioteca para preservar os arquivos originais."))
    paragraphs = iter_transcript_text(store.get_transcript(session_id, revision), style)
    descriptor, temporary = tempfile.mkstemp(
        prefix="." + destination.name + "-", suffix=".tmp", dir=destination.parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            first = True
            for paragraph in paragraphs:
                _cancel(cancel_event)
                if not first:
                    handle.write("\n\n")
                handle.write(paragraph)
                first = False
            handle.flush()
            os.fsync(handle.fileno())
        _cancel(cancel_event)
        os.replace(temporary, destination)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return str(destination)


def _report_export_value(value, key=None):
    """Detach report data and redact path-shaped fields before export."""
    if isinstance(key, str) and key.casefold() in {
            "path", "file_path", "filepath", "absolute_path", "destination"}:
        return "[redacted]"
    if isinstance(value, dict):
        return {str(child_key): _report_export_value(child, child_key)
                for child_key, child in value.items()
                if not (isinstance(child_key, str) and child_key.casefold() in {
                    "path", "file_path", "filepath", "absolute_path", "destination"})}
    if isinstance(value, list):
        return [_report_export_value(child) for child in value]
    if isinstance(value, str):
        if _absolute_path(value):
            return "[redacted]"
        return _redact_absolute_paths(value)
    return copy.deepcopy(value)


def report_export_projection(report, *, section=None):
    """Return a detached, path-free report projection for copy/export flows."""
    if not isinstance(report, dict):
        raise ValueError(tr("O relatório deve ser um objeto."))
    generated = report.get("generated", report.get("payload"))
    if not isinstance(generated, dict):
        raise ValueError(tr("As seções geradas são inválidas."))
    reviewed = report.get("reviewed_artifact")
    reviewed_sections = reviewed.get("sections") if isinstance(reviewed, dict) else None
    selected = copy.deepcopy(generated)
    if isinstance(reviewed_sections, dict):
        selected.update(copy.deepcopy(reviewed_sections))
    if section is not None:
        if not isinstance(section, str) or section not in selected:
            raise ValueError(tr("A seção selecionada não existe neste relatório."))
        selected = {section: selected[section]}
    return {
        "report_id": report.get("id", report.get("report_id")),
        "kind": report.get("kind"),
        "profile_id": report.get("profile_id"),
        "profile_version": report.get("profile_version"),
        "session_id": report.get("session_id"),
        "transcript_revision": report.get("transcript_revision"),
        "model": _redact_free_form_export(_report_export_value(report.get("model", {}))),
        "created_at": report.get("created_at"),
        "sections": _redact_free_form_export(_report_export_value(selected)),
    }


def export_report(report, path, format="markdown", *, section=None, cancel_event=None):
    """Atomically export a selected report or section without local paths."""
    _cancel(cancel_event)
    if format not in {"markdown", "plain", "text", "json"}:
        raise ValueError(tr("Escolha Markdown, texto ou JSON para exportar o relatório."))
    destination = Path(path).absolute()
    if not destination.parent.is_dir() or destination.is_dir():
        raise ValueError(tr("Escolha um arquivo em uma pasta existente para exportar."))
    projection = report_export_projection(report, section=section)
    if format == "json":
        content = json.dumps(projection, ensure_ascii=False, indent=2) + "\n"
    else:
        title = projection.get("report_id") or tr("Relatório local")
        lines = [("# " if format == "markdown" else "") + str(title),
                 tr("Tipo: {kind}; perfil: {profile}", kind=projection.get('kind'),
                    profile=projection.get('profile_id')),
                 tr("Revisão de transcrição: {revision}",
                    revision=projection.get('transcript_revision')), ""]
        for name, value in projection["sections"].items():
            lines.append(("## " if format == "markdown" else "") + str(name))
            if isinstance(value, str):
                lines.append(value)
            else:
                lines.append(json.dumps(value, ensure_ascii=False, indent=2))
            lines.append("")
        content = "\n".join(lines)
        if not content.endswith("\n"):
            content += "\n"
    encoded_length = len(content.encode("utf-8"))
    if encoded_length > 2 * 1024 * 1024:
        raise ValueError(tr("O relatório excede o limite permitido para exportação."))
    descriptor, temporary = tempfile.mkstemp(
        prefix="." + destination.name + "-", suffix=".tmp", dir=destination.parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            _cancel(cancel_event)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        _cancel(cancel_event)
        os.replace(temporary, destination)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return str(destination)


def _clip_annotation(highlight):
    """Keep only stable, non-path annotation fields in clip provenance."""
    allowed = (
        "id", "revision", "transcript_revision", "start", "end", "track",
        "label", "note", "segment_ids", "segments",
    )
    return {key: highlight[key] for key in allowed if key in highlight}


def _commit_new_file(temporary, destination):
    """Publish a temporary file atomically without replacing a destination."""
    try:
        os.link(temporary, destination)
    except FileExistsError:
        raise
    finally:
        if os.path.lexists(temporary):
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _highlight_wav_export(handle, store, session, highlight, cancel_event=None):
    track = highlight["track"]
    start = highlight["start"]
    end = highlight["end"]
    gaps = []
    chunks = iter(_clip_audio_chunks(
        store, session, track, start, end, cancel_event=cancel_event, gaps=gaps,
    ))
    first = next(chunks, None)
    if first is None:
        raise ValueError(tr("A fonte escolhida não contém áudio no intervalo do destaque."))
    rate, channels, _ = first
    handle.write(b"RIFF\0\0\0\0WAVEfmt ")
    handle.write(struct.pack(
        "<IHHIIHH", 16, 1, channels, rate, rate * channels * 2, channels * 2, 16,
    ))
    data_header = handle.tell()
    handle.write(b"data\0\0\0\0")

    data_length = 0

    def write_audio(chunk):
        nonlocal data_length
        _cancel(cancel_event)
        native_rate, native_channels, raw = chunk
        if (native_rate, native_channels) != (rate, channels):
            raise ValueError(
                tr("A fonte mudou de formato durante o destaque. Exporte os segmentos originais separadamente.")
            )
        pcm = _pcm16(raw)
        if data_length + len(pcm) > RIFF_LIMIT:
            raise ValueError(tr("O áudio excede o limite de 4 GiB do WAV."))
        data_length += len(pcm)
        handle.write(pcm)

    write_audio(first)
    for chunk in chunks:
        write_audio(chunk)
    provenance = {
        "schema_version": 1,
        "type": "highlight_clip",
        "session": session,
        "track": track,
        "start": start,
        "end": end,
        "gap": bool(gaps),
        "gaps": gaps,
        "annotation": _clip_annotation(highlight),
    }
    try:
        provenance_bytes = json.dumps(
            provenance, ensure_ascii=False, allow_nan=False, separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError(tr("A proveniência do destaque não é serializável.")) from error
    if len(provenance_bytes) > RIFF_LIMIT:
        raise ValueError(tr("A proveniência excede o limite de 4 GiB do formato WAV."))
    handle.write(b"svpr" + struct.pack("<I", len(provenance_bytes)) + provenance_bytes)
    if len(provenance_bytes) & 1:
        handle.write(b"\0")
    file_end = handle.tell()
    riff_length = file_end - 8
    if riff_length > RIFF_LIMIT:
        raise ValueError(tr("O áudio excede o limite de 4 GiB do WAV."))
    for position, length in ((4, riff_length), (data_header + 4, data_length)):
        handle.seek(position)
        handle.write(struct.pack("<I", length))
    handle.seek(file_end)


def export_highlight_clip(store, session_id, highlight, path, cancel_event=None):
    """Export one source-track highlight as an atomic, non-overwriting PCM16 WAV."""
    if not isinstance(highlight, dict):
        raise ValueError(tr("O destaque deve ser um objeto."))
    track = highlight.get("track")
    start, end = highlight.get("start"), highlight.get("end")
    if track not in {"microphone", "system"}:
        raise ValueError(tr("A fonte do destaque é inválida."))
    if (isinstance(start, bool) or isinstance(end, bool)
            or not isinstance(start, (int, float)) or not isinstance(end, (int, float))
            or not math.isfinite(start) or not math.isfinite(end)
            or start < 0 or end <= start):
        raise ValueError(tr("O intervalo do destaque é inválido."))
    destination = Path(path).absolute()
    if not destination.parent.is_dir() or destination.is_dir() or os.path.lexists(destination):
        if os.path.lexists(destination):
            raise FileExistsError(tr("O arquivo de destino já existe; escolha um novo nome para preservar o clipe anterior."))
        raise ValueError(tr("Escolha um arquivo em uma pasta existente para exportar."))
    library = os.path.realpath(store.root)
    try:
        inside_library = os.path.commonpath((library, os.path.realpath(destination))) == library
    except ValueError:
        inside_library = False
    if inside_library:
        raise ValueError(tr("Escolha uma pasta fora da biblioteca de reuniões para preservar os arquivos originais."))
    store.get(session_id, include_events=False)
    descriptor, temporary = tempfile.mkstemp(
        prefix="." + destination.name + "-", suffix=".tmp", dir=destination.parent,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            _highlight_wav_export(handle, store, session_id, {
                **highlight, "start": float(start), "end": float(end),
            }, cancel_event)
            handle.flush()
            os.fsync(handle.fileno())
        _cancel(cancel_event)
        _commit_new_file(temporary, destination)
    except Exception:
        if os.path.lexists(temporary):
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        raise
    return str(destination)


def _final_audio_chunks(metadata, start, cancel_event):
    final = metadata.get("final_audio")
    path = final.get("path") if isinstance(final, dict) else None
    if not isinstance(path, str) or not path or not os.path.isfile(path):
        raise ValueError(tr("O áudio final não está disponível para reprodução."))
    suffix = os.path.splitext(path)[1].casefold()
    if suffix == ".mp3":
        yield from _pyav_final_audio_chunks(path, float(start), cancel_event)
        return
    try:
        with wave.open(path, "rb") as reader:
            rate, channels, width = reader.getframerate(), reader.getnchannels(), reader.getsampwidth()
            if not 8000 <= rate <= 192000 or channels not in (1, 2) or width != 2:
                raise ValueError(tr("O arquivo de áudio final não usa WAV PCM16 compatível."))
            reader.setpos(min(reader.getnframes(), int(start * rate)))
            while not cancel_event.is_set():
                raw = reader.readframes(PLAY_FRAMES)
                if not raw:
                    return
                yield rate, channels, _pcm_float(raw, width)
    except (OSError, wave.Error) as exc:
        raise ValueError(tr("Não foi possível ler o arquivo de áudio final.")) from exc


def _pyav_final_audio_chunks(path, start, cancel_event):
    """Decode bounded final MP3 frames, seeking before decoding the target."""
    try:
        import av
    except ImportError as error:
        raise RuntimeError(
            tr("A reprodução de MP3 exige o decodificador de áudio incluído na instalação completa do SnipVoice.")
        ) from error
    try:
        with av.open(str(path), mode="r") as container:
            stream = container.streams.best("audio")
            if stream is None:
                raise ValueError(tr("O arquivo MP3 não contém uma faixa de áudio compatível."))
            target = max(0.0, float(start))
            time_base = getattr(stream, "time_base", None)
            time_scale = float(time_base) if time_base else None
            stream_start = getattr(stream, "start_time", None)
            origin = (float(stream_start) * time_scale
                      if stream_start is not None and time_scale else 0.0)
            stream_duration = getattr(stream, "duration", None)
            if (stream_duration is not None and time_scale
                    and target >= max(0.0, float(stream_duration) * time_scale)):
                return
            _cancel(cancel_event)
            seek_target = max(0.0, target - MP3_SEEK_PREROLL_SECONDS)
            did_seek = seek_target > 0
            if did_seek:
                offset = (int(stream_start or 0) + int(seek_target / time_scale)
                          if time_scale else int(seek_target * 1_000_000))
                container.seek(offset, stream=stream, backward=True)

            resampler = None
            rate = channels = None
            timeline_known = not did_seek
            source_seen = False
            sample_cursor = seek_target if did_seek else 0.0

            def frames_from(source_frame):
                nonlocal resampler, rate, channels, timeline_known, source_seen, sample_cursor
                _cancel(cancel_event)
                if source_frame is not None:
                    source_seen = True
                    if resampler is None:
                        rate = source_frame.sample_rate
                        channels = source_frame.layout.nb_channels
                        if channels not in (1, 2) or not isinstance(rate, int) or not 8000 <= rate <= 48000:
                            raise ValueError(tr("O MP3 final deve usar áudio de 8–48 kHz e 1–2 canais."))
                        resampler = av.AudioResampler(
                            format="flt", layout=source_frame.layout.name, rate=rate,
                            frame_size=PLAY_FRAMES,
                        )
                    decoded = resampler.resample(source_frame)
                else:
                    decoded = resampler.resample(None)
                for frame in decoded:
                    _cancel(cancel_event)
                    if frame.samples > PLAY_FRAMES:
                        raise ValueError(tr("O decodificador retornou um bloco MP3 grande demais."))
                    payload = _packed_float_frame(frame, rate, channels)
                    frame_time = getattr(frame, "time", None)
                    if frame_time is None:
                        pts = getattr(frame, "pts", None)
                        frame_base = getattr(frame, "time_base", None)
                        if pts is not None and frame_base is not None:
                            frame_time = float(pts * frame_base)
                    if frame_time is None:
                        if not timeline_known:
                            raise ValueError(tr("O decodificador MP3 não informou timestamps para uma busca precisa."))
                        frame_time = sample_cursor
                    else:
                        frame_time = float(frame_time) - origin
                        timeline_known = True
                    frame_end = frame_time + frame.samples / rate
                    sample_cursor = max(sample_cursor, frame_end)
                    if frame_end <= target:
                        continue
                    if frame_time < target:
                        skip = min(frame.samples, max(0, int(math.ceil((target - frame_time) * rate - 1e-9))))
                        payload = payload[skip * channels * 4:]
                        if not payload:
                            continue
                    yield rate, channels, payload

            for source_frame in container.decode(stream):
                yield from frames_from(source_frame)
            if not source_seen and target <= 0:
                raise ValueError(tr("O arquivo MP3 não contém áudio decodificável."))
            if resampler is not None:
                yield from frames_from(None)
    except Exception as error:
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError(
                tr("A operação foi cancelada; o áudio salvo e os arquivos anteriores foram preservados.")
            ) from error
        ffmpeg_error = getattr(av, "FFmpegError", ())
        if isinstance(error, ffmpeg_error):
            raise ValueError(tr("Não foi possível decodificar o MP3 final.")) from error
        if isinstance(error, (OSError, ValueError, RuntimeError)):
            raise
        raise ValueError(tr("Não foi possível decodificar o MP3 final.")) from error


def play_audio(store, session_id, track, start, cancel_event):
    if track not in {"microphone", "system", "final"} or not isinstance(start, (int, float)) or isinstance(start, bool) or not math.isfinite(start) or start < 0:
        raise ValueError(tr("Escolha uma fonte e um instante de reprodução válido."))
    if cancel_event.is_set():
        return
    try:
        import sounddevice
    except ImportError as error:
        raise RuntimeError(tr("A reprodução exige o runtime de áudio local já provisionado. Use uma instalação completa do SnipVoice.")) from error
    stream = None
    current_format = None
    metadata = store.get(session_id, include_events=False)
    chunks = (_final_audio_chunks(metadata, float(start), cancel_event) if track == "final" else
              _audio_chunks(store, session_id, track, float(start), duration=metadata.get("duration")))
    try:
        for rate, channels, payload in chunks:
            if cancel_event.is_set():
                return
            if current_format != (rate, channels):
                if stream is not None:
                    stream.stop()
                    stream.close()
                stream = sounddevice.OutputStream(samplerate=rate, channels=channels, dtype="float32", device=None)
                stream.start()
                current_format = (rate, channels)
            frames = len(payload) // (channels * 4)
            stream.write(memoryview(payload).cast("f", shape=[frames, channels]))
    finally:
        if stream is not None:
            try:
                stream.abort() if cancel_event.is_set() else stream.stop()
            finally:
                stream.close()
