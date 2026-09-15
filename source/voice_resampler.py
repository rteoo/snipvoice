"""Streaming conversion to the canonical voice PCM format.

The provider boundary consumes mono float32 samples at 16 kHz.  Microphones
often expose 44.1 or 48 kHz instead, so conversion must retain its fractional
position across callback-sized chunks and flush the delayed tail exactly once.
``soxr`` is deliberately imported only when conversion is needed; source
startup remains usable when the optional voice dependency is absent.
"""

import math


TARGET_SAMPLE_RATE = 16000


class VoiceResamplerError(RuntimeError):
    """A user-actionable sample-rate conversion failure."""


def _valid_rate(value):
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(rate) or rate <= 0:
        return None
    return rate


class StreamingResampler:
    """Convert mono float32 chunks to 16 kHz without resetting DSP state."""

    def __init__(self, source_rate, target_rate=TARGET_SAMPLE_RATE):
        source_rate = _valid_rate(source_rate)
        target_rate = _valid_rate(target_rate)
        if source_rate is None or target_rate is None:
            raise VoiceResamplerError("A taxa de amostragem do microfone é inválida.")
        self.source_rate = source_rate
        self.target_rate = target_rate
        self._finished = False
        self._passthrough = source_rate == target_rate
        self._numpy = None
        self._stream = None
        if not self._passthrough:
            try:
                import soxr
                import numpy as np
            except Exception as exc:
                raise VoiceResamplerError(
                    "O conversor de áudio não está instalado. "
                    "Reinstale o pacote de voz para habilitar este microfone."
                ) from exc
            try:
                self._numpy = np
                self._stream = soxr.ResampleStream(
                    source_rate,
                    target_rate,
                    1,
                    dtype="float32",
                    quality="HQ",
                )
            except Exception as exc:
                raise VoiceResamplerError(
                    f"Não foi possível preparar o conversor de áudio: {exc}"
                ) from exc

    def push(self, samples):
        """Convert one chunk, retaining state for the next chunk."""
        if self._finished:
            raise VoiceResamplerError("O conversor de áudio já foi encerrado.")
        values = _as_float_list(samples)
        if self._passthrough:
            return values
        if not values:
            return []
        try:
            array = self._numpy.asarray(values, dtype=self._numpy.float32)
            output = self._stream.resample_chunk(array, last=False)
            return _as_float_list(output)
        except Exception as exc:
            raise VoiceResamplerError(
                f"Falha ao normalizar o áudio do microfone: {exc}"
            ) from exc

    def finish(self):
        """Flush delayed samples once and close the conversion stream."""
        if self._finished:
            return []
        self._finished = True
        if self._passthrough:
            return []
        try:
            empty = self._numpy.empty((0,), dtype=self._numpy.float32)
            output = self._stream.resample_chunk(empty, last=True)
            return _as_float_list(output)
        except Exception as exc:
            raise VoiceResamplerError(
                f"Falha ao finalizar a normalização do áudio: {exc}"
            ) from exc


def _as_float_list(values):
    if values is None:
        return []
    if hasattr(values, "reshape"):
        values = values.reshape(-1)
    elif hasattr(values, "flatten"):
        values = values.flatten()
    return [float(value) for value in values]
