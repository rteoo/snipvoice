"""Voice input state machine: one session from enable through paste.

States: unavailable, loading, idle, recording, transcribing, routing.
Hotkey callbacks only flip guarded state and enqueue work. Capture, download,
inference, Tk, and disk stay off the keyboard threads.
"""

import os
import threading
import time

from clipboard_support import Clipboard
from voice_audio import (
    AudioCapture,
    CaptureIssue,
    CaptureResult,
    VoiceAudioError,
    sounddevice_available,
)
from voice_catalog import PROFILE_STREAMING, catalog_entry
from voice_dispatch import (
    MODE_COMMAND,
    MODE_DICTATION,
    OUTCOME_CANCELLED,
    OUTCOME_EMPTY,
    OUTCOME_FAILED,
    OUTCOME_NO_MATCH,
    OUTCOME_SECURE_INPUT,
    OUTCOME_TARGET_LOST,
    VoiceTarget,
    dispatch_voice_result,
)
from voice_hotkey import VoiceHotkeyMonitor, parse_chord
from voice_history import (
    STATUS_CANCELLED,
    STATUS_FAILED,
    VoiceHistoryStore,
)
from voice_models import (
    VoiceModelError,
    default_voice_cache_dir,
    delete_model,
    download_model,
    installed_model_path,
    model_is_installed,
)
from voice_provider import create_provider
from voice_runtime import VoiceRuntimeError
from voice_settings import resolve_voice_settings, voice_settings_payload
from voice_text_support import VoiceTextReplacements


STATE_UNAVAILABLE = "unavailable"
STATE_LOADING = "loading"
STATE_IDLE = "idle"
STATE_RECORDING = "recording"
STATE_TRANSCRIBING = "transcribing"
STATE_ROUTING = "routing"

_SHUTDOWN_JOIN_SECONDS = 2.0


class _TrackedWorker:
    """Track a task before its runner can execute it."""

    __slots__ = ("done", "thread")

    def __init__(self):
        self.done = threading.Event()
        self.thread = None

_CAPTURE_ISSUE_MESSAGES = {
    CaptureIssue.DURATION_LIMIT: (
        "A gravação de voz atingiu o limite e foi cancelada. "
        "O áudio parcial foi salvo no histórico."
    ),
    CaptureIssue.INPUT_STATUS: (
        "O microfone relatou uma falha durante a gravação. "
        "O áudio parcial foi salvo no histórico."
    ),
    CaptureIssue.RAW_QUEUE: (
        "A captura de áudio não acompanhou o microfone. "
        "O áudio parcial foi salvo no histórico."
    ),
    CaptureIssue.NORMALIZED_QUEUE: (
        "A transcrição em tempo real não acompanhou a gravação. "
        "O áudio foi salvo no histórico."
    ),
    CaptureIssue.NORMALIZATION: (
        "Não foi possível normalizar o áudio do microfone. "
        "O áudio disponível foi salvo no histórico."
    ),
    CaptureIssue.JOURNAL: (
        "Não foi possível salvar toda a gravação durante a captura."
    ),
    CaptureIssue.STOP: "O microfone falhou ao encerrar a gravação.",
    CaptureIssue.CLOSE: "O microfone falhou ao liberar a gravação.",
}

_STATE_LABELS = {
    STATE_UNAVAILABLE: "Entrada por voz (indisponível)",
    STATE_LOADING: "Entrada por voz (carregando…)",
    STATE_IDLE: "Entrada por voz (pronta)",
    STATE_RECORDING: "Entrada por voz (gravando…)",
    STATE_TRANSCRIBING: "Entrada por voz (transcrevendo…)",
    STATE_ROUTING: "Entrada por voz (inserindo…)",
}


class VoiceController:
    """Owns the voice feature for one Snipvoice process."""

    def __init__(
        self,
        settings,
        task_runner,
        insert_text,
        expand_trigger,
        notify,
        logger,
        persist_settings=None,
        gui_submit=None,
        capture_target=None,
        restore_target=None,
        secure_input_blocks=None,
        microphone_status=None,
        on_status_change=None,
        provider=None,
        backend=None,
        capture_factory=None,
        cache_dir=None,
        download=None,
        history_store=None,
        history_dir=None,
        capture_available=None,
    ):
        warnings = []
        self.settings = resolve_voice_settings(settings, warnings)
        self._replacement_config = tuple(self.settings.voice_replacements.items())
        self._text_replacements = VoiceTextReplacements(
            self.settings.voice_replacements
        )
        self.task_runner = task_runner
        self._insert_text = insert_text
        self._expand_trigger = expand_trigger
        self._notify = notify
        self._logger = logger
        self._persist_settings = persist_settings
        self._gui_submit = gui_submit
        self._capture_target = capture_target or (lambda: VoiceTarget("unknown"))
        self._restore_target = restore_target or (lambda target: True)
        self._secure_input_blocks = secure_input_blocks or (lambda: False)
        self._microphone_status = microphone_status
        self._capture_available = capture_available
        self._on_status_change = on_status_change
        self._download_done = 0
        self._download_total = 0
        self._model_download_active = False
        self._model_download_profile = None
        self._model_download_cancel = threading.Event()
        self._capture_factory = capture_factory or AudioCapture
        self._download = download or download_model
        self.cache_dir = cache_dir or self.settings.cache_dir or default_voice_cache_dir()
        self._provider = provider or create_provider(
            self.cache_dir,
            backend=backend,
            download=lambda *args, **kwargs: self._download(*args, **kwargs),
            is_installed=lambda entry, directory: model_is_installed(entry, directory),
            installed_path=lambda entry, directory: installed_model_path(entry, directory),
            delete=lambda entry, directory: delete_model(entry, directory),
        )
        if history_store is None:
            resolved_history_dir = history_dir or os.path.join(self.cache_dir, "history")
            history_store = VoiceHistoryStore(resolved_history_dir)
        self._history = history_store
        self._lock = threading.Lock()
        self._state = STATE_UNAVAILABLE
        self._session_generation = 0
        self._cancel = threading.Event()
        self._shutdown = threading.Event()
        self._active_mode = None
        self._active_target = None
        self._session_form_apply = None
        self._session_form_apply_guarded = None
        self._capture = None
        self._capture_starting = False
        self._startup_generation = None
        self._starting_capture = None
        self._starting_recording = None
        self._history_recording = None
        self._retry_record_id = None
        self._retry_generation = None
        self._form_apply = None
        self._form_apply_guarded = None
        self._form_guard_token = None
        self._partial = ""
        self._monitor = None
        self._load_error = None
        self._workers = []
        self._workers_lock = threading.Lock()
        self._unload_lock = threading.Lock()
        self._unload_pending = False
        self._unload_in_progress = False
        self._clear_disable_after_unload = False
        self._disable_requested = False
        self._meeting_token = None
        self._stream_worker_events = {}
        self.last_outcome = None
        for warning in warnings:
            self._log(warning)

    def _log(self, message):
        logger = self._logger
        if logger is None:
            return
        info = getattr(logger, "info", None)
        if info is not None:
            info(message)

    def _warn(self, message):
        logger = self._logger
        if logger is None:
            return
        warning = getattr(logger, "warning", None) or getattr(logger, "info", None)
        if warning is not None:
            warning(message)

    def _session_valid_locked(self, generation, state):
        return (
            generation == self._session_generation
            and self._state == state
            and not self._cancel.is_set()
            and not self._shutdown.is_set()
        )

    def _session_invalid(self, generation, state):
        with self._lock:
            return not self._session_valid_locked(generation, state)

    def _switch_valid_locked(self, generation):
        return (
            (generation is None or generation == self._session_generation)
            and self.settings.enabled
            and not self._shutdown.is_set()
        )

    def _run_session_side_effect(self, generation, callback, *args, **kwargs):
        """Run one irreversible session callback while its generation owns state."""
        with self._lock:
            if not self._session_valid_locked(generation, STATE_ROUTING):
                return False, None
        return True, callback(*args, **kwargs)

    def _guarded_session_callback(self, generation, callback, *args, **kwargs):
        allowed, result = self._run_session_side_effect(
            generation, callback, *args, **kwargs
        )
        return result if allowed else False

    def _run_history_side_effect(self, generation, callback, *args, **kwargs):
        """Serialize a history write with the generation transition."""
        with self._lock:
            if not self._session_valid_locked(generation, STATE_ROUTING):
                return False, None
            return True, callback(*args, **kwargs)

    def _apply_form_if_current(self, generation, text):
        with self._lock:
            if not self._session_valid_locked(generation, STATE_ROUTING):
                return False
            apply_fn = self._session_form_apply
            if apply_fn is None:
                return False
            guarded_apply_fn = self._session_form_apply_guarded
            token = object() if guarded_apply_fn is not None else None
            if token is not None:
                self._form_guard_token = token
        if guarded_apply_fn is not None:
            guarded_apply_fn(text, token)
            return True
        apply_fn(text)
        return True

    def _retry_history_valid_locked(self, generation, record_id=None):
        return (
            self._session_valid_locked(generation, STATE_TRANSCRIBING)
            and self._retry_generation == generation
            and (record_id is None or self._retry_record_id == record_id)
        )

    def _retry_callback_valid_locked(self, generation):
        return (
            generation == self._retry_generation
            and generation == self._session_generation
            and not self._cancel.is_set()
            and not self._shutdown.is_set()
        )

    def _run_retry_side_effect(self, generation, callback, *args, **kwargs):
        """Run retry metadata/clipboard work only while the retry owns state."""
        with self._lock:
            if not self._retry_callback_valid_locked(generation):
                return False, None
        return True, callback(*args, **kwargs)

    def _run_retry_history_side_effect(self, generation, callback, *args, **kwargs):
        """Serialize retry history writes with cancellation and replacement."""
        with self._lock:
            if not self._retry_history_valid_locked(generation):
                return False, None
            return True, callback(*args, **kwargs)

    def _retry_aborted(self, generation):
        with self._lock:
            return not self._retry_history_valid_locked(generation)

    def _startup_valid_locked(self, generation):
        return (
            self._capture_starting
            and generation == self._session_generation
            and generation == self._startup_generation
            and self._state == STATE_IDLE
            and self.settings.enabled
            and not self._cancel.is_set()
            and not self._shutdown.is_set()
        )

    def _abandon_startup_locked(self, generation):
        if (
            not self._capture_starting
            or generation != self._session_generation
            or generation != self._startup_generation
        ):
            return False
        self._capture_starting = False
        self._startup_generation = None
        self._session_generation += 1
        self._active_mode = None
        self._active_target = None
        self._session_form_apply = None
        self._session_form_apply_guarded = None
        return True

    def _finish_startup_locked(self, generation):
        """Release the startup reservation after its worker has cleaned up."""
        if generation != self._startup_generation:
            return False
        self._capture_starting = False
        self._startup_generation = None
        return True

    def _abort_capture_start(self, generation, capture, recording, error):
        with self._lock:
            current = self._capture_starting and generation == self._session_generation
            if self._starting_capture is capture:
                self._starting_capture = None
            if self._starting_recording is recording:
                self._starting_recording = None
            if current:
                self._abandon_startup_locked(generation)
            else:
                self._finish_startup_locked(generation)
        if capture is not None:
            try:
                capture.stop()
            except Exception:
                pass
        if recording is not None:
            try:
                recording.close_as(
                    STATUS_FAILED if current else STATUS_CANCELLED,
                    error if current else None,
                )
            except Exception as close_exc:
                self._warn(f"Não foi possível encerrar o histórico de voz: {close_exc}")
        return current

    @property
    def state(self):
        with self._lock:
            return self._state

    def status_label(self):
        with self._lock:
            if self._meeting_token is not None:
                return "Entrada por voz (ocupada pela gravação)"
        with self._lock:
            if self._model_download_active:
                if self._download_total:
                    percent = min(100, int(100 * self._download_done / self._download_total))
                    return f"Entrada por voz (baixando {percent}%)"
                return "Entrada por voz (baixando modelo…)"
            if not self.settings.enabled:
                return "Entrada por voz"
            if self._state == STATE_LOADING and self._download_total:
                percent = min(100, int(100 * self._download_done / self._download_total))
                return f"Entrada por voz (baixando {percent}%)"
            return _STATE_LABELS.get(self._state, "Entrada por voz")

    def _emit_status(self):
        callback = self._on_status_change
        if callback is None:
            return
        try:
            callback()
        except Exception:
            pass

    def is_enabled(self):
        return bool(self.settings.enabled)

    @property
    def enabled(self):
        return self.is_enabled()

    @property
    def profile(self):
        return self.settings.profile

    def set_enabled(self, enabled):
        if enabled:
            self.enable()
        else:
            self.disable()

    def model_installed(self):
        return self._provider.profile_installed(self.settings.profile)

    def model_download_in_progress(self, profile=None):
        with self._lock:
            if not self._model_download_active:
                return False
            return profile is None or profile == self._model_download_profile

    def download_profile(self, profile):
        """Download and verify one profile without enabling or loading voice."""
        entry = catalog_entry(profile)
        if entry is None or self._shutdown.is_set():
            return False
        if self._provider.profile_installed(profile):
            self._emit_status()
            return False
        with self._lock:
            if (self._meeting_token is not None or self._model_download_active
                    or self._state == STATE_LOADING):
                return False
            self._model_download_active = True
            self._model_download_profile = profile
            self._download_done = 0
            self._download_total = entry["size_bytes"]
            self._model_download_cancel.clear()
        self._emit_status()
        self._start_worker(
            self._download_profile_worker,
            profile,
            name="voice-model-download",
        )
        return True

    def provider_available(self):
        return self._provider.available()

    def capture_available(self):
        """Return whether the configured capture runtime can be imported."""
        checker = self._capture_available
        if checker is None:
            # Tests and embedders may provide a fake factory without installing
            # the optional sounddevice wheel. The production AudioCapture path
            # is the one that needs the runtime gate.
            checker = sounddevice_available if self._capture_factory is AudioCapture else None
        if checker is None:
            return True
        try:
            return bool(checker())
        except Exception:
            return False

    def history_entries(self):
        return self._history.list_entries()

    def history_entry(self, record_id):
        return self._history.get(record_id)

    def retry_history(self, record_id):
        """Retry saved audio without pasting into a potentially stale target."""
        with self._lock:
            if (
                self._meeting_token is not None
                or self._state != STATE_IDLE
                or self._capture_starting
                or not self.settings.enabled
            ):
                self._notify(
                    "Ative a entrada por voz e aguarde ela ficar pronta para tentar novamente.",
                    key="voice-history",
                )
                return False
            if not self._history.is_retryable(record_id):
                return False
            self._session_generation += 1
            generation = self._session_generation
            self._cancel.clear()
            self._retry_record_id = record_id
            self._retry_generation = generation
            self._form_guard_token = None
            self._state = STATE_TRANSCRIBING
        self._emit_status()
        self._start_worker(
            self._retry_history_worker,
            generation,
            record_id,
            name="voice-history-retry",
        )
        return True

    def copy_history_transcript(self, record_id):
        entry = self._history.get(record_id)
        transcript = entry.get("transcript", "") if entry else ""
        if not transcript:
            return False
        return self._leave_on_clipboard(transcript)

    def set_language(self, language):
        self.apply_options(language=language)

    def apply_options(
        self,
        profile=None,
        language=None,
        hotkey=None,
        command_hotkey=None,
    ):
        """Persist voice options and apply only the runtime changes required."""
        warnings = []
        payload = voice_settings_payload(self.settings)
        if profile is not None:
            payload["voice_profile"] = profile
        if language is not None:
            payload["voice_language"] = language
        if hotkey is not None:
            payload["voice_hotkey"] = hotkey
        if command_hotkey is not None:
            payload["voice_command_hotkey"] = command_hotkey
        candidate = resolve_voice_settings(payload, warnings)
        for warning in warnings:
            self._log(warning)
        with self._lock:
            if self._disable_requested or self._meeting_token is not None:
                return
            previous = self.settings
            runtime_same = (
                candidate.profile == previous.profile
                and candidate.language == previous.language
            )
            hotkeys_same = (
                candidate.hotkey == previous.hotkey
                and candidate.command_hotkey == previous.command_hotkey
            )
            self.settings = candidate
        self._persist()
        if runtime_same and hotkeys_same:
            if candidate.enabled and self.state == STATE_UNAVAILABLE:
                self.enable()
            return
        if not candidate.enabled:
            return
        with self._lock:
            active = self._state in (
                STATE_RECORDING,
                STATE_TRANSCRIBING,
                STATE_ROUTING,
            ) or self._capture_starting or any(
                value is not None
                for value in (
                    self._capture,
                    self._starting_capture,
                    self._starting_recording,
                    self._history_recording,
                    self._retry_record_id,
                )
            )
        if runtime_same:
            if active:
                self._cancel_session_locked(reason="hotkey")
            with self._lock:
                state = self._state
            if state == STATE_IDLE:
                self._start_monitor()
            elif state == STATE_UNAVAILABLE:
                self.enable()
            # A load already in progress starts the monitor with the latest
            # settings when it reaches idle.
            return
        if active:
            # Stop the microphone before the switch worker unloads the backend.
            # The abort also bumps the session generation and keeps LOADING so
            # a cancelled finish worker cannot reopen IDLE mid-unload.
            self._cancel_session_locked(reason="switch")
        else:
            with self._lock:
                if not candidate.enabled:
                    return
                self._session_generation += 1
                self._form_guard_token = None
                self._state = STATE_LOADING
            self._emit_status()
        with self._lock:
            switch_generation = self._session_generation
        with self._workers_lock:
            antecedent_workers = tuple(self._workers)
        self._start_worker(
            self._switch_profile_worker,
            previous,
            antecedent_workers,
            switch_generation,
            name="voice-switch",
        )

    def partial_text(self):
        with self._lock:
            return self._partial

    def status_snapshot(self):
        """Return one consistent status value for UI callbacks."""
        with self._lock:
            return {
                "state": self._state,
                "mode": self._active_mode,
                "partial": self._partial,
            }

    def register_form_target(self, apply_fn, guarded_apply_fn=None):
        with self._lock:
            self._form_apply = apply_fn
            self._form_apply_guarded = guarded_apply_fn

    def form_guard_valid(self, token):
        """Return whether a queued form update still belongs to its session."""
        with self._lock:
            return token is self._form_guard_token and not self._shutdown.is_set()

    def unregister_form_target(self):
        with self._lock:
            closed = self._form_apply
            self._form_apply = None
            self._form_apply_guarded = None
            self._form_guard_token = None
            if self._session_form_apply is closed:
                self._session_form_apply = None
                self._session_form_apply_guarded = None

    def enable(self):
        if self._shutdown.is_set():
            return
        with self._unload_lock:
            unload_busy = self._unload_pending or self._unload_in_progress
        if unload_busy:
            self._notify(
                "A entrada por voz ainda está encerrando; tente ativá-la novamente em instantes.",
                key="voice-load",
            )
            return
        with self._lock:
            if self._disable_requested or self._meeting_token is not None:
                return
            if self._model_download_active:
                blocked_by_download = True
            else:
                blocked_by_download = False
                self.settings.enabled = True
                if self._state in (STATE_LOADING, STATE_IDLE):
                    return
                if self._state in (STATE_RECORDING, STATE_TRANSCRIBING, STATE_ROUTING):
                    return
                # disable()/cancel() leave this set; a later enable must start clean.
                self._cancel.clear()
                self._state = STATE_LOADING
                self._load_error = None
                self._download_done = 0
                self._download_total = 0
        if blocked_by_download:
            self._notify(
                "Aguarde o download do modelo terminar antes de ativar a voz.",
                key="voice-model-download",
            )
            return
        self._persist()
        self._emit_status()
        self._start_worker(self._load_worker, name="voice-load")

    def disable(self):
        # Close new-session admission before cancellation can wait on a worker.
        # Keep active pointers intact until _cancel_session_locked captures and
        # cleans them.
        with self._lock:
            self._disable_requested = True
            self.settings.enabled = False
            self._state = STATE_UNAVAILABLE
        self._cancel_session_locked(reason="disable")
        joined = self._join_workers(_SHUTDOWN_JOIN_SECONDS)
        with self._lock:
            self.settings.enabled = False
            self._state = STATE_UNAVAILABLE
        self._stop_monitor()
        if joined:
            unloaded = self._unload_provider()
            if unloaded:
                with self._lock:
                    self._disable_requested = False
            else:
                self._schedule_unload_after_workers(clear_disable=True)
        else:
            self._schedule_unload_after_workers(clear_disable=True)
        self._persist()
        self._emit_status()

    def shutdown(self, timeout=_SHUTDOWN_JOIN_SECONDS):
        self._shutdown.set()
        self._model_download_cancel.set()
        self._cancel_session_locked(reason="shutdown")
        self._stop_monitor()
        with self._lock:
            self._state = STATE_UNAVAILABLE
            self.settings.enabled = False
        self._emit_status()
        # ceiling: 2 s before detaching a stuck native run; runtime unload is
        # deferred until that worker exits so native resources stay valid.
        joined = self._join_workers(timeout)
        if not joined:
            self._warn(
                "Encerramento da voz atingiu o tempo limite; o runtime será "
                "descarregado quando os workers terminarem."
            )
            self._schedule_unload_after_workers()
            return
        self._unload_provider()

    def set_profile(self, profile):
        self.apply_options(profile=profile)

    def handle_hotkey_press(self, mode):
        if self._shutdown.is_set():
            return False
        with self._lock:
            if (
                self._meeting_token is not None
                or self._disable_requested
                or not self.settings.enabled
                or self._state != STATE_IDLE
                or self._capture_starting
            ):
                return False
            if mode not in (MODE_DICTATION, MODE_COMMAND):
                mode = MODE_DICTATION
            form_apply = self._form_apply
            form_apply_guarded = self._form_apply_guarded
            self._form_guard_token = None
            self._session_generation += 1
            generation = self._session_generation
            self._cancel.clear()
            self._capture_starting = True
            self._startup_generation = generation
        if self._microphone_status is not None:
            try:
                status = self._microphone_status()
            except Exception as exc:
                with self._lock:
                    current = self._capture_starting and generation == self._session_generation
                    if current:
                        self._abandon_startup_locked(generation)
                    else:
                        self._finish_startup_locked(generation)
                if current:
                    self._warn(f"Não foi possível verificar o microfone: {exc}")
                return False
            if status == "denied":
                with self._lock:
                    current = self._capture_starting and generation == self._session_generation
                    if current:
                        self._abandon_startup_locked(generation)
                    else:
                        self._finish_startup_locked(generation)
                if current:
                    self._notify(
                        "O macOS bloqueou o microfone. Conceda a permissão e reinicie o app.",
                        key="voice-mic",
                    )
                return False
        try:
            target = self._capture_target()
        except Exception as exc:
            with self._lock:
                current = self._capture_starting and generation == self._session_generation
                if current:
                    self._abandon_startup_locked(generation)
                else:
                    self._finish_startup_locked(generation)
            if current:
                self._warn(f"Não foi possível capturar o destino da voz: {exc}")
                self._notify("Não foi possível preparar o destino da voz.", key="voice-target")
            return False
        session_form = None
        if form_apply is not None and mode != MODE_COMMAND:
            target = VoiceTarget("form", handle=form_apply)
            session_form = form_apply
        with self._lock:
            if not self._startup_valid_locked(generation):
                self._finish_startup_locked(generation)
                return False
        try:
            recording = self._history.begin(
                mode=mode,
                provider=self._provider.provider_id,
                profile=self.settings.profile,
                language=self.settings.language,
                target_kind=getattr(target, "kind", "unknown"),
            )
        except Exception as exc:
            with self._lock:
                current = self._capture_starting and generation == self._session_generation
                if current:
                    self._abandon_startup_locked(generation)
                else:
                    self._finish_startup_locked(generation)
            if current:
                self._warn(f"Não foi possível preparar o histórico de voz: {exc}")
                self._notify(
                    "Não foi possível iniciar uma gravação recuperável.",
                    key="voice-history",
                )
            return False
        with self._lock:
            current = self._startup_valid_locked(generation)
            if current:
                self._starting_recording = recording
        if not current:
            recording.close_as(STATUS_CANCELLED)
            with self._lock:
                self._finish_startup_locked(generation)
            return False
        capture = None
        try:
            capture = self._capture_factory()
            with self._lock:
                current = self._startup_valid_locked(generation)
                if current:
                    self._starting_capture = capture
            if not current:
                capture.stop()
                recording.close_as(STATUS_CANCELLED)
                with self._lock:
                    self._finish_startup_locked(generation)
                return False
            set_journal = getattr(capture, "set_journal", None)
            if set_journal is not None:
                set_journal(recording)
            capture.start()
        except VoiceAudioError as exc:
            current = self._abort_capture_start(generation, capture, recording, exc)
            if current:
                self._notify(str(exc), key="voice-audio")
            return False
        except Exception as exc:
            current = self._abort_capture_start(generation, capture, recording, exc)
            if current:
                self._notify(f"Não foi possível gravar: {exc}", key="voice-audio")
            return False
        with self._lock:
            startup_aborted = not self._startup_valid_locked(generation)
            if startup_aborted:
                self._finish_startup_locked(generation)
                if self._starting_capture is capture:
                    self._starting_capture = None
                if self._starting_recording is recording:
                    self._starting_recording = None
            else:
                self._capture_starting = False
                self._startup_generation = None
                self._starting_capture = None
                self._starting_recording = None
                self._active_mode = mode
                self._active_target = target
                self._session_form_apply = session_form
                self._session_form_apply_guarded = form_apply_guarded
                self._capture = capture
                self._history_recording = recording
                self._partial = ""
                self._state = STATE_RECORDING
            stream_done = None
            if self._state == STATE_RECORDING and self.settings.profile == PROFILE_STREAMING:
                stream_done = threading.Event()
                self._stream_worker_events[generation] = stream_done
        if startup_aborted:
            if capture is not None:
                try:
                    capture.stop()
                except Exception:
                    pass
            if recording is not None:
                try:
                    recording.close_as(STATUS_CANCELLED)
                except Exception as exc:
                    self._warn(
                        f"Não foi possível encerrar o histórico de voz: {exc}"
                    )
            return False
        self._emit_status()
        if stream_done is not None:
            self._start_worker(
                self._stream_worker,
                generation,
                stream_done,
                name="voice-stream",
            )
        return True

    def handle_hotkey_release(self, mode):
        if self._shutdown.is_set():
            return False
        with self._lock:
            if self._disable_requested or not self.settings.enabled:
                return False
            starting = self._capture_starting
            if not starting and self._state != STATE_RECORDING:
                return False
            if starting:
                generation = None
            else:
                generation = self._session_generation
                capture = self._capture
                self._capture = None
                recording = self._history_recording
                self._state = STATE_TRANSCRIBING
        if starting:
            self._cancel_session_locked(reason="cancel")
            return True
        self._emit_status()
        self._start_worker(
            self._finish_worker,
            generation,
            capture,
            recording,
            name="voice-finish",
        )
        return True

    def cancel(self):
        with self._lock:
            if self._disable_requested or self._shutdown.is_set():
                return
        self._cancel_session_locked(reason="cancel")

    def _cancel_session_locked(self, reason):
        self._cancel.set()
        try:
            self._provider.cancel()
        except Exception:
            pass
        capture = None
        recording = None
        starting_recording = None
        retry_record_id = None
        with self._lock:
            self._form_guard_token = None
            active = self._state in (
                STATE_RECORDING,
                STATE_TRANSCRIBING,
                STATE_ROUTING,
            ) or self._capture_starting or any(
                value is not None
                for value in (
                    self._capture,
                    self._starting_capture,
                    self._starting_recording,
                    self._history_recording,
                    self._retry_record_id,
                )
            )
            if active:
                generation = self._session_generation
                capture = self._capture
                self._capture = None
                if not self._capture_starting:
                    self._starting_capture = None
                recording = self._history_recording
                self._history_recording = None
                if self._capture_starting:
                    starting_recording = self._starting_recording
                else:
                    self._starting_recording = None
                retry_record_id = self._retry_record_id
                self._retry_record_id = None
                self._retry_generation = None
                self._active_mode = None
                self._active_target = None
                self._session_form_apply = None
                self._session_form_apply_guarded = None
                self._partial = ""
                self.last_outcome = OUTCOME_CANCELLED
                self._session_generation += 1
                if (
                    reason == "switch"
                    and self.settings.enabled
                    and not self._disable_requested
                ):
                    self._state = STATE_LOADING
                elif (
                    reason == "shutdown"
                    or self._disable_requested
                    or not self.settings.enabled
                ):
                    self._state = STATE_UNAVAILABLE
                else:
                    self._state = STATE_IDLE
                self._stream_worker_events.pop(generation, None)
                if retry_record_id is not None:
                    try:
                        self._history.cancel(retry_record_id)
                    except Exception as exc:
                        self._warn(f"Não foi possível encerrar o retry de voz: {exc}")
        if capture is not None:
            try:
                capture.stop()
            except Exception:
                pass
        if recording is not None:
            try:
                recording.close_as(STATUS_CANCELLED)
            except Exception as exc:
                self._warn(f"Não foi possível encerrar o histórico de voz: {exc}")
        if starting_recording is not None and starting_recording is not recording:
            try:
                starting_recording.close_as(STATUS_CANCELLED)
            except Exception as exc:
                self._warn(f"Não foi possível encerrar o histórico de voz: {exc}")
        self._emit_status()

    def _load_worker(self):
        if self._shutdown.is_set():
            return
        try:
            if not self.capture_available():
                raise VoiceRuntimeError(
                    "A captura de áudio não está disponível neste aplicativo."
                )
            self._prepare_provider()
            if self._cancel.is_set() or self._shutdown.is_set():
                return
        except (VoiceModelError, VoiceRuntimeError) as exc:
            with self._lock:
                self._state = STATE_UNAVAILABLE
                self._load_error = str(exc)
            self._notify(str(exc), key="voice-load")
            self._emit_status()
            return
        except Exception as exc:
            with self._lock:
                self._state = STATE_UNAVAILABLE
                self._load_error = str(exc)
            self._warn(f"Falha ao ativar a entrada por voz: {exc}")
            self._notify(f"Falha ao ativar a entrada por voz: {exc}", key="voice-load")
            self._emit_status()
            return
        with self._lock:
            if not self.settings.enabled or self._shutdown.is_set():
                self._state = STATE_UNAVAILABLE
                start_monitor = False
            else:
                self._state = STATE_IDLE
                self._load_error = None
                self._download_done = 0
                self._download_total = 0
                start_monitor = True
        if start_monitor:
            self._start_monitor()
        self._emit_status()

    def _switch_profile_worker(
        self,
        previous,
        antecedent_workers=(),
        generation=None,
    ):
        with self._lock:
            if not self._switch_valid_locked(generation):
                return
        if self._shutdown.is_set():
            return
        # A cancelled finish/stream worker may still be inside native inference.
        if not self._join_workers(
            _SHUTDOWN_JOIN_SECONDS,
            workers=antecedent_workers,
        ):
            self._wait_for_workers(antecedent_workers)
        with self._lock:
            switch_valid = self._switch_valid_locked(generation)
        if not switch_valid:
            return
        with self._unload_lock:
            unload_busy = self._unload_pending or self._unload_in_progress
        if unload_busy:
            with self._lock:
                if self._switch_valid_locked(generation):
                    self._state = STATE_UNAVAILABLE
            self._emit_status()
            return
        # The aborted session set this; a new download/load must not inherit it.
        self._cancel.clear()
        try:
            if not self._unload_provider():
                with self._lock:
                    if self._switch_valid_locked(generation):
                        self._state = STATE_UNAVAILABLE
                self._emit_status()
                return
            with self._lock:
                switch_valid = self._switch_valid_locked(generation)
            if not switch_valid:
                return
            self._prepare_provider()
            with self._lock:
                switch_valid = self._switch_valid_locked(generation)
            if not switch_valid or self._cancel.is_set():
                return
        except Exception as exc:
            self._warn(f"Falha ao trocar o perfil de voz; mantendo o anterior: {exc}")
            rollback_payload = None
            with self._lock:
                switch_valid = self._switch_valid_locked(generation)
                if switch_valid:
                    self.settings = previous
                    rollback_payload = voice_settings_payload(self.settings)
                    unavailable = False
                else:
                    unavailable = not self.settings.enabled or self._shutdown.is_set()
                    if unavailable:
                        self._state = STATE_UNAVAILABLE
            if not switch_valid:
                if unavailable:
                    self._emit_status()
                return
            self._persist_payload(rollback_payload)
            try:
                if not self._provider.profile_installed(previous.profile):
                    raise VoiceRuntimeError("O modelo anterior não está mais instalado.")
                self._provider.prepare(previous.profile, previous.language)
                with self._lock:
                    if not self._switch_valid_locked(generation):
                        if not self.settings.enabled or self._shutdown.is_set():
                            self._state = STATE_UNAVAILABLE
                            emit_status = True
                        else:
                            emit_status = False
                    else:
                        emit_status = False
                        self._state = STATE_IDLE
                if emit_status:
                    self._emit_status()
                    return
                self._emit_status()
                return
            except Exception:
                pass
            with self._lock:
                if not self._switch_valid_locked(generation):
                    if not self.settings.enabled or self._shutdown.is_set():
                        self._state = STATE_UNAVAILABLE
                        emit_status = True
                    else:
                        emit_status = False
                else:
                    self._state = STATE_UNAVAILABLE
                    emit_status = True
            if not emit_status:
                return
            self._emit_status()
            self._notify(str(exc), key="voice-switch")
            return
        with self._lock:
            if not self._switch_valid_locked(generation):
                return
            self._state = STATE_IDLE
        self._start_monitor()
        self._emit_status()

    def _prepare_provider(self):
        def progress(done, total):
            with self._lock:
                self._download_done = done
                self._download_total = total or 0
            self._emit_status()

        self._provider.prepare(
            self.settings.profile,
            self.settings.language,
            progress=progress,
            cancel_event=self._cancel,
        )

    def _download_profile_worker(self, profile):
        def progress(done, total):
            with self._lock:
                self._download_done = done
                self._download_total = total or self._download_total
            self._emit_status()

        try:
            self._provider.download_profile(
                profile,
                progress=progress,
                cancel_event=self._model_download_cancel,
            )
            if not self._model_download_cancel.is_set() and not self._shutdown.is_set():
                self._notify(
                    "Modelo de voz baixado e verificado.",
                    key="voice-model-download",
                )
        except (VoiceModelError, VoiceRuntimeError) as exc:
            if not self._model_download_cancel.is_set():
                self._notify(str(exc), key="voice-model-download")
        except Exception as exc:
            if not self._model_download_cancel.is_set():
                self._warn(f"Falha ao baixar o modelo de voz: {exc}")
                self._notify(
                    f"Falha ao baixar o modelo de voz: {exc}",
                    key="voice-model-download",
                )
        finally:
            with self._lock:
                self._model_download_active = False
                self._model_download_profile = None
                self._download_done = 0
                self._download_total = 0
            self._emit_status()

    def _finish_worker(self, generation, capture, recording):
        capture_result = CaptureResult()
        if capture is not None:
            try:
                stopped = capture.stop()
                if isinstance(stopped, CaptureResult):
                    capture_result = stopped
                else:
                    pcm, failed = stopped
                    capture_result = CaptureResult(
                        pcm,
                        issue=(CaptureIssue.DURATION_LIMIT if failed else None),
                    )
            except Exception as exc:
                if self._shutdown.is_set():
                    return
                self._recording_failed_if_current(generation, recording, exc)
                self._fail_to_idle(f"Falha ao encerrar a gravação: {exc}", generation)
                return
        if self._shutdown.is_set():
            return
        pcm = capture_result.samples
        capture_metadata = {
            "audio_format": "f32le",
            "sample_rate_hz": 16000,
            "channels": 1,
            "source_sample_rate_hz": capture_result.source_sample_rate,
            "source_channels": capture_result.source_channels,
            "capture_duration_seconds": capture_result.duration_seconds,
        }
        if capture_result.issue is not None:
            capture_metadata["capture_issue"] = capture_result.issue.value
            capture_metadata["capture_issue_message"] = capture_result.message
            capture_metadata["capture_issues"] = [
                {"issue": issue.value, "message": message}
                for issue, message in capture_result.issues
            ]
        try:
            recording.finish_capture(pcm, capture_metadata)
        except Exception as exc:
            self._recording_failed_if_current(generation, recording, exc)
            self._fail_to_idle(
                f"Falha ao salvar a gravação recuperável: {exc}", generation
            )
            return
        if capture_result.issue is not None:
            error = capture_result.message or capture_result.issue.value
            self._recording_failed_if_current(generation, recording, error)
            self._fail_to_idle(
                _CAPTURE_ISSUE_MESSAGES.get(
                    capture_result.issue,
                    "A captura de áudio falhou. O áudio parcial foi salvo no histórico.",
                ),
                generation,
            )
            return
        if self._cancel.is_set() or self._shutdown.is_set():
            self._cancel_recording_if_current(generation, recording)
            return
        try:
            inference_started = time.monotonic()
            if (
                self.settings.profile == PROFILE_STREAMING
                and self._provider.supports_stream()
            ):
                if capture is not None:
                    with self._lock:
                        stream_done = self._stream_worker_events.get(generation)
                    if stream_done is None or not stream_done.wait(0.25):
                        raise VoiceRuntimeError(
                            "A transcrição contínua não encerrou a tempo."
                        )
                    if not self._drain_stream_chunks(capture, generation):
                        return
                with self._lock:
                    if (
                        generation != self._session_generation
                        or self._state != STATE_TRANSCRIBING
                        or self._cancel.is_set()
                        or self._shutdown.is_set()
                    ):
                        return
                raw_transcript = self._provider.finalize_stream()
            else:
                raw_transcript = self._provider.transcribe(
                    pcm, cancel_event=self._cancel
                )
            inference_duration = max(0.0, time.monotonic() - inference_started)
        except VoiceRuntimeError as exc:
            self._recording_failed_if_current(generation, recording, exc)
            self._fail_to_idle(str(exc), generation)
            return
        except Exception as exc:
            self._recording_failed_if_current(generation, recording, exc)
            self._fail_to_idle(f"Falha na transcrição: {exc}", generation)
            return
        if self._cancel.is_set() or self._shutdown.is_set():
            self._cancel_recording_if_current(generation, recording)
            return
        with self._lock:
            if generation != self._session_generation:
                return
            if self._state != STATE_TRANSCRIBING:
                return
            self._state = STATE_ROUTING
            mode = self._active_mode or MODE_DICTATION
            target = self._active_target
            form_apply = self._session_form_apply
        transcript = self._apply_text_replacements(raw_transcript, mode)
        marked, _ = self._run_history_side_effect(
            generation,
            self._history.mark_transcribed,
            recording.record_id,
            transcript,
            raw_transcript=raw_transcript,
            inference_duration_seconds=inference_duration,
        )
        if not marked:
            return
        self._emit_status()
        if self._shutdown.is_set():
            return
        if getattr(target, "kind", None) == "form" and form_apply is None:
            self._cancel_recording_if_current(generation, recording)
            return
        apply_form = (
            (lambda text: self._apply_form_if_current(generation, text))
            if form_apply is not None and mode != MODE_COMMAND
            else None
        )
        try:
            outcome = dispatch_voice_result(
                transcript,
                mode,
                target,
                snippets=self._snippets(),
                trigger_index=self._trigger_index(),
                insert_text=lambda text: self._guarded_session_callback(
                    generation, self._insert_text, text
                ),
                expand_trigger=lambda trigger: self._guarded_session_callback(
                    generation, self._expand_trigger, trigger
                ),
                apply_form=apply_form,
                restore_target=self._restore_target,
                secure_input_blocks=self._secure_input_blocks,
                leave_on_clipboard=lambda text: self._guarded_session_callback(
                    generation, self._leave_on_clipboard, text
                ),
                cancelled=self._cancel.is_set(),
                is_cancelled=lambda: self._session_invalid(
                    generation, STATE_ROUTING
                ),
            )
        except Exception as exc:
            self._recording_failed_if_current(generation, recording, exc)
            self._fail_to_idle(
                f"Falha ao processar o texto de voz: {exc}", generation
            )
            return
        self._finish_outcome(outcome, generation, recording, transcript)

    def _stream_worker(self, generation, done_event):
        try:
            if not self._provider.supports_stream():
                return
            with self._lock:
                if (
                    self._state != STATE_RECORDING
                    or generation != self._session_generation
                    or self._cancel.is_set()
                    or self._shutdown.is_set()
                ):
                    return
            try:
                self._provider.start_stream()
            except VoiceRuntimeError:
                return
            if self._session_invalid(generation, STATE_RECORDING):
                return
            # Display-only partials. The release path finalizes.
            while not self._shutdown.is_set() and not self._cancel.is_set():
                with self._lock:
                    if (
                        self._state != STATE_RECORDING
                        or generation != self._session_generation
                    ):
                        return
                    capture = self._capture
                if capture is None:
                    return
                try:
                    chunk = capture.read_chunk(timeout=0.1)
                except Exception:
                    continue
                with self._lock:
                    if (
                        self._state != STATE_RECORDING
                        or generation != self._session_generation
                        or capture is not self._capture
                        or self._cancel.is_set()
                        or self._shutdown.is_set()
                    ):
                        return
                try:
                    partial = self._provider.feed(_flatten(chunk))
                except Exception:
                    return
                with self._lock:
                    if self._cancel.is_set() or self._shutdown.is_set():
                        return
                    if (
                        self._state != STATE_RECORDING
                        or generation != self._session_generation
                        or capture is not self._capture
                    ):
                        return
                    self._partial = partial or ""
        finally:
            done_event.set()

    def _drain_stream_chunks(self, capture, generation=None):
        """Feed normalized chunks emitted while capture was stopping."""
        while True:
            try:
                chunk = capture.read_chunk(timeout=0)
            except Exception:
                return True
            if generation is None:
                self._provider.feed(_flatten(chunk))
                continue
            with self._lock:
                if (
                    generation != self._session_generation
                    or self._state != STATE_TRANSCRIBING
                    or self._cancel.is_set()
                    or self._shutdown.is_set()
                ):
                    return False
            self._provider.feed(_flatten(chunk))

    def _snippets(self):
        getter = getattr(self, "get_snippets", None)
        if getter is not None:
            return getter()
        return {}

    def _trigger_index(self):
        getter = getattr(self, "get_trigger_index", None)
        if getter is not None:
            return getter()
        return None

    def bind_library(self, get_snippets, get_trigger_index):
        self.get_snippets = get_snippets
        self.get_trigger_index = get_trigger_index

    def _invoke_session_form(self, text):
        """Apply only if the form captured at press is still the session target."""
        with self._lock:
            apply_fn = self._session_form_apply
        if apply_fn is None:
            return
        apply_fn(text)

    def _leave_on_clipboard(self, text):
        try:
            saved = bool(Clipboard.set_content(text))
        except Exception as exc:
            self._warn(
                "Falha ao copiar a transcrição de voz para a área de "
                f"transferência: {exc}"
            )
            return False
        if not saved:
            self._warn(
                "Falha ao copiar a transcrição de voz para a área de transferência."
            )
        return saved

    def _complete_session_locked(self, generation, outcome):
        if generation != self._session_generation:
            return False
        if self._state not in (
            STATE_RECORDING,
            STATE_TRANSCRIBING,
            STATE_ROUTING,
        ):
            return False
        self.last_outcome = outcome
        self._active_mode = None
        self._active_target = None
        self._session_form_apply = None
        self._session_form_apply_guarded = None
        self._history_recording = None
        self._partial = ""
        self._stream_worker_events.pop(generation, None)
        self._state = STATE_IDLE if self.settings.enabled else STATE_UNAVAILABLE
        return True

    def _complete_session(self, generation, outcome):
        """Return True when this session still owns the controller state."""
        with self._lock:
            completed = self._complete_session_locked(generation, outcome)
        if completed:
            self._emit_status()
        return completed

    def _cancel_recording_if_current(self, generation, recording):
        """Commit cancellation and its journal write as one state transition."""
        with self._lock:
            if not self._complete_session_locked(generation, OUTCOME_CANCELLED):
                return False
            try:
                self._history.cancel(recording.record_id)
            except Exception as exc:
                self._warn(f"Não foi possível encerrar o histórico de voz: {exc}")
        self._emit_status()
        return True

    def _finish_outcome(self, result, generation, recording, transcript):
        outcome = result.outcome
        # State completion and its journal write share the controller lock. The
        # state transition is still performed first: a crash before the journal
        # write leaves an already-transcribed record retryable, while a later
        # cancel cannot claim a session that has finished routing.
        with self._lock:
            if not self._complete_session_locked(generation, outcome):
                return
            try:
                if outcome == OUTCOME_CANCELLED:
                    self._history.cancel(recording.record_id)
                else:
                    self._history.complete(recording.record_id, transcript, outcome)
            except Exception as exc:
                self._warn(f"Não foi possível atualizar o histórico de voz: {exc}")
        self._emit_status()
        if outcome == OUTCOME_CANCELLED:
            return
        if outcome == OUTCOME_NO_MATCH:
            self._notify("Nenhum atalho corresponde ao que foi falado.", key="voice-nomatch")
        elif outcome == OUTCOME_SECURE_INPUT:
            if result.clipboard_saved:
                message = (
                    "Entrada segura do macOS ativa. "
                    "O texto ficou na área de transferência."
                )
            else:
                message = (
                    "Entrada segura do macOS ativa. Não foi possível copiar o texto "
                    "para a área de transferência."
                )
            self._notify(message, key="voice-secure")
        elif outcome == OUTCOME_TARGET_LOST:
            if result.clipboard_saved:
                message = (
                    "O aplicativo de destino não está mais na frente. "
                    "O texto ficou na área de transferência."
                )
            else:
                message = (
                    "O aplicativo de destino não está mais na frente e não foi "
                    "possível copiar o texto para a área de transferência."
                )
            self._notify(message, key="voice-target")
        elif outcome == OUTCOME_EMPTY:
            self._notify("Nenhuma fala foi reconhecida.", key="voice-empty")
        elif outcome == OUTCOME_FAILED:
            if result.clipboard_saved is True:
                message = (
                    "Não foi possível inserir o texto de voz automaticamente. "
                    "Ele está na área de transferência."
                )
            elif result.clipboard_saved is False:
                message = (
                    "Não foi possível inserir o texto de voz nem copiá-lo "
                    "para a área de transferência."
                )
            else:
                message = "Não foi possível inserir o texto de voz."
            self._notify(message, key="voice-insert")

    def _recording_failed_if_current(self, generation, recording, error):
        """Record a failure only while its session still owns the journal."""
        with self._lock:
            if (
                generation != self._session_generation
                or self._state not in (STATE_TRANSCRIBING, STATE_ROUTING)
                or self._cancel.is_set()
                or self._shutdown.is_set()
            ):
                return False
            self._recording_failed(recording, error)
            return True

    def _recording_failed(self, recording, error):
        try:
            close_as = getattr(recording, "close_as", None)
            if close_as is not None:
                close_as(STATUS_FAILED, error)
            else:
                self._history.fail(recording.record_id, error)
        except Exception as exc:
            self._warn(f"Não foi possível atualizar o histórico de voz: {exc}")

    def _apply_text_replacements(self, transcript, mode):
        if mode == MODE_COMMAND:
            return transcript
        replacements = dict(self.settings.voice_replacements or {})
        config = tuple(replacements.items())
        if config != self._replacement_config:
            self._replacement_config = config
            self._text_replacements = VoiceTextReplacements(replacements)
        try:
            return self._text_replacements.apply(transcript)
        except Exception as exc:
            self._warn(f"Não foi possível aplicar as correções de voz: {exc}")
            return transcript

    def _commit_retry_history(self, generation, record_id, transcript):
        """Durably finish a retry before any clipboard or notification callback."""
        with self._lock:
            if not self._retry_history_valid_locked(generation, record_id):
                return False
            try:
                completed = self._history.complete(record_id, transcript, "recovered")
            except Exception as exc:
                self._warn(f"Não foi possível atualizar o histórico de voz: {exc}")
                return False
            if not completed:
                return False
            self._retry_record_id = None
            self._state = STATE_IDLE if self.settings.enabled else STATE_UNAVAILABLE
        self._emit_status()
        return True

    def _retry_history_worker(self, generation, record_id):
        try:
            if self._retry_aborted(generation):
                return
            entry = self._history.get(record_id) or {}
            pcm = self._history.load_samples(record_id)
            if not pcm:
                raise ValueError("A gravação salva está vazia.")
            updated, _ = self._run_retry_history_side_effect(
                generation,
                self._history.update,
                record_id,
                status="pending",
                retry_provider=self._provider.provider_id,
                retry_profile=self.settings.profile,
                retry_language=self.settings.language,
            )
            if not updated:
                return
            inference_started = time.monotonic()
            raw_transcript = self._provider.transcribe(
                pcm, cancel_event=self._cancel
            )
            inference_duration = max(0.0, time.monotonic() - inference_started)
            if not str(raw_transcript or "").strip():
                raise ValueError("Nenhuma fala foi reconhecida na gravação.")
            if self._retry_aborted(generation):
                return
            transcript = self._apply_text_replacements(
                raw_transcript,
                entry.get("mode", MODE_DICTATION),
            )
            marked, _ = self._run_retry_history_side_effect(
                generation,
                self._history.mark_transcribed,
                record_id,
                transcript,
                raw_transcript=raw_transcript,
                inference_duration_seconds=inference_duration,
            )
            if not marked:
                return
            if not self._commit_retry_history(generation, record_id, transcript):
                return
            copied_allowed, copied = self._run_retry_side_effect(
                generation,
                self._leave_on_clipboard,
                transcript,
            )
            if not copied_allowed:
                return
            if copied:
                message = (
                    "A gravação foi recuperada. O texto está na área de transferência."
                )
            else:
                message = (
                    "A gravação foi recuperada no histórico, mas não foi possível "
                    "copiar o texto."
                )
            self._run_retry_side_effect(
                generation,
                self._notify,
                message,
                key="voice-history",
            )
        except Exception as exc:
            if self._retry_aborted(generation):
                return
            failed, _ = self._run_retry_history_side_effect(
                generation,
                self._history.fail,
                record_id,
                exc,
            )
            if failed:
                self._run_retry_side_effect(
                    generation,
                    self._notify,
                    f"Não foi possível recuperar a gravação: {exc}",
                    key="voice-history",
                )
        finally:
            with self._lock:
                if generation == self._session_generation:
                    self._retry_record_id = None
                    self._retry_generation = None
                    if self._state == STATE_TRANSCRIBING:
                        self._state = (
                            STATE_IDLE if self.settings.enabled else STATE_UNAVAILABLE
                        )
            self._emit_status()

    def _fail_to_idle(self, message, generation=None):
        if generation is None:
            with self._lock:
                generation = self._session_generation
        if self._complete_session(generation, OUTCOME_FAILED):
            self._notify(message, key="voice-error")

    def _persist(self):
        self._persist_payload(voice_settings_payload(self.settings))

    def _persist_payload(self, payload):
        if self._persist_settings is None:
            return
        try:
            self._persist_settings(payload)
        except Exception as exc:
            self._warn(f"Não foi possível gravar as configurações de voz: {exc}")

    def _start_monitor(self):
        self._stop_monitor()
        try:
            dictation = parse_chord(self.settings.hotkey)
            command = parse_chord(self.settings.command_hotkey)
        except ValueError as exc:
            self._warn(f"Atalho de voz inválido: {exc}")
            return
        monitor = VoiceHotkeyMonitor(
            dictation,
            command,
            on_press=self._hotkey_press_from_os,
            on_release=self._hotkey_release_from_os,
            on_escape=self._hotkey_escape_from_os,
        )
        try:
            monitor.start()
        except Exception as exc:
            self._warn(f"Não foi possível observar o atalho de voz: {exc}")
            return
        self._monitor = monitor

    def _stop_monitor(self):
        monitor = self._monitor
        self._monitor = None
        if monitor is not None:
            monitor.stop()

    def _start_worker(self, fn, *args, name=None):
        tracked = _TrackedWorker()
        with self._workers_lock:
            self._workers.append(tracked)

        def run():
            tracked.thread = threading.current_thread()
            try:
                fn(*args)
            finally:
                tracked.done.set()

        try:
            thread = self.task_runner.start(run, name=name)
        except Exception:
            tracked.done.set()
            with self._workers_lock:
                if tracked in self._workers:
                    self._workers.remove(tracked)
            raise
        if tracked.thread is None and thread is not None:
            tracked.thread = thread
        return thread

    def _join_workers(self, timeout, workers=None):
        """Join tracked voice workers except the caller. True if all finished."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        current = threading.current_thread()
        with self._workers_lock:
            workers = list(self._workers if workers is None else workers)
        pending = []
        for tracked in workers:
            if tracked.thread is current:
                pending.append(tracked)
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                pending.append(tracked)
                continue
            tracked.done.wait(remaining)
            if not tracked.done.is_set():
                pending.append(tracked)
        with self._workers_lock:
            self._workers = [
                tracked
                for tracked in self._workers
                if tracked in pending or not tracked.done.is_set()
            ]
            remaining = list(self._workers)
        return not any(tracked.thread is not current for tracked in remaining)

    def _wait_for_workers(self, workers=None):
        """Wait without holding controller state while native work unwinds."""
        current = threading.current_thread()
        if workers is not None:
            for tracked in workers:
                if tracked.thread is current:
                    continue
                tracked.done.wait()
            return
        while True:
            with self._workers_lock:
                pending = [
                    tracked
                    for tracked in self._workers
                    if tracked.thread is not current and not tracked.done.is_set()
                ]
            if not pending:
                return
            for tracked in pending:
                tracked.done.wait()

    def _unload_provider(self, deferred=False):
        with self._unload_lock:
            if self._unload_in_progress:
                return False
            if self._unload_pending and not deferred:
                return False
            self._unload_in_progress = True
        try:
            self._provider.unload()
        except Exception:
            pass
        finally:
            with self._unload_lock:
                self._unload_in_progress = False
                clear_disable = (
                    self._clear_disable_after_unload and not self._unload_pending
                )
                if clear_disable:
                    self._clear_disable_after_unload = False
            if clear_disable:
                with self._lock:
                    self._disable_requested = False
        return True

    def _schedule_unload_after_workers(self, clear_disable=False):
        with self._unload_lock:
            if self._unload_pending or self._unload_in_progress:
                self._clear_disable_after_unload |= clear_disable
                return
            self._unload_pending = True
            self._clear_disable_after_unload = clear_disable

        def unload_when_done():
            try:
                self._wait_for_workers()
                self._unload_provider(deferred=True)
            finally:
                with self._unload_lock:
                    clear_disable = self._clear_disable_after_unload
                    self._unload_pending = False
                    self._clear_disable_after_unload = False
                if clear_disable:
                    with self._lock:
                        self._disable_requested = False

        threading.Thread(
            target=unload_when_done,
            name="voice-deferred-unload",
            daemon=True,
        ).start()

    def _hotkey_press_from_os(self, mode):
        # OS callback: enqueue only.
        if self._shutdown.is_set():
            return
        self._start_worker(self.handle_hotkey_press, mode, name="voice-press")

    def _hotkey_release_from_os(self, mode):
        if self._shutdown.is_set():
            return
        self._start_worker(self.handle_hotkey_release, mode, name="voice-release")

    def _hotkey_escape_from_os(self):
        if self._shutdown.is_set():
            return
        self._start_worker(self.cancel, name="voice-escape")

    def delete_active_model(self):
        """Disable voice, then remove only the catalog directory of the profile."""
        with self._lock:
            if self._meeting_token is not None:
                return False
        profile = self.settings.profile
        self.disable()
        with self._unload_lock:
            unload_busy = self._unload_pending or self._unload_in_progress
        if unload_busy:
            self._notify(
                "A entrada por voz ainda está encerrando; tente remover o modelo "
                "novamente em instantes.",
                key="voice-model",
            )
            return False
        self._provider.delete_profile(profile)
        return True


    def reserve_for_meeting(self):
        """Temporarily close admission and unload dictation without persisting disable."""
        token = object()
        with self._lock:
            if (self._shutdown.is_set() or self._meeting_token is not None
                    or self._disable_requested or self._model_download_active
                    or self._state not in (STATE_IDLE, STATE_UNAVAILABLE)
                    or self._capture_starting):
                raise VoiceRuntimeError("Aguarde a entrada por voz terminar antes de gravar.")
            self._meeting_token = token
            previous_state = self._state
            self._state = STATE_UNAVAILABLE
        self._emit_status()
        try:
            if not self._join_workers(_SHUTDOWN_JOIN_SECONDS):
                raise VoiceRuntimeError("A entrada por voz ainda está encerrando.")
            with self._unload_lock:
                if self._unload_pending or self._unload_in_progress:
                    raise VoiceRuntimeError("O modelo de ditado ainda está encerrando.")
            self._provider.unload()
            return token
        except Exception:
            with self._lock:
                if self._meeting_token is token:
                    self._meeting_token = None
                    self._state = previous_state
            self._emit_status()
            raise

    def release_meeting(self, token):
        with self._lock:
            if self._meeting_token is not token:
                return
            self._meeting_token = None
            restore = self.settings.enabled and not self._shutdown.is_set()
            self._state = STATE_LOADING if restore else STATE_UNAVAILABLE
            generation = self._session_generation
            if restore:
                self._cancel.clear()
        self._emit_status()
        if restore:
            self._start_worker(self._restore_after_meeting_worker, generation, name="voice-restore")

    def _restore_after_meeting_worker(self, generation):
        try:
            self._provider.prepare(self.settings.profile, self.settings.language,
                                   cancel_event=self._cancel, allow_download=False)
        except (VoiceRuntimeError, VoiceModelError) as exc:
            with self._lock:
                if generation != self._session_generation:
                    return
                self._state = STATE_UNAVAILABLE
                self._load_error = str(exc)
            self._notify(str(exc), key="voice-restore")
            self._emit_status()
            return
        with self._lock:
            if (generation != self._session_generation or self._shutdown.is_set()
                    or not self.settings.enabled):
                return
            self._state = STATE_IDLE
        self._start_monitor()
        self._emit_status()


def _flatten(chunk):
    if chunk is None:
        return []
    if hasattr(chunk, "flatten"):
        return [float(value) for value in chunk.flatten()]
    return [float(value) for value in chunk]
