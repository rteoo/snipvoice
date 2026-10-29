"""JSON-RPC bridge between the engine and the Tauri manager window.

The engine stays the only process that records, transcribes, owns the tray,
hotkeys and HUD. The manager window is a separate process (``desktop/``) that
this module spawns and talks to with one JSON-RPC 2.0 message per line on the
child's stdin/stdout. Requests run on a small worker pool, never on a
keyboard or Tk thread; state reaches the window as pushed notifications.
"""

import json
import math
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import i18n
from i18n import tr
from i18n_en_us import EN_US
from meeting_files import _redact_absolute_paths
from meeting_gui import (
    PROCESSING_SESSION_STATE,
    SESSION_STATES,
    SOURCE_LABELS,
    UNKNOWN_SESSION_STATE,
    LIBRARY_PROCESSING_JOBS,
    active_transcript_revision,
    format_recording_status,
    format_time,
    hotkey_display,
    lost_sources,
    manager_poll_interval,
    meter_decibels,
    meter_value,
    recording_tone,
    source_health,
    validated_settings,
)
from meeting_settings import FOLLOW_DICTATION, dictation_profile
from meeting_support import CAPTURE_STATES, processing_job_label, start_blocker
from meeting_text import format_local_timestamp
from voice_hotkey import DEFAULT_COMMAND_HOTKEY, DEFAULT_DICTATION_HOTKEY

PROTOCOL_VERSION = 1
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
APP_ERROR = -32000
MAX_LINE_BYTES = 8 * 1024 * 1024
MAX_DETAIL_CHARS = 4096
MAX_TITLE_CHARS = 400
LIBRARY_PAGE_SIZE = 50
HISTORY_LIMIT_MAX = 200
# The manager waits this long for a closed stdin before the process is killed.
SHUTDOWN_GRACE_SECONDS = 3.0
MANAGER_EXE_ENV = "SNIPVOICE_MANAGER_EXE"
MANAGER_KIND_ENV = "SNIPVOICE_MANAGER"
# A source must stay silent this long before the window calls it lost;
# a headset switch mends itself in less (DESIGN.md, recording trouble).
LOST_SOURCE_GRACE = 3.0
TONES = {
    "accent_mark": "live", "success": "ok", "info": "busy", "warning": "warning",
    "danger": "danger", "text_muted": "muted", "neutral": "muted", "text": "default", None: "default",
}


class RpcFailure(Exception):
    """A refusal the window shows as written: ``message`` is already translated."""

    def __init__(self, message, *, detail="", code=APP_ERROR):
        super().__init__(message)
        self.message = message
        self.detail = detail
        self.code = code


def technical_detail(error):
    """One bounded, path-redacted line naming an unexpected failure."""
    text = f"{type(error).__name__}: {error}"
    return _redact_absolute_paths(text)[:MAX_DETAIL_CHARS]


class RpcEndpoint:
    """One JSON-RPC peer over a pair of binary line streams.

    ``methods`` maps a method name to ``handler(params) -> result``. Each
    request runs on ``executor``; replies may therefore leave out of order,
    which JSON-RPC allows. Writes are serialized; a broken pipe closes the
    endpoint once and calls ``on_close``.
    """

    def __init__(self, reader, writer, methods, *, executor, on_close=None, logger=None):
        self._reader = reader
        self._writer = writer
        self._methods = methods
        self._executor = executor
        self._on_close = on_close
        self._logger = logger
        self._write_lock = threading.Lock()
        self._closed = threading.Event()
        self._thread = None

    @property
    def closed(self):
        return self._closed.is_set()

    def start(self):
        self._thread = threading.Thread(target=self._read_loop, name="manager-rpc-reader", daemon=True)
        self._thread.start()

    def join(self, timeout=None):
        if self._thread is not None:
            self._thread.join(timeout)

    def notify(self, method, params=None):
        return self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def close(self):
        if self._closed.is_set():
            return
        self._closed.set()
        try:
            self._writer.close()
        except OSError:
            pass
        if self._on_close is not None:
            self._on_close()

    def _write(self, message):
        if self._closed.is_set():
            return False
        data = (json.dumps(message, ensure_ascii=False, separators=(",", ":"), default=str) + "\n").encode("utf-8")
        try:
            with self._write_lock:
                self._writer.write(data)
                self._writer.flush()
        except (OSError, ValueError):
            self.close()
            return False
        return True

    def _read_loop(self):
        try:
            while not self._closed.is_set():
                line = self._reader.readline(MAX_LINE_BYTES + 1)
                if not line:
                    break
                if len(line) > MAX_LINE_BYTES:
                    self._warn("manager sent an oversized line; closing the bridge")
                    break
                self._receive(line)
        except (OSError, ValueError):
            pass
        finally:
            self.close()
            try:
                self._reader.close()
            except OSError:
                pass

    def _receive(self, line):
        try:
            message = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._warn("manager sent a line that is not JSON")
            return
        if not isinstance(message, dict) or not isinstance(message.get("method"), str):
            if isinstance(message, dict) and "id" in message:
                self._reply_error(message.get("id"), INVALID_REQUEST, "Invalid request")
            return
        request_id = message.get("id")
        params = message.get("params", {})
        if not isinstance(params, dict):
            if request_id is not None:
                self._reply_error(request_id, INVALID_PARAMS, "Params must be an object")
            return
        handler = self._methods.get(message["method"])
        if handler is None:
            if request_id is not None:
                self._reply_error(request_id, METHOD_NOT_FOUND, f"Unknown method {message['method']}")
            return
        try:
            self._executor.submit(self._run, handler, request_id, params)
        except RuntimeError:
            self.close()

    def _run(self, handler, request_id, params):
        try:
            result = handler(params)
        except RpcFailure as failure:
            data = {"detail": failure.detail} if failure.detail else None
            self._reply_error(request_id, failure.code, failure.message, data)
        except Exception as exc:  # noqa: BLE001 - every failure must answer the request
            self._warn(f"manager request failed: {technical_detail(exc)}")
            self._reply_error(request_id, INTERNAL_ERROR, tr("Não foi possível concluir a ação."),
                              {"detail": technical_detail(exc)})
        else:
            if request_id is not None:
                self._write({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _reply_error(self, request_id, code, message, data=None):
        if request_id is None:
            return
        error = {"code": code, "message": message}
        if data:
            error["data"] = data
        self._write({"jsonrpc": "2.0", "id": request_id, "error": error})

    def _warn(self, message):
        if self._logger is not None:
            self._logger.warning(message)


def _text(params, name, *, limit, required=False):
    value = params.get(name, "")
    if not isinstance(value, str):
        raise RpcFailure(tr("Pedido inválido."), code=INVALID_PARAMS)
    value = value.strip()
    if required and not value:
        raise RpcFailure(tr("Pedido inválido."), code=INVALID_PARAMS)
    if len(value) > limit:
        raise RpcFailure(tr("O título deve ter até 400 caracteres."), code=INVALID_PARAMS)
    return value


def _session_id(params):
    return _text(params, "id", limit=128, required=True)


def _flag(device, name):
    value = device.get(name)
    return value if isinstance(value, bool) else None


def _seconds(value):
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return 0.0
    return seconds if math.isfinite(seconds) and seconds > 0 else 0.0


def _final_audio_exists(metadata):
    final = metadata.get("final_audio")
    path = final.get("path") if isinstance(final, dict) else None
    return bool(isinstance(path, str) and os.path.isfile(path))


def _endpoint_payload(value):
    """The engine's EndpointSelection payload from the window's choice."""
    if not isinstance(value, dict):
        raise RpcFailure(tr("Atualize e selecione novamente o dispositivo de áudio."), code=INVALID_PARAMS)
    mode = value.get("mode")
    if mode == "manual":
        endpoint = value.get("endpoint_id")
        if not isinstance(endpoint, str) or not endpoint or len(endpoint) > 512:
            raise RpcFailure(tr("Atualize e selecione novamente o dispositivo de áudio."), code=INVALID_PARAMS)
        return {"mode": "manual", "endpoint_id": endpoint, "default_role": "multimedia"}
    role = value.get("default_role", "multimedia")
    if mode != "default" or role not in {"multimedia", "communications"}:
        raise RpcFailure(tr("Atualize e selecione novamente o dispositivo de áudio."), code=INVALID_PARAMS)
    return {"mode": "default", "endpoint_id": "", "default_role": role}


class ManagerApi:
    """The methods the window may call, over explicit engine dependencies.

    Every dependency is passed in, so the API runs against fakes in tests:
    ``meetings`` (MeetingController), ``voice`` (VoiceController),
    ``settings_getter``/``persist_settings`` (settings.json),
    ``startup_status() -> (ready, message)``, ``voice_presentation() ->
    (text, tone)``, ``history_status(status) -> (label, tone)``,
    ``set_voice_enabled(enabled, confirmed) -> dict`` and
    ``on_language_changed(code)``. ``extensions`` add their ``methods()``:
    ``manager_library.LibraryActions`` and ``manager_settings.SettingsActions``.
    """

    def __init__(self, *, meetings, voice, settings_getter, persist_settings, startup_status,
                 voice_presentation, history_status, set_voice_enabled, on_language_changed,
                 display_name, version, extensions=()):
        self.meetings = meetings
        self.voice = voice
        self.settings_getter = settings_getter
        self.persist_settings = persist_settings
        self.startup_status = startup_status
        self.voice_presentation = voice_presentation
        self.history_status = history_status
        self.set_voice_enabled = set_voice_enabled
        self.on_language_changed = on_language_changed
        self.display_name = display_name
        self.version = version
        self.extensions = list(extensions)
        self._devices = []
        self._devices_lock = threading.Lock()
        # The snapshot does not carry the running recording's sources.
        self._recording_sources = None

    def methods(self):
        return {
            **{name: handler for extension in self.extensions for name, handler in extension.methods().items()},
            "app.hello": self.hello,
            "app.setLanguage": self.set_language,
            "app.setAppearance": self.set_appearance,
            "recording.state": lambda _params: self.recording_view(self.meetings.snapshot()),
            "recording.devices": self.devices,
            "recording.start": self.start_recording,
            "recording.stop": lambda _params: {"accepted": bool(self.meetings.stop())},
            "recording.pause": lambda _params: {"accepted": bool(self.meetings.pause())},
            "recording.resume": lambda _params: {"accepted": bool(self.meetings.resume())},
            "recording.preview": self.preview_sources,
            "recording.cancelProcessing": lambda _params: {"accepted": bool(self.meetings.cancel_processing())},
            "voice.state": lambda _params: self.voice_view(),
            "voice.setEnabled": self.voice_set_enabled,
            "voice.history": self.voice_history,
            "voice.retry": lambda params: {"accepted": bool(self.voice.retry_history(_session_id(params)))},
            "voice.copy": lambda params: {"accepted": bool(self.voice.copy_history_transcript(_session_id(params)))},
            "library.list": self.library_list,
            "library.get": self.library_get,
            "library.transcript": self.library_transcript,
            "library.rename": self.library_rename,
        }

    # -- App ------------------------------------------------------------

    def hello(self, _params):
        settings = self.settings_getter()
        ready, message = self.startup_status()
        language = i18n.current_language()
        return {
            "protocol": PROTOCOL_VERSION,
            "displayName": self.display_name,
            "version": self.version,
            "language": language,
            "languages": dict(i18n.LANGUAGE_NAMES),
            "catalog": EN_US if language == "en-US" else {},
            "appearance": settings.get("appearance") if settings.get("appearance") in {"dark", "light", "system"} else "dark",
            "startup": {"ready": bool(ready), "message": tr(message) if message else ""},
            # The window lets this process take the foreground for its file dialogs.
            "enginePid": os.getpid(),
        }

    def set_language(self, params):
        language = params.get("language")
        if language not in i18n.LANGUAGE_NAMES:
            raise RpcFailure(tr("Pedido inválido."), code=INVALID_PARAMS)
        if not self.persist_settings({"language": language}):
            raise RpcFailure(tr("Não foi possível salvar o idioma. Tente de novo."))
        self.on_language_changed(language)
        return {"language": language, "catalog": EN_US if language == "en-US" else {}}

    def set_appearance(self, params):
        appearance = params.get("appearance")
        if appearance not in {"dark", "light", "system"}:
            raise RpcFailure(tr("Pedido inválido."), code=INVALID_PARAMS)
        if not self.persist_settings({"appearance": appearance}):
            raise RpcFailure(tr("Não foi possível salvar a aparência. Tente de novo."))
        return {"appearance": appearance}

    # -- Recording ------------------------------------------------------

    def recording_view(self, snapshot, silent_since=None):
        """The recorder as the window draws it: written words, tones and meter positions."""
        if not isinstance(snapshot, dict):
            snapshot = {}
        ready, startup_message = self.startup_status()
        state = snapshot.get("state", "idle")
        blocker = start_blocker(snapshot)
        hold = "" if ready else tr(startup_message or "Preparando privacidade e recuperação local…")
        sources = self._sources_for(snapshot)
        now = time.monotonic()
        silent_since = silent_since or {}
        tracks = {}
        for track in ("microphone", "system"):
            peak = (snapshot.get("levels") or {}).get(track, 0)
            since = silent_since.get(track)
            text, tone = source_health(track, snapshot, sources, now - since if since else 0)
            tracks[track] = {
                "label": tr(SOURCE_LABELS[track]),
                "level": meter_value(peak),
                "reading": meter_decibels(peak),
                "health": text,
                "tone": TONES.get(tone, "muted"),
                "waveform": [round(float(value), 4) for value in list((snapshot.get("waveforms") or {}).get(track, ()))[-360:]],
            }
        lost = [
            {"track": track, "label": tr(SOURCE_LABELS[track]),
             "silentFor": int(now - silent_since[track])}
            for track, _health in lost_sources(snapshot)
            if track in silent_since and now - silent_since[track] >= LOST_SOURCE_GRACE
        ]
        export = snapshot.get("audio_export") or {}
        playback = snapshot.get("playback") if isinstance(snapshot.get("playback"), dict) else {}
        return {
            "state": state,
            "status": format_recording_status(snapshot, start_held=bool(hold)),
            "tone": TONES.get(recording_tone(snapshot, not hold), "muted"),
            "elapsed": format_time(snapshot.get("elapsed")),
            "capturing": state in CAPTURE_STATES,
            "previewing": bool(snapshot.get("previewing")),
            "processing": bool(snapshot.get("processing")),
            "processingJob": processing_job_label(snapshot),
            "processingSession": snapshot.get("processing_session"),
            "postprocess": snapshot.get("postprocess") or "",
            "canStart": state == "idle" and not blocker and not hold,
            "startBlocker": blocker,
            "hold": hold,
            "canInterrupt": state in {"idle", "postprocessing"} and bool(processing_job_label(snapshot)),
            "canStop": state in {"starting", "recording", "paused"},
            "canPause": state in {"recording", "paused"},
            "paused": state == "paused",
            "error": snapshot.get("error") or "",
            "tracks": tracks,
            "lost": lost,
            "export": ({"percent": export.get("percent"), "label": export.get("label") or "",
                        "stage": export.get("stage"), "sessionId": export.get("session_id")}
                       if export.get("stage") in {"preparing", "encoding", "finalizing"} else None),
            "hotkey": hotkey_display(self.settings_getter().get("meeting_hotkey", "")),
            "playback": {
                "active": bool(playback.get("active")),
                "sessionId": playback.get("session_id"),
                "position": _seconds(playback.get("position", playback.get("start", 0))),
                "failed": bool(playback.get("error")),
                "generation": playback.get("generation") if isinstance(playback.get("generation"), int) else 0,
            },
        }

    def _sources_for(self, snapshot):
        if self._recording_sources:
            return self._recording_sources
        health = snapshot.get("capture_health") or {}
        present = [track for track in ("microphone", "system") if track in health]
        return present[0] if len(present) == 1 else "both"

    def devices(self, _params):
        devices = [dict(device) for device in (self.meetings.devices() or ()) if isinstance(device, dict)]
        with self._devices_lock:
            self._devices = devices
        return {"devices": [
            {"id": str(device.get("id") or ""), "name": " ".join(str(device.get("name") or device.get("id") or "").split()),
             # None means the capture helper does not report default roles at all.
             "kind": device.get("kind"), "default": _flag(device, "default"),
             "communicationsDefault": _flag(device, "communications_default")}
            for device in devices if device.get("kind") in {"microphone", "system"}
        ]}

    def _settings_data(self):
        data = dict(self.settings_getter())
        if data.get("meeting_profile", FOLLOW_DICTATION) == FOLLOW_DICTATION:
            # The dictation model can change while the window is open.
            data["voice_profile"] = dictation_profile(data)
        data.update(meeting_auto_transcribe=True, meeting_auto_summary=True)
        return data

    def saved_meeting_settings(self):
        """``MeetingSettings`` exactly as saved; raises ValueError when they are invalid."""
        data = self._settings_data()
        return validated_settings(data, data.get("voice_hotkey", DEFAULT_DICTATION_HOTKEY),
                                  data.get("voice_command_hotkey", DEFAULT_COMMAND_HOTKEY))

    def _recording_settings(self, params):
        """``MeetingSettings`` from the saved settings plus this recording's sources."""
        sources = params.get("sources")
        if not isinstance(sources, dict):
            raise RpcFailure(tr("Confira as fontes e tente de novo."), code=INVALID_PARAMS)
        data = self._settings_data()
        for track, key in (("microphone", "meeting_input_enabled"), ("system", "meeting_output_enabled")):
            choice = sources.get(track)
            if not isinstance(choice, dict):
                raise RpcFailure(tr("Confira as fontes e tente de novo."), code=INVALID_PARAMS)
            data[key] = choice.get("enabled") is True
            data["meeting_" + track] = _endpoint_payload(choice.get("selection"))
        try:
            settings = validated_settings(data, data.get("voice_hotkey", DEFAULT_DICTATION_HOTKEY),
                                          data.get("voice_command_hotkey", DEFAULT_COMMAND_HOTKEY))
        except ValueError as exc:
            raise RpcFailure(str(exc) or tr("Confira as fontes e tente de novo."), code=INVALID_PARAMS) from exc
        if settings.destination and (not os.path.isabs(settings.destination)
                                     or not os.path.isdir(settings.destination)):
            raise RpcFailure(tr("A pasta padrão de gravações não existe ou não é absoluta."), code=INVALID_PARAMS)
        with self._devices_lock:
            devices = list(self._devices)
        for track in ("microphone", "system"):
            selection = getattr(settings, track)
            if settings.sources in ("both", track) and selection.mode == "manual" and not any(
                    device.get("id") == selection.endpoint_id and device.get("kind") == track for device in devices):
                raise RpcFailure(tr("O dispositivo manual está indisponível. Atualize ou escolha outro dispositivo."),
                                 code=INVALID_PARAMS)
        return settings

    def start_recording(self, params):
        """Start with the same admission as the Tk manager: startup, privacy, notice, then start.

        Returns ``{"status": "started"}``, ``{"status": "consent", "notice"}``
        when the notice must be confirmed first (call again with
        ``consent: true``), or raises a written refusal.
        """
        ready, message = self.startup_status()
        if not ready:
            raise RpcFailure(tr(message or "A gravação aguarda a recuperação do workspace."))
        title = _text(params, "title", limit=MAX_TITLE_CHARS)
        settings = self._recording_settings(params)
        try:
            self.meetings.refresh_privacy_defaults()
        except Exception as exc:
            raise RpcFailure(tr("Privacidade local indisponível; gravação bloqueada."),
                             detail=technical_detail(exc)) from exc
        if self.meetings.recording_notice_required():
            if params.get("consent") is not True:
                return {"status": "consent", "notice": self.meetings.recording_notice()}
            self.meetings.grant_recording_consent()
        try:
            accepted = self.meetings.start(settings, title=title)
        except Exception as exc:
            self.meetings.revoke_recording_consent()
            raise RpcFailure(tr("Não foi possível iniciar a gravação"),
                             detail=technical_detail(exc)) from exc
        if accepted:
            self._recording_sources = settings.sources
            return {"status": "started"}
        self.meetings.revoke_recording_consent()
        snapshot = self.meetings.snapshot()
        job = start_blocker(snapshot)
        if not job:
            raise RpcFailure(tr("Verifique o estado da gravação e tente de novo."))
        if processing_job_label(snapshot):
            raise RpcFailure(tr("Aguardando {job}. Para gravar agora, use Interromper e gravar.", job=job))
        raise RpcFailure(tr("Aguardando {job}.", job=job))

    def preview_sources(self, params):
        settings = self._recording_settings(params)
        result = self.meetings.preview_sources(settings, seconds=3.0) or {}
        errors = [tr(SOURCE_LABELS[track]) for track in result.get("errors", ()) if track in SOURCE_LABELS]
        return {"errors": errors}

    # -- Dictation ------------------------------------------------------

    def voice_view(self):
        text, tone = self.voice_presentation()
        status, _sep, detail = text.partition("\n")
        snapshot = self.voice.status_snapshot() or {}
        total = snapshot.get("download_total") or 0
        return {
            "enabled": bool(self.voice.is_enabled()),
            "state": snapshot.get("state"),
            "status": status,
            "detail": detail,
            "tone": TONES.get(tone, "muted"),
            "download": ({"done": snapshot.get("download_done") or 0, "total": total,
                          "profile": snapshot.get("download_profile") if snapshot.get("download_active") else None}
                         if snapshot.get("download_active") or (snapshot.get("state") == "loading" and total) else None),
        }

    def voice_set_enabled(self, params):
        enabled = params.get("enabled")
        if not isinstance(enabled, bool):
            raise RpcFailure(tr("Pedido inválido."), code=INVALID_PARAMS)
        return self.set_voice_enabled(enabled, params.get("confirmed") is True)

    def voice_history(self, params):
        limit = params.get("limit", 5)
        if not isinstance(limit, int) or not 1 <= limit <= HISTORY_LIMIT_MAX:
            raise RpcFailure(tr("Pedido inválido."), code=INVALID_PARAMS)
        rows = []
        for row in self.voice.history_rows(limit=limit) or ():
            label, tone = self.history_status(row.get("status"))
            text = " ".join(str(row.get("transcript") or row.get("error") or "").split())
            rows.append({
                "id": str(row.get("id") or ""),
                "date": format_local_timestamp(row.get("created_at")),
                "status": label,
                "tone": TONES.get(tone, "muted"),
                "preview": text[:80] + ("…" if len(text) > 80 else ""),
                "retryable": row.get("retryable") is True,
                "copyable": bool(row.get("transcript")),
            })
        return {"rows": rows}

    # -- Library --------------------------------------------------------

    def _session_state(self, item, snapshot):
        """The row's state key (the window's icon), written label and tone."""
        if (snapshot.get("processing_kind") in LIBRARY_PROCESSING_JOBS
                and snapshot.get("processing_session") == item.get("id")):
            key, (label, tone) = "processing", PROCESSING_SESSION_STATE
        elif item.get("status") in SESSION_STATES:
            key = item["status"]
            label, tone = SESSION_STATES[key]
        else:
            key, (label, tone) = "unknown", UNKNOWN_SESSION_STATE
        return key, tr(label), TONES.get(tone, "default")

    def library_list(self, params):
        query = _text(params, "query", limit=500)
        cursor = params.get("cursor")
        if cursor is not None and not isinstance(cursor, (str, int, dict, list)):
            raise RpcFailure(tr("Pedido inválido."), code=INVALID_PARAMS)
        page = self.meetings.list_sessions_page(limit=LIBRARY_PAGE_SIZE, cursor=cursor, query=query, status="")
        snapshot = self.meetings.snapshot()
        items = []
        for item in page.get("items", ()):
            state, label, tone = self._session_state(item, snapshot)
            items.append({
                "id": item.get("id"),
                "title": " ".join(str(item.get("title") or "").split()) or tr("Sem título"),
                "state": state,
                "status": label,
                "tone": tone,
                "date": format_local_timestamp(item.get("created_at")),
                "duration": format_time(item.get("duration")),
            })
        return {"items": items, "nextCursor": page.get("next_cursor")}

    def library_get(self, params):
        session_id = _session_id(params)
        # The capture event log can dwarf the rest and nothing here reads it.
        metadata = self.meetings.get_session(session_id, include_events=False)
        if not isinstance(metadata, dict):
            raise RpcFailure(tr("Gravação não encontrada."))
        state, label, tone = self._session_state(metadata, self.meetings.snapshot())
        return {
            "id": session_id,
            "title": " ".join(str(metadata.get("title") or "").split()) or tr("Sem título"),
            "state": state,
            "status": label,
            "tone": tone,
            "date": format_local_timestamp(metadata.get("created_at")),
            "duration": format_time(metadata.get("duration")),
            "durationSeconds": _seconds(metadata.get("duration")),
            "revision": active_transcript_revision(metadata),
            # Only the saved final audio plays, as in the Tk player.
            "playable": _final_audio_exists(metadata),
        }

    def library_transcript(self, params):
        session_id = _session_id(params)
        style = params.get("style", "full_text")
        if style not in {"full_text", "timestamped"}:
            raise RpcFailure(tr("Pedido inválido."), code=INVALID_PARAMS)
        revision = params.get("revision")
        if revision is not None and not isinstance(revision, (str, int)):
            raise RpcFailure(tr("Pedido inválido."), code=INVALID_PARAMS)
        if revision is None:
            return {"text": "", "truncated": False, "empty": True}
        preview = self.meetings.get_transcript_preview(session_id, revision, style) or {}
        text = str(preview.get("text") or "")
        return {"text": text, "truncated": bool(preview.get("truncated")), "empty": not text.strip()}

    def library_rename(self, params):
        session_id = _session_id(params)
        title = _text(params, "title", limit=MAX_TITLE_CHARS, required=True)
        if not self.meetings.rename_session(session_id, title):
            raise RpcFailure(tr("Não foi possível salvar o título. Tente de novo."))
        return {"title": title}


def manager_executable(resource_dir, repo_root=None):
    """The manager window binary, or None when this build has none.

    ``SNIPVOICE_MANAGER_EXE`` wins; a frozen build ships it beside its
    resources; a source checkout uses the release, then the debug, Cargo build.
    """
    override = os.environ.get(MANAGER_EXE_ENV, "").strip()
    if override:
        return override if os.path.isfile(override) else None
    name = "snipvoice-manager.exe" if sys.platform == "win32" else "snipvoice-manager"
    candidates = [os.path.join(resource_dir, name)]
    if repo_root:
        target = os.path.join(repo_root, "desktop", "src-tauri", "target")
        candidates += [os.path.join(target, "release", name), os.path.join(target, "debug", name)]
    return next((path for path in candidates if os.path.isfile(path)), None)


def tauri_manager_requested():
    """The Tauri window is the default; ``SNIPVOICE_MANAGER=tk`` keeps the Tk manager for now."""
    return os.environ.get(MANAGER_KIND_ENV, "").strip().lower() != "tk"


class ManagerHost:
    """Spawn, focus and stop the manager window; push state while it lives."""

    def __init__(self, api, executable, *, logger=None, on_closed=None, on_spawn_failed=None,
                 on_unusable=None, popen=subprocess.Popen):
        self.api = api
        self.executable = executable
        self.logger = logger
        self.on_spawn_failed = on_spawn_failed
        self.on_closed = on_closed
        # A window that exits before greeting the engine cannot render here
        # (e.g. no WebView2 runtime); the engine then falls back to Tk.
        self.on_unusable = on_unusable
        self._greeted = False
        self._popen = popen
        self._lock = threading.Lock()
        self._process = None
        self._endpoint = None
        self._executor = None
        self._pusher = None
        self._wake = threading.Event()
        self._silent_since = {}
        self._last_recording = None
        self._quitting = False

    @property
    def running(self):
        with self._lock:
            return self._endpoint is not None and not self._endpoint.closed

    def show(self, destination=None):
        """Bring the window forward, starting it first when it is not running.

        A window that cannot start returns False after logging why and
        calling ``on_spawn_failed``, so the caller's tray action is never silent.
        """
        with self._lock:
            if self._quitting:
                return False
            failure = None
            if self._endpoint is None or self._endpoint.closed:
                try:
                    self._spawn()
                except OSError as exc:
                    failure = exc
            endpoint = self._endpoint
        if failure is not None:
            self._warn(f"manager window did not start: {technical_detail(failure)}")
            if self.on_spawn_failed is not None:
                self.on_spawn_failed()
            return False
        return endpoint.notify("window.show", {"destination": destination} if destination else {})

    def notify(self, method, params=None):
        with self._lock:
            endpoint = self._endpoint
        return bool(endpoint is not None and endpoint.notify(method, params))

    def voice_changed(self):
        """Engine callback: dictation state moved; push it when a window listens."""
        if self.running:
            try:
                self.notify("voice.state", self.api.voice_view())
            except Exception as exc:  # noqa: BLE001 - a status push never breaks the voice worker
                self._warn(f"voice state push failed: {technical_detail(exc)}")

    def shutdown(self):
        with self._lock:
            self._quitting = True
            process, endpoint = self._process, self._endpoint
        self._wake.set()
        if endpoint is not None:
            endpoint.close()
        if process is not None:
            try:
                process.wait(SHUTDOWN_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()

    def _spawn(self):
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        process = self._popen(
            [self.executable], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, creationflags=flags, bufsize=0,
        )
        executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="manager-rpc")
        methods = self.api.methods()
        hello = methods["app.hello"]

        def greeted(params):
            self._greeted = True
            return hello(params)

        methods["app.hello"] = greeted
        self._greeted = False
        endpoint = RpcEndpoint(process.stdout, process.stdin, methods, executor=executor,
                               on_close=lambda: self._closed(endpoint), logger=self.logger)
        self._process, self._endpoint, self._executor = process, endpoint, executor
        self._last_recording = None
        self._wake.clear()
        endpoint.start()
        self._pusher = threading.Thread(target=self._push_loop, args=(endpoint,),
                                        name="manager-state", daemon=True)
        self._pusher.start()

    def _closed(self, endpoint):
        with self._lock:
            if self._endpoint is not endpoint:
                return
            self._endpoint = None
            executor, self._executor = self._executor, None
            quitting = self._quitting
            greeted = self._greeted
        self._wake.set()
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
        if quitting:
            return
        if not greeted and self.on_unusable is not None:
            self._warn("manager window closed before reaching the engine; falling back to the Tk manager")
            self.on_unusable()
        elif self.on_closed is not None:
            self.on_closed()

    def _push_loop(self, endpoint):
        """Push the recorder at the Tk manager's cadence, only when it changed."""
        while not endpoint.closed:
            try:
                snapshot = self.api.meetings.snapshot()
                if not isinstance(snapshot, dict):
                    snapshot = {}
                self._track_silence(snapshot)
                view = self.api.recording_view(snapshot, self._silent_since)
                if view != self._last_recording:
                    self._last_recording = view
                    endpoint.notify("recording.state", view)
                interval = manager_poll_interval(snapshot)
            except Exception as exc:  # noqa: BLE001 - the loop outlives one bad snapshot
                self._warn(f"recording state push failed: {technical_detail(exc)}")
                interval = 2000
            if self._wake.wait(interval / 1000):
                return

    def _track_silence(self, snapshot):
        lost = {track for track, _health in lost_sources(snapshot)}
        now = time.monotonic()
        for track in ("microphone", "system"):
            if track in lost:
                self._silent_since.setdefault(track, now)
            else:
                self._silent_since.pop(track, None)

    def _warn(self, message):
        if self.logger is not None:
            self.logger.warning(message)
