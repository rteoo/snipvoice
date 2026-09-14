"""Microphone capture and normalization for push-to-talk voice input.

PortAudio callbacks do only bounded raw-block copies. A worker owns channel
mixing, sample-rate conversion, canonical journaling, and shutdown draining so
the callback never performs disk I/O or DSP. The public result keeps partial
audio for history while making every capture issue explicit to the controller.
"""

import enum
import math
import queue
import threading

from voice_resampler import StreamingResampler, VoiceResamplerError


SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 4  # float32
# ceiling: 30 s of 16 kHz mono float32 (~1.9 MB). Longer holds cancel rather
# than grow without bound. Raise if real dictation needs longer takes.
MAX_SAMPLES = SAMPLE_RATE * 30
BLOCK_SIZE = 1024
QUEUE_MAX_CHUNKS = (MAX_SAMPLES + BLOCK_SIZE - 1) // BLOCK_SIZE
# ceiling: normalized streaming chunks allow native rates up to 512 kHz for
# the 16 kHz target. Raise only if supported hardware exceeds that ratio.
NORMALIZED_QUEUE_RATE_RATIO = 32


class VoiceAudioError(Exception):
    """User-visible capture failure."""


class CaptureIssue(str, enum.Enum):
    """Stable categories for a capture that cannot be transcribed safely."""

    DURATION_LIMIT = "duration_limit"
    INPUT_STATUS = "input_status"
    RAW_QUEUE = "raw_queue"
    NORMALIZED_QUEUE = "normalized_queue"
    NORMALIZATION = "normalization"
    JOURNAL = "journal"
    STOP = "stop"
    CLOSE = "close"


class CaptureResult:
    """Canonical audio and truthful capture outcome.

    Iteration remains a compatibility bridge for older capture callers:
    ``samples, failed = result``. New callers should inspect ``issue`` and
    preserve ``samples`` for failed history entries.
    """

    __slots__ = (
        "samples",
        "issue",
        "issues",
        "message",
        "source_sample_rate",
        "source_channels",
        "duration_seconds",
    )

    def __init__(
        self,
        samples=None,
        issue=None,
        message=None,
        source_sample_rate=SAMPLE_RATE,
        source_channels=CHANNELS,
        duration_seconds=0.0,
        issues=None,
    ):
        self.samples = list(samples) if samples is not None else []
        self.issue = issue
        self.issues = tuple(issues or (((issue, message),) if issue else ()))
        self.message = str(message) if message else None
        self.source_sample_rate = source_sample_rate
        self.source_channels = source_channels
        self.duration_seconds = float(duration_seconds or 0.0)

    @property
    def ok(self):
        return self.issue is None

    @property
    def overflow(self):
        """Compatibility alias; new code should use ``issue``."""
        return not self.ok

    def __iter__(self):
        yield self.samples
        yield self.overflow


def sounddevice_available():
    try:
        import sounddevice  # noqa: F401
    except Exception:
        return False
    return True


class AudioCapture:
    """Bounded native-format capture normalized to mono 16 kHz float32."""

    _normalizer_slot_lock = threading.Lock()
    _normalizer_slot_owner = None

    def __init__(self, max_samples=MAX_SAMPLES, queue_max=None, device=None):
        self.max_samples = max(1, int(max_samples))
        self.max_duration_seconds = self.max_samples / SAMPLE_RATE
        if queue_max is None:
            queue_max = (self.max_samples + BLOCK_SIZE - 1) // BLOCK_SIZE
        self._queue_max = max(1, int(queue_max))
        self._raw_queue = queue.Queue(maxsize=self._queue_max)
        # Resampling can turn one source block into several output blocks.
        self._normalized_queue = queue.Queue(
            maxsize=max(4, self._queue_max * NORMALIZED_QUEUE_RATE_RATIO)
        )
        self._queue = self._normalized_queue  # compatibility alias
        self._overflow = False  # compatibility observation for older callers
        self._source_frame_count = [0]
        self._samples = []
        self._stream = None
        self._started = False
        self._worker = None
        self._journal = None
        self._issue_lock = threading.Lock()
        self._issues = []
        self._native_rate = SAMPLE_RATE
        self._native_channels = CHANNELS
        self._device = device
        self._resampler = None
        self._raw_sentinel = object()
        self._expected_stop = None

    def set_journal(self, journal):
        """Attach an append-only recording journal before ``start()``."""
        if self._started:
            raise VoiceAudioError("A gravação já começou.")
        self._journal = journal

    def start(self):
        if self._started:
            return
        if self._worker is not None:
            if self._worker.is_alive():
                raise VoiceAudioError(
                    "A gravação anterior ainda está encerrando. Tente novamente."
                )
            self._worker = None
        try:
            import sounddevice as sd
        except Exception as exc:
            raise VoiceAudioError(
                "A captura de áudio não está disponível neste aplicativo."
            ) from exc

        self._claim_normalizer_slot()
        self._reset_session()
        try:
            self._native_rate, self._native_channels = _negotiate_input(
                sd, self._device
            )
            self._resampler = StreamingResampler(self._native_rate, SAMPLE_RATE)
            source_blocks = math.ceil(
                self.max_duration_seconds * self._native_rate / BLOCK_SIZE
            )
            self._raw_queue = queue.Queue(
                maxsize=max(self._queue_max, source_blocks)
            )
        except VoiceResamplerError as exc:
            self._record_issue(CaptureIssue.NORMALIZATION, str(exc))
            self._release_normalizer_slot()
            raise VoiceAudioError(str(exc)) from exc
        except Exception as exc:
            self._record_issue(CaptureIssue.NORMALIZATION, str(exc))
            self._release_normalizer_slot()
            raise VoiceAudioError(
                f"Não foi possível preparar o áudio do microfone: {exc}"
            ) from exc

        raw_queue = self._raw_queue
        normalized_queue = self._normalized_queue
        resampler = self._resampler
        native_channels = self._native_channels
        native_rate = self._native_rate
        source_frame_count = self._source_frame_count
        samples = self._samples
        journal = self._journal
        session_issues = self._issues
        expected_stop = threading.Event()
        self._expected_stop = expected_stop

        def callback(indata, frames, time_info, status):
            del time_info
            if status:
                self._record_issue_to(
                    session_issues, CaptureIssue.INPUT_STATUS, str(status)
                )
            try:
                frame_count = max(0, int(frames))
            except (TypeError, ValueError):
                frame_count = _frame_count(indata, native_channels)
            source_frame_count[0] += frame_count
            if source_frame_count[0] / native_rate > self.max_duration_seconds:
                self._record_issue_to(
                    session_issues,
                    CaptureIssue.DURATION_LIMIT,
                    "A captura de áudio excedeu o limite configurado.",
                )
                self._overflow = True
                return
            try:
                # The callback must not retain PortAudio-owned memory.
                chunk = (
                    indata.copy()
                    if hasattr(indata, "copy")
                    else _copy_block(indata)
                )
                raw_queue.put_nowait((chunk, frame_count))
            except queue.Full:
                self._overflow = True
                self._record_issue_to(
                    session_issues,
                    CaptureIssue.RAW_QUEUE,
                    "A fila de captura de áudio ficou cheia.",
                )

        def finished_callback():
            if not expected_stop.is_set():
                self._record_issue_to(
                    session_issues,
                    CaptureIssue.INPUT_STATUS,
                    "O fluxo do microfone terminou inesperadamente.",
                )

        try:
            kwargs = {
                "samplerate": self._native_rate,
                "channels": self._native_channels,
                "dtype": "float32",
                "blocksize": BLOCK_SIZE,
                "callback": callback,
                "finished_callback": finished_callback,
            }
            if self._device is not None:
                kwargs["device"] = self._device
            self._stream = sd.InputStream(**kwargs)
            self._started = True
            self._worker = threading.Thread(
                target=self._normalize_worker,
                args=(
                    raw_queue,
                    normalized_queue,
                    resampler,
                    native_channels,
                    samples,
                    journal,
                    session_issues,
                ),
                name="voice-audio-normalizer",
                daemon=True,
            )
            self._worker.start()
            self._stream.start()
        except Exception as exc:
            self._started = False
            expected_stop.set()
            stream = self._stream
            self._stream = None
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
            self._signal_worker()
            self._join_worker()
            self._release_normalizer_slot()
            raise VoiceAudioError(f"Não foi possível abrir o microfone: {exc}") from exc

    def read_chunk(self, timeout=0.1):
        """Read a normalized 16 kHz mono chunk for optional streaming ASR."""
        return self._normalized_queue.get(timeout=timeout)

    def stop(self):
        """Drain, flush, close, and return canonical samples plus issues."""
        stream = self._stream
        self._stream = None
        was_started = self._started
        self._started = False
        if not was_started and stream is None and self._worker is None:
            return CaptureResult()

        if stream is not None:
            if self._expected_stop is not None:
                self._expected_stop.set()
            try:
                stream.stop()
            except Exception as exc:
                self._record_issue(CaptureIssue.STOP, str(exc))
            finally:
                try:
                    stream.close()
                except Exception as exc:
                    self._record_issue(CaptureIssue.CLOSE, str(exc))

        self._signal_worker()
        self._join_worker()
        if self._worker is not None and self._worker.is_alive():
            self._record_issue(
                CaptureIssue.NORMALIZATION,
                "A normalização do áudio não terminou a tempo.",
            )
        else:
            self._worker = None
        self._release_normalizer_slot()

        with self._issue_lock:
            issues = tuple(self._issues)
        issue = issues[0][0] if issues else None
        message = issues[0][1] if issues else None
        result = CaptureResult(
            samples=self._samples,
            issue=issue,
            issues=issues,
            message=message,
            source_sample_rate=self._native_rate,
            source_channels=self._native_channels,
            duration_seconds=(self._source_frame_count[0] / self._native_rate),
        )
        self._source_frame_count = [0]
        self._overflow = False
        self._samples = []
        self._resampler = None
        self._expected_stop = None
        return result

    def _claim_normalizer_slot(self):
        with self.__class__._normalizer_slot_lock:
            owner = self.__class__._normalizer_slot_owner
            if owner is not None and owner is not self:
                worker = owner._worker
                if worker is None or worker.is_alive():
                    raise VoiceAudioError(
                        "Uma gravação anterior ainda está encerrando. "
                        "Reinicie o aplicativo se o problema continuar."
                    )
            self.__class__._normalizer_slot_owner = self

    def _release_normalizer_slot(self):
        with self.__class__._normalizer_slot_lock:
            if self.__class__._normalizer_slot_owner is not self:
                return
            if self._worker is not None and self._worker.is_alive():
                return
            self.__class__._normalizer_slot_owner = None

    def _reset_session(self):
        self._source_frame_count = [0]
        self._samples = []
        self._issues = []
        self._native_rate = SAMPLE_RATE
        self._native_channels = CHANNELS
        self._resampler = None
        # Swap queues per session. A timed-out worker from an earlier session
        # must never consume or append into the next session's buffers.
        self._raw_queue = queue.Queue(maxsize=self._queue_max)
        self._normalized_queue = queue.Queue(
            maxsize=max(4, self._queue_max * NORMALIZED_QUEUE_RATE_RATIO)
        )
        self._queue = self._normalized_queue

    def _record_issue(self, issue, message=None):
        self._record_issue_to(self._issues, issue, message)

    def _record_issue_to(self, issues, issue, message=None):
        with self._issue_lock:
            item = (issue, str(message) if message else issue.value)
            if item not in issues:
                issues.append(item)

    def _signal_worker(self):
        if self._worker is None:
            return
        try:
            self._raw_queue.put(self._raw_sentinel, timeout=1.0)
        except queue.Full:
            self._record_issue(
                CaptureIssue.RAW_QUEUE,
                "A fila de captura não pôde ser encerrada.",
            )

    def _join_worker(self):
        worker = self._worker
        if worker is not None:
            worker.join(2.0)

    def _normalize_worker(
        self,
        raw_queue,
        normalized_queue,
        resampler,
        native_channels,
        samples,
        journal,
        session_issues,
    ):
        try:
            while True:
                item = raw_queue.get()
                if item is self._raw_sentinel:
                    break
                chunk, _frame_count = item
                try:
                    mono = _to_mono(chunk, native_channels)
                    output = resampler.push(mono)
                    self._publish(
                        output,
                        normalized_queue,
                        samples,
                        journal,
                        session_issues,
                    )
                except Exception as exc:
                    self._record_issue_to(
                        session_issues, CaptureIssue.NORMALIZATION, str(exc)
                    )
            if resampler is not None:
                try:
                    self._publish(
                        resampler.finish(),
                        normalized_queue,
                        samples,
                        journal,
                        session_issues,
                    )
                except Exception as exc:
                    self._record_issue_to(
                        session_issues, CaptureIssue.NORMALIZATION, str(exc)
                    )
        except Exception as exc:
            self._record_issue_to(
                session_issues, CaptureIssue.NORMALIZATION, str(exc)
            )

    def _publish(self, values, normalized_queue, samples, journal, session_issues):
        values = _as_floats(values)
        if not values:
            return
        remaining = self.max_samples - len(samples)
        if remaining <= 0:
            return
        values = values[:remaining]
        samples.extend(values)
        try:
            normalized_queue.put_nowait(values)
        except queue.Full:
            self._record_issue_to(
                session_issues,
                CaptureIssue.NORMALIZED_QUEUE,
                "A fila de áudio normalizado ficou cheia.",
            )
        if journal is not None:
            try:
                journal.write_chunk(values)
            except Exception as exc:
                self._record_issue_to(session_issues, CaptureIssue.JOURNAL, str(exc))


def _negotiate_input(sd, device=None):
    """Choose the device's native rate and a validated mono/stereo layout."""
    info = None
    query = getattr(sd, "query_devices", None)
    if query is not None:
        try:
            info = (
                query(device=device, kind="input")
                if device is not None
                else query(kind="input")
            )
        except Exception:
            try:
                info = query(device) if device is not None else query()
            except Exception:
                info = None
    if isinstance(info, dict):
        rate = info.get("default_samplerate", SAMPLE_RATE)
        channels_available = info.get("max_input_channels", 1)
    else:
        rate = SAMPLE_RATE
        channels_available = 1
    try:
        rate = float(rate)
    except (TypeError, ValueError):
        rate = SAMPLE_RATE
    if not math.isfinite(rate) or rate <= 0:
        rate = SAMPLE_RATE
    try:
        channels_available = max(1, min(2, int(channels_available)))
    except (TypeError, ValueError):
        channels_available = 1

    check = getattr(sd, "check_input_settings", None)
    candidates = [1] + ([2] if channels_available >= 2 else [])
    last_error = None
    for channels in candidates:
        if check is not None:
            try:
                kwargs = {
                    "channels": channels,
                    "samplerate": rate,
                    "dtype": "float32",
                }
                if device is not None:
                    kwargs["device"] = device
                check(**kwargs)
            except Exception as exc:
                last_error = exc
                continue
        return rate, channels
    raise VoiceAudioError(f"O microfone não aceita o formato nativo: {last_error}")


def _copy_block(chunk):
    if isinstance(chunk, (bytes, bytearray)):
        return bytes(chunk)
    if isinstance(chunk, (list, tuple)):
        return list(chunk)
    return chunk


def _frame_count(chunk, channels=1):
    if hasattr(chunk, "shape") and len(chunk.shape) > 0:
        return int(chunk.shape[0])
    values = [] if chunk is None else list(chunk)
    return len(values) // max(1, channels)


def _to_mono(chunk, channels):
    if chunk is None:
        return []
    if hasattr(chunk, "tolist"):
        chunk = chunk.tolist()
    rows = list(chunk)
    if channels <= 1:
        if rows and isinstance(rows[0], (list, tuple)):
            return [float(row[0]) for row in rows if row]
        return _as_floats(rows)
    if not rows:
        return []
    if not isinstance(rows[0], (list, tuple)):
        flat = _as_floats(rows)
        return [
            sum(flat[index : index + channels]) / channels
            for index in range(0, len(flat) - channels + 1, channels)
        ]
    result = []
    for row in rows:
        values = _as_floats(row)
        if values:
            result.append(sum(values[:channels]) / min(channels, len(values)))
    return result


def _as_floats(chunk):
    if chunk is None:
        return []
    if hasattr(chunk, "flatten"):
        chunk = chunk.flatten()
    if isinstance(chunk, (bytes, bytearray)):
        import array

        values = array.array("f")
        usable = len(chunk) - (len(chunk) % SAMPLE_WIDTH)
        values.frombytes(bytes(chunk[:usable]))
        return values.tolist()
    return [float(value) for value in chunk]
