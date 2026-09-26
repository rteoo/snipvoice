"""Streaming local mixdown for the microphone and system meeting tracks.

The meeting store keeps the two sources as timestamped little-endian float32
blocks. This module derives an MP3 or PCM16 WAV without changing the source
recordings. Mixing stays bounded; MP3 encoding uses the bundled PyAV/LAME
runtime and WAV export requires only the standard library.
"""

from __future__ import annotations

from contextlib import contextmanager
from fractions import Fraction
import math
import os
from pathlib import Path
import struct
import tempfile

from i18n import tr


OUTPUT_CHUNK_FRAMES = 4096
MAX_OUTPUT_BYTES = 0xFFFFFFFF
_FLOAT_BYTES = 4
_PCM16 = struct.Struct("<h")
MP3_RATES = (8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000)


class MixdownCancelled(RuntimeError):
    """Raised when a caller cancels before the atomic destination replace."""


def _cancel(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise MixdownCancelled(
            tr("A mixagem foi cancelada; as gravações originais e a saída existente foram preservadas.")
        )


def _float_to_pcm16(value):
    if not math.isfinite(value):
        value = 0.0
    value = max(-1.0, min(1.0, value))
    return -32768 if value <= -1.0 else int(round(value * 32767.0))


def _validate_event(event, payload, track):
    if not isinstance(event, dict) or event.get("type") != "audio":
        raise ValueError(tr("A fonte {track} contém um evento que não é áudio.", track=track))
    if event.get("track") != track:
        raise ValueError(tr("A fonte {track} contém um rótulo de faixa inesperado.", track=track))
    rate = event.get("rate")
    channels = event.get("channels")
    frames = event.get("frames")
    timestamp = event.get("timestamp")
    if (not isinstance(rate, int) or isinstance(rate, bool) or not 1 <= rate <= 192000
            or not isinstance(channels, int) or isinstance(channels, bool) or channels not in (1, 2)
            or not isinstance(frames, int) or isinstance(frames, bool) or frames <= 0
            or not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool)
            or not math.isfinite(float(timestamp)) or timestamp < 0):
        raise ValueError(tr("A fonte {track} tem formato de áudio ou instante incompatível.", track=track))
    try:
        raw = memoryview(payload)
    except TypeError as exc:
        raise ValueError(tr("O bloco da fonte {track} não é compatível com bytes.", track=track)) from exc
    expected = frames * channels * _FLOAT_BYTES
    if raw.nbytes != expected:
        raise ValueError(
            tr("O bloco da fonte {track} está incompleto para a quantidade de frames informada.", track=track))
    return rate, channels, frames, float(timestamp), raw


class _Track:
    """One lazy timestamped source; at most one store event is retained."""

    def __init__(self, name, source):
        self.name = name
        self._source = iter(source)
        self._current = None
        self._last_end = -1.0
        self.rate = None
        self.channels = None
        self.done = False
        self._read_next()

    def _read_next(self):
        if self.done:
            return
        try:
            event, payload = next(self._source)
        except StopIteration:
            self.done = True
            self._current = None
            return
        rate, channels, frames, timestamp, raw = _validate_event(event, payload, self.name)
        if self.rate is None:
            self.rate, self.channels = rate, channels
        elif (rate, channels) != (self.rate, self.channels):
            raise ValueError(
                tr("A fonte {track} altera o formato ({old_rate} Hz/{old_channels} canais "
                   "para {rate} Hz/{channels} canais); converta-a antes da mixagem.",
                   track=self.name, old_rate=self.rate, old_channels=self.channels,
                   rate=rate, channels=channels)
            )
        start = timestamp
        end = start + frames / rate
        if start < self._last_end:
            overlap = self._last_end - start
            # Native packet clocks can differ by a tiny amount after their
            # timestamps are serialized. Snap ordinary clock jitter to the
            # prior block boundary, but reject a material overlap.
            if overlap > max(0.02, 2.0 / rate):
                raise ValueError(tr("A fonte {track} contém blocos sobrepostos ou fora de ordem.", track=self.name))
            start = self._last_end
            end = start + frames / rate
        self._last_end = end
        self._current = (start, end, frames, raw)

    def render(self, first_frame, last_frame, output_rate, output_channels, target):
        """Add this source's overlap into an output float buffer."""
        first_time = first_frame / output_rate
        last_time = last_frame / output_rate
        while not self.done:
            current = self._current
            if current is None:
                self._read_next()
                continue
            start, end, frames, raw = current
            if end <= first_time:
                self._read_next()
                continue
            if start >= last_time:
                return
            overlap_start = max(first_frame, int(math.ceil(start * output_rate - 1e-9)))
            overlap_end = min(last_frame, int(math.ceil(end * output_rate - 1e-9)))
            source_channels = self.channels
            for frame in range(overlap_start, overlap_end):
                position = (frame / output_rate - start) * self.rate
                source_frame = min(frames - 1, max(0, int(math.floor(position))))
                fraction = position - source_frame
                source_offset = source_frame * source_channels * _FLOAT_BYTES
                destination_offset = (frame - first_frame) * output_channels
                if source_channels == 1:
                    value = struct.unpack_from("<f", raw, source_offset)[0]
                    if source_frame + 1 < frames:
                        next_value = struct.unpack_from("<f", raw, source_offset + _FLOAT_BYTES)[0]
                        value += (next_value - value) * fraction
                    for channel in range(output_channels):
                        target[destination_offset + channel] += value
                else:
                    for channel in range(output_channels):
                        value = struct.unpack_from(
                            "<f", raw, source_offset + channel * _FLOAT_BYTES
                        )[0]
                        if source_frame + 1 < frames:
                            next_value = struct.unpack_from(
                                "<f", raw, source_offset + source_channels * _FLOAT_BYTES + channel * _FLOAT_BYTES
                            )[0]
                            value += (next_value - value) * fraction
                        target[destination_offset + channel] += value
            if end <= last_time:
                self._read_next()
            else:
                return


class _MicEnhancer:
    """Apply a bounded gain without deleting quiet speech below a hard gate."""

    def __init__(self, gain=1.5):
        self.gain = gain
        self.limit = 0.95

    def apply(self, samples, channels):
        for index in range(0, len(samples), channels):
            for channel in range(channels):
                value = samples[index + channel]
                samples[index + channel] = max(-self.limit, min(self.limit, value * self.gain))


def _adaptive_microphone_gain(source, cancel_event):
    """Measure a recording in bounded memory before deriving its final audio."""
    histogram = [0] * 97  # 1 dB RMS buckets from -96 through 0 dBFS.
    windows = 0
    peak = 0.0
    count = 0
    squared = 0.0
    native_format = None

    def add_window():
        nonlocal windows
        rms = math.sqrt(squared / count)
        # Measure audible windows, not the time the microphone was silent.
        # This only controls gain analysis; no source samples are gated out.
        if rms >= 0.002:
            bucket = max(0, min(96, int(math.floor(96 + 20 * math.log10(rms)))))
            histogram[bucket] += 1
            windows += 1

    for event, payload in source:
        _cancel(cancel_event)
        rate, channels, _, _, _ = _validate_event(event, payload, "microphone")
        if native_format is not None and native_format != (rate, channels):
            raise ValueError(tr("O formato do microfone mudou durante a gravação; converta-o antes de ajustar o volume."))
        native_format = (rate, channels)
        window_values = max(1, event["rate"] // 10) * event["channels"]
        for (value,) in struct.iter_unpack("<f", payload):
            if math.isfinite(value):
                amplitude = min(1.0, abs(value))
                peak = max(peak, amplitude)
                squared += amplitude * amplitude
            count += 1
            if count == window_values:
                add_window()
                count = 0
                squared = 0.0
    if count:
        add_window()
    if not windows or not peak:
        return 1.0
    rank = math.ceil(windows * 0.9)
    seen = 0
    percentile = 0.0
    for bucket, amount in enumerate(histogram):
        seen += amount
        if seen >= rank:
            percentile = 10 ** ((bucket - 95) / 20)
            break
    if percentile < 0.002:
        return 1.0
    # Aim for speech around -24 dBFS while preserving transient headroom.
    return max(1.0, min(8.0, (10 ** (-24 / 20)) / percentile, 0.95 / peak))


def _write_header(handle, rate, channels):
    handle.write(b"RIFF\x00\x00\x00\x00WAVEfmt ")
    handle.write(struct.pack("<IHHIIHH", 16, 1, channels, rate, rate * channels * 2, channels * 2, 16))
    handle.write(b"data\x00\x00\x00\x00")


def _finish_header(handle, data_bytes):
    if data_bytes > MAX_OUTPUT_BYTES - 36:
        raise ValueError(tr("A mixagem excede o limite de 4 GiB do WAV PCM."))
    end = handle.tell()
    handle.seek(4)
    handle.write(struct.pack("<I", 36 + data_bytes))
    handle.seek(40)
    handle.write(struct.pack("<I", data_bytes))
    handle.seek(end)


@contextmanager
def _encoded_output(handle, rate, channels, format, cancel_event):
    """Yield a bounded PCM16 sink and finalize the selected container on success."""
    if format == "wav":
        _write_header(handle, rate, channels)
        data_bytes = 0

        def write_pcm(payload):
            nonlocal data_bytes
            if data_bytes + len(payload) > MAX_OUTPUT_BYTES - 36:
                raise ValueError(tr("A mixagem excede o limite de 4 GiB do WAV PCM."))
            handle.write(payload)
            data_bytes += len(payload)

        yield write_pcm
        _cancel(cancel_event)
        _finish_header(handle, data_bytes)
        return

    try:
        import av
        av.codec.Codec("libmp3lame", "w")
    except (ImportError, ValueError) as exc:
        raise RuntimeError(
            tr("A gravação MP3 exige o codificador incluído na instalação completa do Snipvoice. "
               "O áudio original foi preservado; atualize o aplicativo ou exporte como WAV.")
        ) from exc
    output_rate = min(MP3_RATES, key=lambda candidate: abs(candidate - rate))
    layout = "mono" if channels == 1 else "stereo"
    with av.open(handle, mode="w", format="mp3") as container:
        stream = container.add_stream("libmp3lame", rate=output_rate)
        stream.layout = layout
        stream.codec_context.format = "fltp"
        if output_rate < 16000:
            stream.bit_rate = 32000 if channels == 1 else 64000
        elif output_rate < 32000:
            stream.bit_rate = 64000 if channels == 1 else 96000
        else:
            stream.bit_rate = 128000 if channels == 1 else 192000
        position = 0

        def write_pcm(payload):
            nonlocal position
            _cancel(cancel_event)
            samples = len(payload) // (channels * 2)
            if not samples:
                return
            frame = av.AudioFrame(format="s16", layout=layout, samples=samples)
            frame.planes[0].update(payload)
            frame.sample_rate = rate
            frame.time_base = Fraction(1, rate)
            frame.pts = position
            position += samples
            # PyAV resamples and reframes for the encoder without retaining
            # the complete recording. The original track clocks stay intact.
            for packet in stream.encode(frame):
                _cancel(cancel_event)
                container.mux(packet)

        yield write_pcm
        _cancel(cancel_event)
        for packet in stream.encode(None):
            _cancel(cancel_event)
            container.mux(packet)


def mixdown_tracks(track_sources, destination, *, enhance_microphone=False,
                   microphone_gain=1.5,
                   cancel_event=None, chunk_frames=OUTPUT_CHUNK_FRAMES):
    """Mix timestamped tracks into an atomic MP3 or WAV selected by extension.

    ``track_sources`` is a mapping whose values yield ``(event, float32
    payload)`` pairs, matching :meth:`MeetingStore.iter_audio`.  The output
    starts at session time zero and ends at the last source frame.  Missing
    sources are valid, but an empty mapping or all-empty sources is not.
    """
    if not hasattr(track_sources, "items"):
        raise TypeError("track_sources deve ser um mapa de faixas para iteráveis.")
    unknown = set(track_sources) - {"microphone", "system"}
    if unknown:
        raise ValueError(tr("Somente as fontes microfone e sistema são suportadas."))
    if not isinstance(chunk_frames, int) or isinstance(chunk_frames, bool) or not 1 <= chunk_frames <= 65536:
        raise ValueError("chunk_frames deve ser um inteiro entre 1 e 65536.")
    if (isinstance(microphone_gain, bool) or not isinstance(microphone_gain, (int, float))
            or not math.isfinite(microphone_gain) or not 1 <= microphone_gain <= 8):
        raise ValueError(tr("O ganho do microfone deve estar entre 1 e 8."))
    _cancel(cancel_event)
    tracks = [_Track(name, source) for name, source in track_sources.items() if source is not None]
    tracks = [track for track in tracks if not track.done]
    if not tracks:
        raise ValueError(tr("Pelo menos uma fonte deve conter áudio."))
    # A common highest-rate clock preserves the native timing and lets lower
    # rate sources use bounded linear interpolation during rendering.
    rate = max(track.rate for track in tracks)
    output_channels = max(track.channels for track in tracks)
    destination = Path(destination).absolute()
    format = destination.suffix.lower().lstrip(".")
    if format not in {"mp3", "wav"}:
        raise ValueError(tr("Escolha um arquivo MP3 ou WAV para salvar o áudio final."))
    if not destination.parent.is_dir() or destination.is_dir():
        raise ValueError(tr("A pasta destino deve existir e o destino deve ser um arquivo."))

    descriptor, temporary = tempfile.mkstemp(
        prefix="." + destination.name + "-", suffix=".tmp", dir=destination.parent
    )
    try:
        with os.fdopen(descriptor, "w+b") as handle, _encoded_output(
                handle, rate, output_channels, format, cancel_event) as write_pcm:
            enhancer = _MicEnhancer(microphone_gain) if enhance_microphone else None
            frame = 0
            # ceiling: output chunks are capped at 4,096 frames (~32 KiB for
            # stereo PCM16); increase only with measured memory profiling.
            while not all(track.done and track._current is None for track in tracks):
                _cancel(cancel_event)
                next_end = frame + chunk_frames
                samples = [0.0] * ((next_end - frame) * output_channels)
                for track in tracks:
                    contribution = samples
                    if enhancer is not None and track.name == "microphone":
                        contribution = [0.0] * ((next_end - frame) * output_channels)
                    track.render(frame, next_end, rate, output_channels, contribution)
                    if contribution is not samples:
                        enhancer.apply(contribution, output_channels)
                        for index, value in enumerate(contribution):
                            samples[index] += value
                all_done = all(track.done and track._current is None for track in tracks)
                write_frames = next_end - frame
                if all_done:
                    final_end = max(track._last_end for track in tracks)
                    final_end_frame = int(math.ceil(final_end * rate - 1e-9))
                    write_frames = max(0, min(next_end, final_end_frame) - frame)
                    samples = samples[:write_frames * output_channels]
                encoded = bytearray(len(samples) * 2)
                for index, value in enumerate(samples):
                    _PCM16.pack_into(encoded, index * 2, _float_to_pcm16(value))
                write_pcm(encoded)
                frame = next_end
                if all_done:
                    break
            _cancel(cancel_event)
        # Container trailers and encoder delay metadata are written by the
        # output context before the atomic publication.
        with open(temporary, "r+b") as completed:
            os.fsync(completed.fileno())
        _cancel(cancel_event)
        os.replace(temporary, destination)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return str(destination)


def export_mixdown(store, session_id, destination, *, enhance_microphone=False,
                   cancel_event=None, chunk_frames=OUTPUT_CHUNK_FRAMES):
    """Export one meeting's two store tracks without retaining the session."""
    destination = Path(destination).absolute()
    library = os.path.realpath(store.root)
    try:
        inside_library = os.path.commonpath((library, os.path.realpath(destination))) == library
    except ValueError:
        inside_library = False
    if inside_library:
        raise ValueError(tr("Escolha um destino fora da biblioteca de reuniões para preservar as gravações originais."))
    microphone_gain = (
        _adaptive_microphone_gain(store.iter_audio(session_id, "microphone"), cancel_event)
        if enhance_microphone else 1.5
    )
    sources = {
        track: store.iter_audio(session_id, track)
        for track in ("microphone", "system")
    }
    return mixdown_tracks(
        sources,
        destination,
        enhance_microphone=enhance_microphone,
        microphone_gain=microphone_gain,
        cancel_event=cancel_event,
        chunk_frames=chunk_frames,
    )


mixdown_meeting = export_mixdown


__all__ = ["MixdownCancelled", "OUTPUT_CHUNK_FRAMES", "export_mixdown", "mixdown_meeting", "mixdown_tracks"]
