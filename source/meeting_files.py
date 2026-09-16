"""Worker-owned, bounded WAV import, provenance exports, and native playback."""

import array
import itertools
import json
import math
import os
from pathlib import Path
import struct
import sys
import tempfile
import wave


# ceiling: 1,024 frames per playback write and 8,192 per import; raise only after cancellation/memory profiling.
PLAY_FRAMES = 1024
IMPORT_FRAMES = 8192
RIFF_LIMIT = 0xFFFFFFFF
SUPPORTED_AUDIO_EXTENSIONS = frozenset({".wav", ".mp3", ".aac", ".m4a", ".flac", ".ogg", ".opus"})


def _cancel(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise RuntimeError("A operação foi cancelada; o áudio salvo e os arquivos anteriores foram preservados.")


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
            raise ValueError("Escolha um arquivo WAV RIFF com PCM de 8, 16, 24 ou 32 bits.")
        end = struct.unpack_from("<I", header, 4)[0] + 8
        if end > size or end < 12:
            raise ValueError("O arquivo WAV está incompleto; preserve o original e escolha um arquivo válido.")
        seen_format = False
        seen_data = False
        chunks = 0
        while handle.tell() + 8 <= end:
            _cancel(cancel_event)
            chunks += 1
            # ceiling: 4096 RIFF chunks; unusually fragmented containers need a separately reviewed parser.
            if chunks > 4096:
                raise ValueError("O WAV contém blocos demais. Converta para PCM padrão antes de importar.")
            name, length = struct.unpack("<4sI", handle.read(8))
            position = handle.tell()
            if position + length > end:
                raise ValueError("O arquivo WAV contém um bloco incompleto.")
            if name == b"fmt ":
                if seen_format or length != 16:
                    raise ValueError("Use WAV PCM padrão, sem compressão ou formato extensível.")
                tag, channels, rate, byte_rate, alignment, bits = struct.unpack("<HHIIHH", handle.read(16))
                if (tag != 1 or channels not in range(1, 9) or not 8000 <= rate <= 192000
                        or bits not in (8, 16, 24, 32) or alignment != channels * (bits // 8)
                        or byte_rate != rate * alignment):
                    raise ValueError("Use WAV PCM de 8–192 kHz, 1–8 canais e 8, 16, 24 ou 32 bits.")
                seen_format = True
            elif name == b"data":
                if not seen_format or seen_data or not length or length % alignment:
                    raise ValueError("O arquivo WAV não contém frames PCM completos.")
                seen_data = True
            handle.seek(position + length + (length & 1))
        if not seen_format or not seen_data:
            raise ValueError("O arquivo WAV não contém áudio PCM válido.")


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
                raise ValueError("O formato WAV mudou durante a leitura. Escolha novamente um arquivo PCM válido.")
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
                    raise ValueError("A leitura do WAV foi interrompida; o áudio já importado foi preservado.")
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
                raise OSError("A importação falhou e não foi possível registrar o estado final. Preserve a reunião para recuperação.") from persistence_error
        raise


def _pyav_chunks(path, cancel_event=None):
    try:
        import av
    except ImportError as error:
        raise RuntimeError(
            "A importação deste formato exige o decodificador de áudio incluído na instalação completa do Snipvoice."
        ) from error
    try:
        with av.open(str(path), mode="r") as container:
            stream = container.streams.best("audio")
            if stream is None:
                raise ValueError("O arquivo não contém uma faixa de áudio compatível.")
            resampler = None
            rate = channels = None
            decoded = False
            for source_frame in container.decode(stream):
                _cancel(cancel_event)
                if resampler is None:
                    rate = source_frame.sample_rate
                    channels = source_frame.layout.nb_channels
                    if channels not in range(1, 9) or not isinstance(rate, int) or not 8000 <= rate <= 192000:
                        raise ValueError("Use áudio de 8–192 kHz e 1–8 canais.")
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
                raise ValueError("O arquivo não contém áudio decodificável.")
    except Exception as error:
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError(
                "A operação foi cancelada; o áudio salvo e os arquivos anteriores foram preservados."
            ) from error
        ffmpeg_error = getattr(av, "FFmpegError", ())
        if isinstance(error, ffmpeg_error):
            raise ValueError("Não foi possível decodificar o arquivo de áudio selecionado.") from error
        if isinstance(error, (OSError, ValueError)):
            raise
        raise ValueError("Não foi possível decodificar o arquivo de áudio selecionado.") from error


def _packed_float_frame(frame, rate, channels):
    if (frame.sample_rate != rate or frame.layout.nb_channels != channels
            or frame.format.name != "flt" or len(frame.planes) != 1):
        raise ValueError("O formato do áudio mudou durante a decodificação.")
    if not isinstance(frame.samples, int) or frame.samples < 1 or frame.samples > IMPORT_FRAMES:
        raise ValueError("O decodificador retornou um bloco de áudio inválido.")
    expected = frame.samples * channels * 4
    plane = memoryview(frame.planes[0])
    if len(plane) < expected:
        raise ValueError("O decodificador retornou um bloco de áudio incompleto.")
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
        raise ValueError("Escolha um arquivo WAV, MP3, AAC/M4A, FLAC, OGG ou Opus.")
    if suffix == ".wav":
        return import_wav(store, path, settings, cancel_event)
    snapshot = settings.payload() if hasattr(settings, "payload") else dict(settings)
    chunks = iter(_pyav_chunks(path, cancel_event))
    first = next(chunks, None)
    if first is None:
        raise ValueError("O arquivo não contém áudio decodificável.")
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
                raise ValueError("O decodificador retornou um bloco de áudio inválido.")
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
                    "A importação falhou e não foi possível registrar o estado final. Preserve a reunião para recuperação."
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


def _json(handle, value):
    for piece in json.JSONEncoder(ensure_ascii=False, allow_nan=False).iterencode(value):
        handle.write(piece)


def _json_export(handle, store, session, metadata, cancel_event=None):
    _cancel(cancel_event)
    public_metadata = {key: value for key, value in metadata.items() if key != "events"}
    settings = public_metadata.get("settings")
    if isinstance(settings, dict) and "meeting_destination" in settings:
        settings = dict(settings)
        settings.pop("meeting_destination", None)
        public_metadata["settings"] = settings
    final_audio = public_metadata.get("final_audio")
    if isinstance(final_audio, dict) and final_audio.get("path"):
        final_audio = dict(final_audio)
        final_audio["path"] = os.path.basename(os.fspath(final_audio["path"]))
        public_metadata["final_audio"] = final_audio
    handle.write('{"metadata":')
    _json(handle, public_metadata)
    handle.write(',"events":[')
    separator = ""
    for event in _events(store, session, metadata):
        _cancel(cancel_event)
        handle.write(separator)
        _json(handle, event)
        separator = ","
    handle.write('],"transcripts":[')
    separator = ""
    for revision in metadata.get("revisions", []):
        _cancel(cancel_event)
        handle.write(separator + '{"revision":')
        _json(handle, revision)
        handle.write(',"segments":[')
        segment_separator = ""
        for segment in store.get_transcript(session, revision["id"]):
            _cancel(cancel_event)
            handle.write(segment_separator)
            _json(handle, segment)
            segment_separator = ","
        handle.write("]}")
        separator = ","
    handle.write("]}")


def _text_export(handle, store, session, metadata, markdown, cancel_event=None):
    def line(value=""):
        handle.write(str(value) + "\n")
    title = metadata.get("title") or session
    line(("# " if markdown else "") + title)
    line(f"ID: {session}; estado: {metadata.get('status')}; duração: {metadata.get('duration', 0):.3f} s")
    line("Fontes: microfone/sistema; rótulos não identificam pessoas. Tempos da transcrição representam blocos de áudio.")
    line("\nNotas:")
    line(metadata.get("notes", ""))
    line("\nMarcadores:")
    for bookmark in metadata.get("bookmarks", []):
        _cancel(cancel_event)
        line(json.dumps(bookmark, ensure_ascii=False))
    line("\nProveniência e lacunas:")
    for event in _events(store, session, metadata):
        _cancel(cancel_event)
        if event.get("type") != "audio":
            line(json.dumps(event, ensure_ascii=False))
    for revision in metadata.get("revisions", []):
        _cancel(cancel_event)
        line("\nRevisão: " + json.dumps(revision, ensure_ascii=False))
        for segment in store.get_transcript(session, revision["id"]):
            _cancel(cancel_event)
            line(f"[{segment.get('start', 0):.3f}–{segment.get('end', 0):.3f} s | {segment.get('track', 'unknown')} | {segment.get('id', '')}] {segment.get('text', '')}")
    if metadata.get("summary"):
        line("\nResumo local editável:")
        line(json.dumps(metadata["summary"], ensure_ascii=False))
    if metadata.get("reviewed_summary"):
        line("\nResumo revisado manualmente:")
        line(metadata["reviewed_summary"])


def _audio_chunks(store, session, track, start=0.0, cancel_event=None, duration=None):
    """Trim overlapping blocks and seeks, preserving elapsed-time gaps."""
    cursor = start
    rate = channels = None
    for event, payload in store.iter_audio(session, track, start):
        _cancel(cancel_event)
        rate, channels = event["rate"], event["channels"]
        frame_bytes = channels * 4
        if len(payload) != event["frames"] * frame_bytes:
            raise ValueError("O bloco de áudio salvo está incompleto. Preserve a reunião e tente recuperá-la.")
        skip = min(event["frames"], max(0, math.ceil((cursor - event["timestamp"]) * rate - 1e-9)))
        timestamp = event["timestamp"] + skip / rate
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
        cursor = event["timestamp"] + event["frames"] / rate
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
            raise ValueError("O bloco de áudio salvo está incompleto. Preserve a reunião e tente recuperá-la.")
        event_start = float(event["timestamp"])
        event_end = event_start + frames / rate
        if event_end <= start:
            continue
        if event_start >= end:
            break
        native_format = (rate, channels)
        if output_format is not None and native_format != output_format:
            raise ValueError(
                "A fonte mudou de formato durante o destaque. Exporte os segmentos originais separadamente."
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
        raise ValueError("A fonte escolhida não contém áudio no intervalo do destaque.")
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
        raise ValueError("A fonte escolhida não contém áudio para exportar.")
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
        raise ValueError("A proveniência excede o limite do formato WAV. Exporte JSON.")
    if provenance_length & 1:
        handle.write(b"\0")
    data_header = handle.tell()
    handle.write(b"data\0\0\0\0")
    data_start = handle.tell()

    def write_audio(chunk):
        _cancel(cancel_event)
        native_rate, native_channels, raw = chunk
        if (native_rate, native_channels) != (rate, channels):
            raise ValueError("A fonte mudou de formato durante a gravação. Exporte JSON ou texto; os segmentos originais permanecem separados.")
        if handle.tell() + len(raw) // 2 - 8 > RIFF_LIMIT:
            raise ValueError("O áudio excede o limite de 4 GiB do WAV. Exporte os segmentos separadamente.")
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
        raise ValueError("Escolha Markdown, texto, JSON ou WAV de uma fonte.")
    destination = Path(path).absolute()
    if not destination.parent.is_dir() or destination.is_dir():
        raise ValueError("Escolha um arquivo em uma pasta existente para exportar.")
    library = os.path.realpath(store.root)
    try:
        inside_library = os.path.commonpath((library, os.path.realpath(destination))) == library
    except ValueError:
        inside_library = False
    if inside_library:
        raise ValueError("Escolha uma pasta fora da biblioteca de reuniões para preservar os arquivos originais.")
    metadata = store.get(session_id, include_events=False)
    if format == "wav":
        tracks = list(metadata.get("tracks", {}))
        if len(tracks) != 1:
            raise ValueError("Selecione WAV do microfone ou WAV do sistema para exportar uma fonte por arquivo.")
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
        raise ValueError("A fonte escolhida não contém áudio no intervalo do destaque.")
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
                "A fonte mudou de formato durante o destaque. Exporte os segmentos originais separadamente."
            )
        pcm = _pcm16(raw)
        if data_length + len(pcm) > RIFF_LIMIT:
            raise ValueError("O áudio excede o limite de 4 GiB do WAV.")
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
        raise ValueError("A proveniência do destaque não é serializável.") from error
    if len(provenance_bytes) > RIFF_LIMIT:
        raise ValueError("A proveniência excede o limite de 4 GiB do formato WAV.")
    handle.write(b"svpr" + struct.pack("<I", len(provenance_bytes)) + provenance_bytes)
    if len(provenance_bytes) & 1:
        handle.write(b"\0")
    file_end = handle.tell()
    riff_length = file_end - 8
    if riff_length > RIFF_LIMIT:
        raise ValueError("O áudio excede o limite de 4 GiB do WAV.")
    for position, length in ((4, riff_length), (data_header + 4, data_length)):
        handle.seek(position)
        handle.write(struct.pack("<I", length))
    handle.seek(file_end)


def export_highlight_clip(store, session_id, highlight, path, cancel_event=None):
    """Export one source-track highlight as an atomic, non-overwriting PCM16 WAV."""
    if not isinstance(highlight, dict):
        raise ValueError("O destaque deve ser um objeto.")
    track = highlight.get("track")
    start, end = highlight.get("start"), highlight.get("end")
    if track not in {"microphone", "system"}:
        raise ValueError("A fonte do destaque é inválida.")
    if (isinstance(start, bool) or isinstance(end, bool)
            or not isinstance(start, (int, float)) or not isinstance(end, (int, float))
            or not math.isfinite(start) or not math.isfinite(end)
            or start < 0 or end <= start):
        raise ValueError("O intervalo do destaque é inválido.")
    destination = Path(path).absolute()
    if not destination.parent.is_dir() or destination.is_dir() or os.path.lexists(destination):
        if os.path.lexists(destination):
            raise FileExistsError("O arquivo de destino já existe; escolha um novo nome para preservar o clipe anterior.")
        raise ValueError("Escolha um arquivo em uma pasta existente para exportar.")
    library = os.path.realpath(store.root)
    try:
        inside_library = os.path.commonpath((library, os.path.realpath(destination))) == library
    except ValueError:
        inside_library = False
    if inside_library:
        raise ValueError("Escolha uma pasta fora da biblioteca de reuniões para preservar os arquivos originais.")
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


def play_audio(store, session_id, track, start, cancel_event):
    if track not in {"microphone", "system"} or not isinstance(start, (int, float)) or isinstance(start, bool) or not math.isfinite(start) or start < 0:
        raise ValueError("Escolha uma fonte e um instante de reprodução válido.")
    if cancel_event.is_set():
        return
    try:
        import sounddevice
    except ImportError as error:
        raise RuntimeError("A reprodução exige o runtime de áudio local já provisionado. Use uma instalação completa do Snipvoice.") from error
    stream = None
    current_format = None
    metadata = store.get(session_id, include_events=False)
    try:
        for rate, channels, payload in _audio_chunks(store, session_id, track, float(start), duration=metadata.get("duration")):
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
