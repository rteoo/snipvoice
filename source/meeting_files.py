"""Worker-owned, bounded WAV import, provenance exports, and native playback."""

import array
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


def _events(store, session, metadata):
    if hasattr(store, "iter_events"):
        return store.iter_events(session)
    return iter(metadata.get("events", ()))


def _json(handle, value):
    for piece in json.JSONEncoder(ensure_ascii=False, allow_nan=False).iterencode(value):
        handle.write(piece)


def _json_export(handle, store, session, metadata, cancel_event=None):
    _cancel(cancel_event)
    handle.write('{"metadata":')
    _json(handle, {key: value for key, value in metadata.items() if key != "events"})
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
