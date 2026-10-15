"""Bounded, local transcription jobs for durable meeting recordings."""

import math
import os
import struct
import threading

from i18n import N_, tr
from voice_provider import LocalVoiceProvider
from voice_resampler import StreamingResampler


TARGET_RATE = 16000
CHUNK_SECONDS = 30.0
MAX_CHUNK_SAMPLES = int(TARGET_RATE * CHUNK_SECONDS)
_FLOAT = struct.Struct("<f")


class MeetingTranscriptionError(RuntimeError):
    """A local transcription job could not complete."""

    def __init__(self, message, resource_live=False):
        super().__init__(message)
        self.resource_live = resource_live


def _pcm_to_mono(payload, channels):
    if not isinstance(channels, int) or channels < 1:
        raise MeetingTranscriptionError(tr("O formato do canal de áudio é inválido."))
    if len(payload) % (_FLOAT.size * channels):
        raise MeetingTranscriptionError(tr("O bloco de áudio está incompleto."))
    values = []
    for offset in range(0, len(payload), _FLOAT.size * channels):
        total = 0.0
        for channel in range(channels):
            total += _FLOAT.unpack_from(payload, offset + channel * _FLOAT.size)[0]
        values.append(total / channels)
    return values


class MeetingTranscriber:
    """Process a meeting one bounded source chunk at a time.

    ``provider`` is an injected ``VoiceProvider``.  The provider is prepared
    with ``allow_download=False`` so opening a meeting can never start a
    network download.  Jobs on one transcriber serialize native inference.
    """

    def __init__(self, store, provider, resampler_factory=StreamingResampler,
                 chunk_seconds=CHUNK_SECONDS, final_audio_decoder=None):
        self.store = store
        self.provider = provider
        self.resampler_factory = resampler_factory
        if not isinstance(chunk_seconds, (int, float)) or not 0.1 <= chunk_seconds <= 30.0:
            raise ValueError(tr("O tamanho do bloco de transcrição é inválido."))
        self.chunk_seconds = float(chunk_seconds)
        self._max_chunk_samples = min(MAX_CHUNK_SAMPLES, int(TARGET_RATE * self.chunk_seconds))
        self.final_audio_decoder = final_audio_decoder
        self._lock = threading.Lock()

    def transcribe(self, session_id, profile, language, cancel_event=None, revision=None):
        """Transcribe all saved tracks, returning the processing revision ID."""
        cancel_event = cancel_event or threading.Event()
        if not self._lock.acquire(blocking=False):
            raise MeetingTranscriptionError(tr("Já existe uma transcrição em andamento."))
        if revision is None:
            try:
                revision = self.store.begin_revision(session_id, profile, language)
            except Exception:
                self._lock.release()
                raise
            completed_ids = set()
        else:
            try:
                metadata = self.store.get(session_id)
            except Exception:
                self._lock.release()
                raise
            revision_item = next((item for item in metadata.get("revisions", []) if item.get("id") == revision), None)
            if revision_item is None:
                self._lock.release()
                raise ValueError(tr("A revisão de transcrição não foi encontrada."))
            if revision_item.get("status") not in {"processing", "pending", "failed", "cancelled"}:
                self._lock.release()
                raise ValueError(tr("A revisão de transcrição já foi encerrada."))
            if revision_item.get("profile") != profile or revision_item.get("language") != language:
                self._lock.release()
                raise ValueError(tr("Retome com o mesmo modelo e idioma ou crie uma nova revisão."))
            # An interrupted JSONL may end in an incomplete line. Preserve its bytes
            # and copy only the valid prefix into a fresh revision before appending.
            previous = revision
            try:
                revision = self.store.begin_revision(session_id, profile, language)
                source_tracks = self._source_tracks(metadata)
                previous_segments = list(self.store.get_transcript(session_id, previous))
                previous_tracks = {
                    str(segment.get("track")) for segment in previous_segments
                    if isinstance(segment, dict) and isinstance(segment.get("track"), str)
                }
                # Mixed final audio cannot safely resume source-specific
                # microphone/system segments. Preserve the old revision, but
                # start this revision clean when the source kind changes.
                copy_previous = previous_tracks.issubset(set(source_tracks))
                completed_ids = set()
                for segment in previous_segments if copy_previous else ():
                    if isinstance(segment.get("id"), str):
                        segment = dict(segment, revision=revision)
                        self.store.add_transcript(session_id, revision, segment)
                        completed_ids.add(segment["id"])
                self.store.finish_revision(session_id, previous, "superseded",
                                           N_("Retomada em uma nova revisão; os resultados originais foram preservados."))
            except Exception:
                self._lock.release()
                raise

        try:
            try:
                if cancel_event.is_set():
                    raise _Cancelled
                self.provider.prepare(
                    profile,
                    language,
                    cancel_event=cancel_event,
                    allow_download=False,
                )
                metadata = self.store.get(session_id, include_events=False)
                tracks = self._source_tracks(metadata)
                wrote_audio = False
                for track in tracks:
                    if cancel_event.is_set():
                        raise _Cancelled
                    wrote_audio = self._transcribe_track(
                        session_id,
                        track,
                        profile,
                        language,
                        revision,
                        completed_ids,
                        cancel_event,
                    ) or wrote_audio
                if not wrote_audio:
                    raise MeetingTranscriptionError(
                        tr("A fonte de áudio não contém blocos decodificáveis para transcrição.")
                    )
                self.store.finish_revision(session_id, revision, "completed")
                return revision
            except _Cancelled:
                self.store.finish_revision(session_id, revision, "cancelled", N_("A transcrição foi cancelada."))
                self.provider.cancel()
                return revision
            except Exception as exc:
                self.store.finish_revision(session_id, revision, "failed", str(exc))
                raise
        finally:
            try:
                self.provider.unload()
            except Exception as exc:
                raise MeetingTranscriptionError(
                    tr("A transcrição terminou, mas o modelo local não foi liberado; feche a reunião antes de tentar outra."),
                    resource_live=True,
                ) from exc
            finally:
                self._lock.release()

    def _source_tracks(self, metadata):
        final = metadata.get("final_audio") if isinstance(metadata, dict) else None
        path = final.get("path") if isinstance(final, dict) else None
        if isinstance(path, str) and path and os.path.isfile(path):
            return ("final",)
        return ("microphone", "system")

    def _source_events(self, session_id, track, cancel_event):
        if track != "final":
            yield from self.store.iter_audio(session_id, track=track)
            return
        metadata = self.store.get(session_id, include_events=False)
        decoder = self.final_audio_decoder
        if decoder is None:
            from meeting_files import _final_audio_chunks
            decoder = _final_audio_chunks
        timestamp = 0.0
        sequence = 0
        saw_audio = False
        try:
            for item in decoder(metadata, 0.0, cancel_event):
                if cancel_event.is_set():
                    raise _Cancelled
                try:
                    rate, channels, payload = item
                    frames = len(payload) // (channels * 4)
                except (TypeError, ValueError, ZeroDivisionError) as exc:
                    raise MeetingTranscriptionError(
                        tr("O decodificador do áudio final retornou um bloco inválido.")
                    ) from exc
                if (not isinstance(rate, int) or not 8000 <= rate <= 192000
                        or not isinstance(channels, int) or channels not in (1, 2)
                        or frames <= 0 or len(payload) != frames * channels * 4):
                    raise MeetingTranscriptionError(
                        tr("O decodificador do áudio final retornou um formato incompatível.")
                    )
                saw_audio = True
                yield {
                    "type": "audio", "generation": 0, "track": "final",
                    "rate": rate, "channels": channels, "frames": frames,
                    "timestamp": timestamp, "sequence": sequence,
                }, payload
                timestamp += frames / rate
                sequence += 1
        except _Cancelled:
            raise
        except Exception:
            if cancel_event.is_set():
                raise _Cancelled
            raise
        if not saw_audio:
            raise MeetingTranscriptionError(
                tr("O áudio final não contém blocos decodificáveis para transcrição.")
            )

    def _transcribe_track(self, session_id, track, profile, language, revision, completed_ids, cancel_event):
        samples = []
        chunk_start = None
        chunk_end = None
        source_rate = None
        resampler = None
        expected_next = None

        def flush():
            nonlocal samples, chunk_start, chunk_end, resampler
            if chunk_start is None:
                return
            if resampler is not None:
                samples.extend(resampler.finish())
            if samples:
                segment_id = self._segment_id(track, chunk_start, chunk_end)
                if segment_id not in completed_ids:
                    self._write_segment(
                        session_id,
                        track,
                        profile,
                        language,
                        revision,
                        segment_id,
                        chunk_start,
                        chunk_end,
                        samples,
                        cancel_event,
                    )
                    completed_ids.add(segment_id)
            samples = []
            chunk_start = None
            chunk_end = None
            resampler = None

        wrote_audio = False
        for event, payload in self._source_events(session_id, track, cancel_event):
            if cancel_event.is_set():
                raise _Cancelled
            wrote_audio = True
            rate = event["rate"]
            event_start = float(event["timestamp"])
            event_end = event_start + event["frames"] / rate
            if chunk_start is not None and (
                source_rate != rate
                or (expected_next is not None and event_start > expected_next + 0.05)
                or event_end - chunk_start > self.chunk_seconds
            ):
                flush()
            if chunk_start is None:
                chunk_start = event_start
                source_rate = rate
                resampler = self.resampler_factory(rate, TARGET_RATE)
            values = _pcm_to_mono(payload, event["channels"])
            samples.extend(resampler.push(values))
            chunk_end = event_end
            expected_next = event_end
            if len(samples) >= self._max_chunk_samples:
                flush()
                expected_next = None
        flush()
        return wrote_audio

    def _write_segment(self, session_id, track, profile, language, revision, segment_id, start, end, samples, cancel_event):
        if cancel_event.is_set():
            raise _Cancelled
        # Silent chunks remain represented with an empty result, which lets a
        # resumed job distinguish processed silence from a missing chunk.
        energy = math.sqrt(sum(value * value for value in samples) / len(samples)) if samples else 0.0
        raw_text = "" if energy < 1e-5 else self.provider.transcribe(samples, cancel_event=cancel_event)
        text = "" if raw_text is None else str(raw_text).strip()
        self.store.add_transcript(
            session_id,
            revision,
            {
                "id": segment_id,
                "track": track,
                "start": start,
                "end": end,
                "text": text,
                "raw_text": "" if raw_text is None else str(raw_text),
                "timing_precision": "chunk",
                "profile": profile,
                "language": language,
                "revision": revision,
            },
        )

    @staticmethod
    def _segment_id(track, start, end):
        return "%s:%0.6f:%0.6f" % (track, start, end)


class _Cancelled(Exception):
    pass


def transcribe_meeting(
    store,
    session_id,
    profile,
    language,
    cache_dir,
    cancel_event=None,
    revision=None,
    reprocess=False,
):
    """Run one manually requested, installed only local transcription job.

    A matching processing revision is resumed by default.  ``reprocess``
    explicitly creates a new revision while preserving previous results.
    """
    if revision is None and not reprocess:
        metadata = store.get(session_id)
        for item in reversed(metadata.get("revisions", [])):
            if (
                item.get("status") in {"processing", "pending", "failed", "cancelled"}
                and item.get("profile") == profile
                and item.get("language") == language
            ):
                revision = item.get("id")
                break
    provider = LocalVoiceProvider(cache_dir)
    return MeetingTranscriber(store, provider).transcribe(
        session_id,
        profile,
        language,
        cancel_event=cancel_event,
        revision=revision,
    )


__all__ = [
    "MeetingTranscriber",
    "MeetingTranscriptionError",
    "transcribe_meeting",
    "CHUNK_SECONDS",
    "MAX_CHUNK_SAMPLES",
]
