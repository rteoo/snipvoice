"""Lazy local speech activity detection for bounded ASR chunks."""

from __future__ import annotations

import math
from pathlib import Path


class VoiceActivityError(RuntimeError):
    """The configured speech detector could not produce a safe decision."""


class SileroVoiceActivity:
    """Small ONNX Silero VAD adapter with bounded state per ASR chunk.

    The detector is intentionally lazy: importing or constructing a provider
    never loads ONNX Runtime or the model. ``runtime`` is injectable so tests
    can exercise the state and hysteresis contract without a model download.
    """

    def __init__(self, model_path=None, runtime=None, sample_rate=16000,
                 frame_samples=512, speech_threshold=0.35, min_speech_frames=2):
        if sample_rate != 16000:
            raise ValueError("Silero VAD requires 16 kHz PCM")
        if frame_samples != 512:
            raise ValueError("Silero VAD requires 512-sample frames at 16 kHz")
        if not 0.0 < float(speech_threshold) < 1.0:
            raise ValueError("Silero VAD threshold is invalid")
        if not isinstance(min_speech_frames, int) or not 1 <= min_speech_frames <= 8:
            raise ValueError("Silero VAD hysteresis is invalid")
        self.model_path = Path(model_path) if model_path else None
        self.runtime = runtime
        self.sample_rate = sample_rate
        self.frame_samples = frame_samples
        self.speech_threshold = float(speech_threshold)
        self.min_speech_frames = min_speech_frames
        self._session = None
        self._state = None

    def _resolve_model(self):
        if self.model_path is not None:
            if self.model_path.is_file():
                return self.model_path
        else:
            try:
                from voice_activity_model import verified_model_path
                return verified_model_path()
            except (ImportError, OSError, ValueError) as exc:
                raise VoiceActivityError(
                    "Local speech detection is unavailable: the verified Silero VAD model was not found."
                ) from exc
        raise VoiceActivityError(
            "Local speech detection is unavailable: the verified Silero VAD model was not found."
        )

    def _load(self):
        if self._session is not None:
            return
        runtime = self.runtime
        if runtime is None:
            try:
                import onnxruntime as runtime
            except Exception as exc:
                raise VoiceActivityError(
                    "Local speech detection is unavailable: ONNX Runtime is not installed."
                ) from exc
        model = self._resolve_model()
        try:
            options_type = getattr(runtime, "SessionOptions", None)
            options = options_type() if options_type is not None else None
            if options is not None:
                options.intra_op_num_threads = 1
                options.inter_op_num_threads = 1
            kwargs = {"providers": ["CPUExecutionProvider"]}
            if options is not None:
                kwargs["sess_options"] = options
            self._session = runtime.InferenceSession(str(model), **kwargs)
        except Exception as exc:
            raise VoiceActivityError("The local speech detector could not load its verified model.") from exc
        self._state = None

    @staticmethod
    def _input_names(session):
        names = [item.name for item in session.get_inputs()]
        if len(names) < 3:
            raise VoiceActivityError("The local speech detector model has an unsupported input schema.")
        lower = {name.lower(): name for name in names}
        audio = next((name for key, name in lower.items() if "input" in key and "state" not in key), names[0])
        state = next((name for key, name in lower.items() if "state" in key), names[1])
        rate = next((name for key, name in lower.items() if key in {"sr", "sample_rate", "sampling_rate"}), names[2])
        return audio, state, rate

    def speech_ranges(self, samples, cancel_event=None):
        if cancel_event is not None and cancel_event.is_set():
            raise VoiceActivityError("Speech detection was cancelled.")
        self._load()
        try:
            import numpy as np
        except Exception as exc:
            raise VoiceActivityError("Local speech detection is unavailable: NumPy is not installed.") from exc
        values = np.asarray(samples, dtype=np.float32).reshape(-1)
        if values.size == 0:
            return ()
        if not np.isfinite(values).all():
            raise VoiceActivityError("The speech detector received invalid audio samples.")
        audio_name, state_name, rate_name = self._input_names(self._session)
        state = np.zeros((2, 1, 128), dtype=np.float32)
        # Silero's 16 kHz ONNX wrapper evaluates each 512-sample window with
        # the preceding 64 samples prepended.  The recurrent state alone is
        # insufficient; omitting this context materially changes probabilities
        # at every frame boundary.
        context = np.zeros(64, dtype=np.float32)
        run_length = int(values.size)
        probabilities = []
        for offset in range(0, run_length, self.frame_samples):
            if cancel_event is not None and cancel_event.is_set():
                raise VoiceActivityError("Speech detection was cancelled.")
            frame = values[offset:offset + self.frame_samples]
            if frame.size < self.frame_samples:
                frame = np.pad(frame, (0, self.frame_samples - frame.size))
            model_frame = np.concatenate((context, frame))
            output = self._session.run(None, {
                audio_name: model_frame.reshape(1, -1),
                state_name: state,
                rate_name: np.asarray([self.sample_rate], dtype=np.int64),
            })
            if len(output) < 2:
                raise VoiceActivityError("The speech detector returned an invalid result.")
            try:
                probability_values = np.asarray(output[0]).reshape(-1)
                probability = float(probability_values[0])
            except (IndexError, TypeError, ValueError) as exc:
                raise VoiceActivityError("The speech detector returned an invalid probability.") from exc
            state = np.asarray(output[1], dtype=np.float32)
            if state.shape != (2, 1, 128):
                raise VoiceActivityError("The speech detector returned an invalid state.")
            context = model_frame[-64:]
            if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
                raise VoiceActivityError("The speech detector returned an invalid probability.")
            probabilities.append(probability)
        ranges = []
        run_start = None
        high_frames = 0
        quiet_frames = 0
        for index, probability in enumerate(probabilities):
            if probability >= self.speech_threshold:
                high_frames += 1
                quiet_frames = 0
                if run_start is None and high_frames >= self.min_speech_frames:
                    run_start = index - self.min_speech_frames + 1
                continue
            high_frames = 0
            if run_start is not None:
                quiet_frames += 1
                if quiet_frames >= self.min_speech_frames:
                    ranges.append((run_start * self.frame_samples,
                                   min(run_length, (index - quiet_frames + 1) * self.frame_samples)))
                    run_start = None
                    quiet_frames = 0
        if run_start is not None:
            ranges.append((run_start * self.frame_samples, run_length))
        padding = int(0.2 * self.sample_rate)
        padded = []
        for start, end in ranges:
            start = max(0, start - padding)
            end = min(run_length, end + padding)
            if padded and start <= padded[-1][1]:
                padded[-1] = (padded[-1][0], max(padded[-1][1], end))
            else:
                padded.append((start, end))
        return tuple(padded)

    def detect(self, samples, cancel_event=None):
        return bool(self.speech_ranges(samples, cancel_event))

    def voiced_samples(self, samples, cancel_event=None):
        """Return only voiced ranges with short context padding."""
        import numpy as np
        values = np.asarray(samples, dtype=np.float32).reshape(-1)
        ranges = self.speech_ranges(values, cancel_event)
        if not ranges:
            return None
        return np.concatenate([values[start:end] for start, end in ranges])

    def reset(self):
        self._state = None
