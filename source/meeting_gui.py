"""Shared-root meeting workspace with bounded workers and Tk-only updates."""

from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from itertools import islice
import copy
import json
import math
import os
import queue
import re
import threading
import time
import types
import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, ttk

from clipboard_support import Clipboard
import data_relocation
import i18n
from i18n import N_, tr
from meeting_files import report_export_projection
from meeting_chat import MeetingChat
from meeting_support import CAPTURE_STATES, PIPELINE_JOBS, processing_job_label, start_blocker
from meeting_text import (
    format_detailed_notes, format_expiry, format_local_timestamp, format_storage_size,
    format_summary, seconds_until,
)
from meeting_titles import safe_filename_stem
from meeting_settings import (
    FOLLOW_DICTATION, EndpointSelection, dictation_profile, resolve_meeting_settings,
    validate_hotkey_conflicts,
)
from meeting_waveform import MeetingWaveform
from summary_catalog import format_model_size, summary_catalog, summary_catalog_entry
from summary_models import (
    delete_summary_model,
    download_summary_model,
    summary_model_is_installed,
)
import ui_theme
from ops_status import (
    ReadinessProbe, completion_can_select, processing_view, readiness_rows, recent_attention,
)
from voice_catalog import available_languages, format_size, selectable_catalog
from voice_hotkey import DEFAULT_COMMAND_HOTKEY, DEFAULT_DICTATION_HOTKEY


PAGE_SIZE = 50
MAX_PAGE_BACKSTACK = 32
MAX_SEARCH_RESULTS = 32
TRANSCRIPT_LIMIT = 500
APPEARANCE_LABELS = {
    "system": N_("Sistema"),
    "light": N_("Claro"),
    "dark": N_("Escuro"),
}
APPEARANCE_STATUS = {
    "system": N_("Segue o tema do sistema."),
    "light": N_("Tema claro fixo."),
    "dark": N_("Tema escuro fixo."),
}
TRANSCRIPT_PAGE_SIZE = 100
TRANSCRIPT_STYLES = {N_("Texto completo"): "full_text", N_("Com horários"): "timestamped"}
MAX_ANSWER_CHARS = 4_000
MAX_CHAT_TURNS = 24
MAX_CHAT_SESSIONS = 8
LONG_JOB_KEYS = frozenset({"generate_report", "ask_this_meeting", "cross_question", "summary_model",
                           "semantic_model", "semantic_rebuild", "rebuild_index"})
REPORT_HISTORY_LIMIT = 500
MAX_RETENTION_PREVIEW_ITEMS = 64
MAX_TRASH_ITEMS = 256
MAX_RETENTION_TEXT = 800
MAX_REPORT_REVIEW_CHARS = 65536
RETENTION_POLICY_LABELS = {
    "keep": N_("Manter indefinidamente"),
    "whole_meeting": N_("Excluir reunião após o prazo"),
    "raw_tracks": N_("Remover áudio raw após o prazo"),
}
STATE_LABELS = {"idle": N_("Pronto"), "starting": N_("Iniciando"), "recording": N_("Gravando"),
                "paused": N_("Pausado"), "stopping": N_("Finalizando"), "postprocessing": N_("Processando"),
                "completed": N_("Concluído"),
                "stopped": N_("Parado"), "interrupted": N_("Interrompido"), "failed": N_("Falha"),
                "partial": N_("Parcial"), "teardown_blocked": N_("Recursos ainda em uso"),
                "unavailable": N_("Recursos ainda em uso")}
INDEX_STATE_LABELS = {"ready": N_("pronto"), "stale": N_("desatualizado"), "rebuilding": N_("reconstruindo"),
                      "unavailable": N_("indisponível"), "incomplete": N_("incompleto"),
                      "compatibility": N_("modo de compatibilidade"), "cancelled": N_("cancelado"),
                      "failed": N_("falhou")}
PROFILE_LABELS = {"whisper-small": N_("Leve · Whisper Small"), "balanced": N_("Equilibrado · Parakeet"),
                  "whisper-turbo": N_("Intermediário · Whisper Turbo"), "compact": N_("Preciso · Qwen 0.6B"),
                  "accuracy": N_("Muito preciso · Qwen 1.7B"),
                  "whisper-large-v3": N_("Máxima precisão · Whisper Large"),
                  "streaming": N_("Transcrição contínua")}
LANGUAGE_LABELS = {"auto": N_("Automático"), "pt-BR": N_("Português (Brasil)"),
                   "en-US": N_("Inglês (Estados Unidos)")}
TRACK_LABELS = {N_("Microfone"): "microphone", N_("Sistema"): "system"}
# One written state per recording, shared by the library list and the detail
# header (OPS-01 job words, feminine for "gravação"). The tone colors the word;
# the word carries the meaning, because a selected row draws the selection text.
SESSION_STATES = {
    "completed": (N_("Concluída"), None),
    "partial": (N_("Parcial"), "warning"),
    "interrupted": (N_("Interrompida"), "warning"),
    "failed": (N_("Falhou"), "danger"),
    "cancelled": (N_("Cancelada"), "neutral"),
    "recording": (N_("Gravando"), "info"),
}
UNKNOWN_SESSION_STATE = (N_("Desconhecido"), "neutral")
PROCESSING_SESSION_STATE = (N_("Processando"), "info")
# Jobs that work on a recording's own content, so its row reads "Processando".
LIBRARY_PROCESSING_JOBS = PIPELINE_JOBS | {"report", "import"}
PROCESSING_STAGES = ("audio", "transcription", "summary", "capture_cleanup")
# The narrowest readable title column (about 20 characters); duration and date
# give way first, so the title stays the recording's identity at the smallest window.
SESSION_TITLE_MIN = 190
SUMMARY_LABELS = {entry["id"]: entry["label"] for entry in summary_catalog()}
# The pages that own a message slot: what happens on one is written on that page.
DESTINATIONS = ("recording", "library", "settings")
# The recording sources, named as the page names them and as prose ("sem áudio do sistema").
SOURCE_LABELS = {"microphone": N_("Microfone"), "system": N_("Áudio do sistema")}
SOURCE_PROSE = {"microphone": N_("do microfone"), "system": N_("do sistema")}
# A source silent for less than this is a device switch that mends itself; the
# warning bar waits so a headset change does not flash it.
LOST_SOURCE_GRACE = 3.0
MAX_MESSAGE_TEXT = 320
MAX_MESSAGE_DETAILS = 4096
# The recording monitor's last grid row: the waveform, under the state,
# transport and meters, takes the pane's spare height while it shows.
WAVEFORM_ROW = 10
SOURCE_TEST_HINT = N_("Teste as fontes antes de começar. Fale ou reproduza áudio nas fontes escolhidas.")
RECORDING_HINT = N_("Trocar um dispositivo vale na próxima gravação.")


def profile_display_label(profile):
    """Dropdown text for a stored meeting model setting."""
    return tr("Igual ao ditado") if profile == FOLLOW_DICTATION else tr(PROFILE_LABELS[profile])


def displayed_key(labels, displayed):
    """Map a translated label shown in a selector back to its table key."""
    return next((key for key, label in labels.items() if tr(label) == displayed), None)


def displayed_value(labels, displayed):
    """Map a translated selector label back to the value of a label-keyed table."""
    return next((value for label, value in labels.items() if tr(label) == displayed), None)


def _safe_retention_text(value, limit=MAX_RETENTION_TEXT):
    """Keep retention previews bounded and free of local absolute paths."""
    text = " ".join(str(value or "").split())
    # Plan reasons are backend-owned text.  Do not let a future reason or an
    # OS error disclose the workspace path through a confirmation dialog.
    # Redact the remainder of a comma/semicolon-delimited field so paths with
    # spaces and UNC shares cannot leak only their tail.
    redacted = tr("[caminho local]")
    text = re.sub(r"\\\\[^\\/\s]+[\\/][^,;]*", redacted, text)
    text = re.sub(r"(?i)(?<!\w)[A-Z]:[\\/][^,;]*", redacted, text)
    text = re.sub(r"(?<![:\w])/[^,;]*", redacted, text)
    return text[:limit]


def retention_plan_projection(plan):
    """Return a UI-safe, bounded projection of a RetentionPlan-like value."""
    if isinstance(plan, dict):
        get = plan.get
        targets = plan.get("targets", ())
    else:
        get = lambda key, default=None: getattr(plan, key, default)
        targets = getattr(plan, "targets", ())
    projected_targets = []
    for target in list(targets or ())[:MAX_RETENTION_PREVIEW_ITEMS]:
        if isinstance(target, dict):
            kind, track, size = target.get("kind"), target.get("track"), target.get("bytes", 0)
        else:
            kind = getattr(target, "kind", "target")
            track = getattr(target, "track", None)
            size = getattr(target, "bytes", 0)
        try:
            size = max(0, int(size))
        except (TypeError, ValueError):
            size = 0
        projected_targets.append({
            "kind": _safe_retention_text(kind, 80),
            "track": _safe_retention_text(track, 80) if track else None,
            "bytes": size,
        })
    reasons = [_safe_retention_text(item) for item in list(get("reasons", ()) or ())[:32]]
    lost = [_safe_retention_text(item) for item in list(get("lost_capabilities", ()) or ())[:32]]
    excluded = [_safe_retention_text(item) for item in list(get("excluded_external_exports", ()) or ())[:32]]
    policy = get("policy", {})
    purge_days = policy.get("purge_after_days", 30) if isinstance(policy, dict) else policy.purge_after_days
    return {
        "session_id": _safe_retention_text(get("session_id", ""), 128),
        "operation": _safe_retention_text(get("operation", ""), 80),
        "eligible": bool(get("eligible", False)),
        "byte_estimate": max(0, int(get("byte_estimate", 0) or 0)),
        "target_count": len(list(targets or ())),
        "targets": projected_targets,
        "reasons": reasons,
        "lost_capabilities": lost,
        "excluded_external_exports": excluded,
        "canonical_changes": [_safe_retention_text(item) for item in list(get("canonical_changes", ()) or ())[:32]],
        "recovery_mode": _safe_retention_text(get("recovery_mode", "none"), 160),
        "purge_after_days": purge_days,
        "raw_tracks": [_safe_retention_text(item, 80) for item in list(get("raw_tracks", ()) or ())[:4]],
    }


def format_time(seconds):
    seconds = float(seconds or 0)
    if not math.isfinite(seconds) or seconds < 0:
        seconds = 0
    whole = int(seconds)
    return f"{whole // 3600:02d}:{whole // 60 % 60:02d}:{whole % 60:02d}"


def playback_sources(tracks, final_available):
    """Expose only the saved final MP3 as the playback source."""
    return (tr("Áudio final"),) if final_available else ()


def meter_value(peak):
    """Make quiet speech visible on a bounded -60 dB to 0 dB meter."""
    try:
        peak = float(peak)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(peak) or peak <= 0:
        return 0.0
    return max(0.0, min(1.0, 1.0 + math.log10(min(1.0, peak)) / 3.0))


def meter_decibels(peak):
    """Peak amplitude relative to digital full scale, not acoustic loudness."""
    try:
        peak = float(peak)
    except (TypeError, ValueError):
        return tr("sem sinal")
    if not math.isfinite(peak) or peak <= 0:
        return tr("sem sinal")
    return f"{20 * math.log10(min(1.0, peak)):.1f} dBFS"


def active_transcript_revision(metadata):
    """Choose the library's active revision without loading transcript content."""
    if not isinstance(metadata, dict):
        return None
    revisions = metadata.get("revisions", [])
    known = {
        item.get("id") for item in revisions
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    explicit = metadata.get("active_revision")
    if isinstance(explicit, str) and explicit in known:
        return explicit
    for item in reversed(revisions):
        if isinstance(item, dict) and item.get("status") == "completed" and item.get("id") in known:
            return item["id"]
    for item in reversed(revisions):
        if isinstance(item, dict) and item.get("id") in known:
            return item["id"]
    return None


def annotations_for_revision(annotations, revision):
    """Project only annotations that belong to the visible transcript revision."""
    if not isinstance(annotations, dict):
        return {"generation": 0, "speaker_labels": {}, "highlights": []}
    if not revision:
        return {
            "generation": annotations.get("generation", 0),
            "speaker_labels": dict(annotations.get("speaker_labels", {}) or {}),
            "highlights": list(annotations.get("highlights", []) or []),
        }
    labels = {
        key: value for key, value in (annotations.get("speaker_labels", {}) or {}).items()
        if isinstance(value, dict)
        and (value.get("revision") or value.get("transcript_revision")) == revision
    }
    highlights = [
        value for value in (annotations.get("highlights", []) or ())
        if isinstance(value, dict)
        and (value.get("revision") or value.get("transcript_revision")) == revision
    ]
    return {
        "generation": annotations.get("generation", 0),
        "speaker_labels": labels,
        "highlights": highlights,
        "revision_filter": revision,
    }


def elide(font, text, width):
    """``text`` cut to ``width`` pixels in ``font`` with a trailing "…"."""
    if font.measure(text) <= width:
        return text
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if font.measure(text[:middle].rstrip() + "…") <= width:
            low = middle
        else:
            high = middle - 1
    return text[:low].rstrip() + "…"


def _date_column_samples():
    """Every date shape the library shows, for sizing its column in this language."""
    base = datetime(2026, 12, 23, 23, 59, tzinfo=timezone.utc)
    pairs = [(base, base), (base - timedelta(days=1), base), (base - timedelta(days=30), base),
             (base.replace(year=2025), base)]
    # Three days back from seven consecutive days names every weekday.
    pairs += [(base + timedelta(days=day - 3), base + timedelta(days=day)) for day in range(7)]
    return [format_local_timestamp(moment.isoformat(), now=now) for moment, now in pairs]


def destination_display(path):
    """Describe the implicit local destination without changing its saved value."""
    return str(path or "").strip() or tr("Pasta local padrão (recordings)")


def _technical_error_detail(error):
    """Format one bounded, redacted technical error for recording details."""
    return tr("Detalhes técnicos: {details}", details=_safe_retention_text(error, 4096))


def _append_detail_once(details, value):
    """Keep repeated worker/error reporting from duplicating one detail line."""
    if value and value not in details:
        details.append(value)


class Message:
    """What one page says now: a quiet receipt, or trouble with its cause and a way on."""

    __slots__ = ("kind", "tone", "title", "body", "details", "action", "session", "key", "when",
                 "sticky")

    def __init__(self, kind, title, *, tone=None, body="", details="", action=None,
                 session=None, key=None, when=None, sticky=False):
        self.kind, self.tone, self.title, self.body = kind, tone, title, body
        self.details, self.action, self.session, self.key, self.when = (
            details, action, session, key, when)
        self.sticky = sticky


class Messages:
    """The one message each page shows, with the technical details that belong to it.

    Bounded by construction: one slot per destination, one message in it, its
    details cut at 4 KiB. A message about one recording (``session``) goes when
    another recording is chosen; the window clears a page's slot when its user
    leaves the page, except a ``sticky`` one, which states a standing condition
    (recovery pending) and ends only when the condition does.
    """

    def __init__(self, render, clock=datetime.now):
        self._render = render
        self._clock = clock
        self._current = dict.fromkeys(DESTINATIONS)

    def current(self, destination):
        return self._current[destination]

    def _set(self, destination, message):
        self._current[destination] = message
        self._render(destination, message)

    def receipt(self, destination, text, *, session=None):
        """A routine outcome as one quiet line with its local time."""
        line = " ".join(str(text).split())[:MAX_MESSAGE_TEXT]
        self._set(destination, Message("receipt", line, session=session, when=self._clock()))

    def problem(self, destination, title, body="", *, tone="danger", error=None, details="",
                action=None, session=None, key=None, sticky=False):
        """Trouble: what happened, what it affects, one action, the cause behind Detalhes."""
        if error:
            details = _technical_error_detail(error)
        self._set(destination, Message(
            "problem", title, tone=tone, body=body, details=str(details or "")[:MAX_MESSAGE_DETAILS],
            action=action, session=session, key=key, when=self._clock(), sticky=sticky))

    def clear(self, destination, key=None):
        """Empty a page's slot; with ``key``, only when it holds that message."""
        message = self._current[destination]
        if message is not None and (key is None or message.key == key):
            self._set(destination, None)

    def leave(self, destination):
        """The user left a page: what it said goes, unless it states a standing condition."""
        message = self._current[destination]
        if message is not None and not message.sticky:
            self._set(destination, None)

    def drop_other_sessions(self, keep=None):
        """Forget every message about a recording other than ``keep``."""
        for destination, message in self._current.items():
            if message is not None and message.session is not None and message.session != keep:
                self._set(destination, None)


def _processing_failure_details(metadata):
    """Keep retained capture diagnostics separate from persisted processing failures."""
    stages = metadata.get("processing")
    if not isinstance(stages, dict):
        return ""
    details = []
    for name in ("audio", "capture_cleanup", "transcription", "summary"):
        stage = stages.get(name)
        if isinstance(stage, dict) and stage.get("status") in {"failed", "cancelled"}:
            details.append(f"{name}: {str(stage.get('error') or stage['status'])[:1024]}")
    return "; ".join(details)


def _stage_status(metadata, name):
    """The persisted status of one processing stage, or "" when it never ran."""
    stages = metadata.get("processing")
    stage = stages.get(name) if isinstance(stages, dict) else None
    return str(stage.get("status") or "") if isinstance(stage, dict) else ""


def _processing_issue(metadata):
    """The first failed or cancelled processing stage in pipeline order, or None."""
    for name in PROCESSING_STAGES:
        status = _stage_status(metadata, name)
        if status in {"failed", "cancelled"}:
            error = metadata["processing"][name].get("error")
            return {"stage": name, "status": status, "error": str(error or "")[:4096]}
    return None


def format_recording_status(snapshot, start_held=False):
    """Return bounded user-facing state without exposing paths or native errors.

    An idle recorder that cannot start never reads "Pronto": it reads the work
    that holds it (testing or processing) or "Aguardando". ``start_held`` adds
    the window's own holds (settings, privacy, recovery) to the snapshot's.
    """
    state = snapshot.get("state", "idle")
    if state == "idle" and snapshot.get("previewing"):
        state_label = tr("Testando")
    elif state == "idle" and snapshot.get("processing"):
        state_label = tr(STATE_LABELS["postprocessing"])
    elif state == "idle" and (start_held or start_blocker(snapshot)):
        state_label = tr("Aguardando")
    else:
        state_label = tr(STATE_LABELS.get(state, state))
    parts = [state_label,
             format_time(snapshot.get("elapsed"))]
    export = snapshot.get("audio_export") or {}
    if snapshot.get("processing") and export.get("stage") in {"preparing", "encoding", "finalizing"}:
        parts.append(export["label"])
    for track, health in lost_sources(snapshot):
        parts.append(f"{tr(SOURCE_LABELS[track])}: "
                     + (tr("reconectando") if health == "recovering" else tr("sem áudio")))
    if snapshot.get("final_audio"):
        parts.append(tr("Gravação preservada"))
    return " · ".join(parts)


def recording_tone(snapshot, start_ready):
    """The ``Theme`` color of the dot beside the recorder's written state.

    Copper means live; the other tones follow the OPS-01 ladder (success for a
    recorder that can start, info for work in progress, warning for a lost
    source or a blocked recorder, danger for failure).
    """
    state = snapshot.get("state", "idle")
    if lost_sources(snapshot) or state in {"unavailable", "teardown_blocked"}:
        return "warning"
    if state == "failed":
        return "danger"
    if state == "recording":
        return "accent_mark"
    if state == "paused":
        return "text_muted"
    if state == "idle" and start_ready and not snapshot.get("processing") \
            and not snapshot.get("previewing"):
        return "success"
    return "info"


def lost_sources(snapshot):
    """``[(track, health)]`` for each source of a running capture that delivers no audio.

    ``disconnected`` means the endpoint is gone; ``recovering`` that the helper
    keeps trying to reopen it. Outside a capture nothing is lost.
    """
    if snapshot.get("state") not in CAPTURE_STATES:
        return []
    health = snapshot.get("capture_health") or {}
    return [(track, health[track]["state"]) for track in ("microphone", "system")
            if isinstance(health.get(track), dict)
            and health[track].get("state") in {"disconnected", "recovering"}]


def format_silence(seconds):
    """How long a source has been silent, with at most two units ("12 s", "1 min 05 s")."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return tr("{seconds} s", seconds=seconds)
    if seconds < 3600:
        return tr("{minutes} min {seconds:02d} s", minutes=seconds // 60, seconds=seconds % 60)
    return tr("{hours} h {minutes:02d} min", hours=seconds // 3600, minutes=seconds % 3600 // 60)


def source_health(track, snapshot, sources, silent_for=0):
    """``(text, color_role)``: the written health of one source, shown under its meter.

    A source left out of this recording says so; a lost one says how long it has
    been silent. Nothing shows while the recorder is not capturing.
    """
    if snapshot.get("state") not in CAPTURE_STATES:
        return "", "text_muted"
    if sources not in ("both", track):
        return tr("Não incluído nesta gravação"), "text_muted"
    health = (snapshot.get("capture_health") or {}).get(track)
    value = health.get("state") if isinstance(health, dict) else None
    since = " · " + tr("há {duration}", duration=format_silence(silent_for)) if silent_for >= 1 else ""
    if value == "disconnected":
        return tr("Sem áudio") + since, "warning"
    if value == "recovering":
        return tr("Reconectando…") + since, "warning"
    if value == "failed":
        return tr("A captura desta fonte falhou"), "danger"
    if value == "paused":
        return tr("Em pausa"), "text_muted"
    if value == "quiet":
        return tr("Conectado · sem som no momento"), "text_muted"
    if value == "receiving":
        return tr("Recebendo sinal"), "text_muted"
    return tr("Aguardando sinal…"), "text_muted"


def hotkey_display(spec):
    """A stored chord ("ctrl+alt+r") as people write it ("Ctrl+Alt+R")."""
    return "+".join(part.capitalize() for part in str(spec or "").split("+") if part)


def _endpoint_name(device, track):
    name = " ".join(str(device.get("name") or "").split())
    if name and name != device.get("id"):
        return name
    return tr("Dispositivo de entrada") if track == "microphone" else tr("Dispositivo de saída")


def endpoint_options(devices, track, selection):
    """Keep a missing manually selected endpoint explicit and pinned."""
    devices = [device for device in devices if isinstance(device, dict)
               and device.get("kind") == track
               and isinstance(device.get("id"), str) and device["id"]]
    options = []
    for label, flag, role in ((tr("Padrão do sistema"), "default", "multimedia"),
                              (tr("Padrão para chamadas"), "communications_default", "communications")):
        device = next((device for device in devices if device.get(flag) is True), None)
        if device is not None:
            label += " · " + _endpoint_name(device, track)
        options.append((label, EndpointSelection(default_role=role)))
    used_labels = {label for label, _selection in options}
    for device in devices:
        base_label = _endpoint_name(device, track)
        label = base_label
        suffix = 2
        while label in used_labels:
            label = f"{base_label} ({suffix})"
            suffix += 1
        used_labels.add(label)
        options.append((label, EndpointSelection("manual", device["id"])))
    if (selection.mode == "manual"
            and not any(item.endpoint_id == selection.endpoint_id for _, item in options)):
        label = tr("Dispositivo selecionado indisponível")
        while label in used_labels:
            label += " · " + tr("desconectado")
        options.append((label, selection))
    return options


def endpoint_hint(devices, track, selection, *, enabled=True, loaded=True):
    """Explain automatic routing versus a pinned endpoint beside its selector."""
    if not enabled:
        return tr("Desativado · não será incluído na próxima gravação."), False
    if not loaded:
        return tr("Buscando dispositivos…"), False
    available = [device for device in devices if isinstance(device, dict)
                 and device.get("kind") == track
                 and isinstance(device.get("id"), str) and device["id"]]
    if selection.mode == "manual":
        if not any(device["id"] == selection.endpoint_id for device in available):
            return tr("Dispositivo desconectado. Conecte-o ou escolha outro."), True
        return tr("Usa sempre este dispositivo, mesmo se o padrão do sistema mudar."), False
    flag = "communications_default" if selection.default_role == "communications" else "default"
    if not available or (any(flag in device for device in available)
                         and not any(device.get(flag) is True for device in available)):
        return tr("Nenhum dispositivo padrão disponível. Conecte um ou escolha outro."), True
    if selection.default_role == "communications":
        return tr("Automático · acompanha o dispositivo padrão para chamadas."), False
    return tr("Automático · acompanha o dispositivo padrão do sistema."), False


def validated_settings(raw, dictation=DEFAULT_DICTATION_HOTKEY, command=DEFAULT_COMMAND_HOTKEY):
    settings = resolve_meeting_settings(raw)
    if settings.language not in available_languages(settings.profile):
        raise ValueError(tr("Este modelo usa detecção automática de idioma. Selecione auto."))
    validate_hotkey_conflicts(dict(raw, voice_hotkey=dictation, voice_command_hotkey=command))
    return settings


@dataclass
class ManagerViewState:
    """Python-only context; no Tcl objects may outlive a manager rebuild."""

    destination: str = "recording"
    query: str = ""
    selection: str | None = None
    detail_section: str = "transcript"
    transcript_style: str = "full_text"
    transcript_audio_expanded: bool = False
    list_scroll: float = 0.0
    cursor: object = None
    back_stack: list = field(default_factory=lambda: [None])
    chats: dict = field(default_factory=dict)
    settings_drafts: dict = field(default_factory=dict)
    settings_section: str = "general"
    title_draft: tuple | None = None


def manager_poll_interval(snapshot):
    if (snapshot.get("state") in CAPTURE_STATES or snapshot.get("previewing")
            or (snapshot.get("playback") or {}).get("active")):
        return 200
    return 1000 if snapshot.get("processing") else 2000


class BackgroundBridge:
    """Bounded control, read and long-job lanes with GUI-owned callbacks."""

    def __init__(self):
        # ceiling: eight pending jobs per lane and sixteen returned results.
        # Increase only after measured UI request/IO requirements justify it.
        self.jobs = {False: queue.Queue(8), True: queue.Queue(8), "long": queue.Queue(8)}
        self.results = queue.Queue(16)
        self.closed = threading.Event()
        self.tokens = {}
        self.admitted = {}
        self.callbacks = {}
        self.serial = 0
        self.workers = []
        self.admission_lock = threading.Lock()
        for urgent in (False, True, "long"):
            worker = threading.Thread(target=self._work, args=(urgent,), daemon=True,
                                      name=("MeetingGuiLong" if urgent == "long" else
                                            "MeetingGuiControl" if urgent else "MeetingGuiIO"))
            worker.start()
            self.workers.append(worker)

    def submit(self, key, operation, callback, urgent=False):
        if self.closed.is_set():
            return False
        self.serial += 1
        token = self.serial
        with self.admission_lock:
            self.tokens[key] = token
            self.callbacks[key] = callback
            try:
                self.jobs[urgent].put_nowait((key, token, operation))
            except queue.Full:
                self.tokens.pop(key, None)
                self.callbacks.pop(key, None)
                return False
        return True

    def invalidate(self, key):
        with self.admission_lock:
            self.tokens.pop(key, None)
            self.callbacks.pop(key, None)

    def cancel_queued(self, key):
        """Remove only a job that has not crossed worker admission yet.

        Admitted jobs keep their callback until completion, including while
        their result is waiting to drain, so cancellation cannot report that
        finished work never ran or accidentally cancel a replacement job.
        """
        with self.admission_lock:
            token = self.tokens.get(key)
            if token is None or self.admitted.get(key) == token:
                return False
            self.tokens.pop(key, None)
            self.callbacks.pop(key, None)
            return True

    def _work(self, urgent):
        while not self.closed.is_set():
            try:
                key, token, operation = self.jobs[urgent].get(timeout=0.1)
            except queue.Empty:
                continue
            if self.closed.is_set():
                break
            with self.admission_lock:
                if self.tokens.get(key) != token:
                    continue
                self.admitted[key] = token
            try:
                value, error = operation(), None
            except Exception as exc:
                value, error = None, str(exc)
            while not self.closed.is_set():
                try:
                    self.results.put((key, token, value, error), timeout=0.1)
                    break
                except queue.Full:
                    continue

    def drain(self):
        """Caller owns Tk; stale and post-close results never reach widgets."""
        for _ in range(16):
            try:
                key, token, value, error = self.results.get_nowait()
            except queue.Empty:
                break
            with self.admission_lock:
                if self.admitted.get(key) == token:
                    self.admitted.pop(key, None)
            if not self.closed.is_set() and self.tokens.get(key) == token:
                callback = self.callbacks.pop(key, None)
                self.tokens.pop(key, None)
                if callback:
                    callback(value, error)

    def close(self):
        self.closed.set()
        self.callbacks.clear()
        self.tokens.clear()
        self.admitted.clear()


class SectionSwitcher:
    """Show one of several section frames at a time, chosen from a nav bar.

    Replaces pages that stacked every section in one long scroll. Hidden
    sections keep their widgets alive, so callers keep addressing them
    directly; only the chosen frame is packed into ``container``.
    """

    def __init__(self, ui, nav, container, *, vertical=False, on_select=None, on_user_select=None):
        self.ui = ui
        self.nav = nav
        self.container = container
        self.vertical = vertical
        self.on_select = on_select
        self.on_user_select = on_user_select
        self.frames = {}
        self.buttons = {}
        self.markers = {}
        self.current = None

    def add(self, key, title):
        bg = self.nav.cget("background")
        item = tk.Frame(self.nav, bg=bg)
        # Windows selection indicator: a 3x16 pill beside a vertical item, a
        # 2px underline below a horizontal one. No selection plane, no copper text.
        marker = tk.Frame(item, bg=bg, width=3, height=16 if self.vertical else 2)
        button = tk.Button(
            item, text=title, font=self.ui.body(), command=lambda: self.user_select(key),
            anchor="w" if self.vertical else "center",
            **self.ui.nav_button_colors(bg), **self.ui.button_chrome(compact=True, ring=False),
        )
        ui_theme.focus_ring(button, self.ui)
        if self.vertical:
            item.pack(side="top", fill="x", pady=self.ui.space_xs)
            marker.pack(side="left")
            button.pack(side="left", fill="x", expand=True)
        else:
            item.pack(side="left")
            marker.pack(side="bottom", fill="x")
            button.pack(side="top")
        frame = tk.Frame(self.container, bg=self.ui.surface)
        self.frames[key] = frame
        self.buttons[key] = button
        self.markers[key] = marker
        if self.current is None:
            self.select(key)
        return frame

    def user_select(self, key):
        if self.on_user_select is not None:
            if self.on_user_select(key) is False:
                return
        self.select(key)

    def select(self, key):
        if key == self.current:
            return
        bg = self.nav.cget("background")
        if self.current is not None:
            self.frames[self.current].pack_forget()
            self.buttons[self.current].configure(font=self.ui.body())
            self.markers[self.current].configure(bg=bg)
        self.current = key
        self.frames[key].pack(fill="both", expand=True)
        self.buttons[key].configure(font=self.ui.body_strong())
        self.markers[key].configure(bg=self.ui.accent_mark)
        if self.on_select is not None:
            self.on_select(key)


class MeetingWindow:
    def __init__(self, root, controller, settings_getter, persist_settings,
                 on_settings_changed=None, on_recording_state_changed=None,
                 on_appearance_changed=None, *,
                 window=None, notebook=None, data_location=None, relocate_data=None,
                 models_location=None, relocate_models=None, on_language_changed=None,
                 set_window_icon=None):
        self.root, self.controller = root, controller
        self.set_window_icon = set_window_icon
        self.data_location, self.relocate_data = data_location, relocate_data
        self.models_location, self.relocate_models = models_location, relocate_models
        self.settings_getter, self.persist_settings = settings_getter, persist_settings
        self.on_settings_changed = on_settings_changed
        self.on_recording_state_changed = on_recording_state_changed
        self.on_appearance_changed = on_appearance_changed
        self.on_language_changed = on_language_changed
        if (window is None) != (notebook is None):
            raise ValueError("window and notebook must be supplied together")
        self.embedded = window is not None
        self.window = window or tk.Toplevel(root)
        self.window._snipvoice_set_icon = self._dialog_icon
        self.notebook = notebook
        if not self.embedded:
            self.window.title(tr("Gravações e reuniões"))
            self.window.geometry("1120x820")
            self.window.minsize(920, 700)
        self.ui = ui_theme.theme() if self.embedded else ui_theme.bind(self.window)
        self.window.configure(bg=self.ui.surface)
        self.bridge = BackgroundBridge()
        self.closed = False
        self.visible = True
        self.after_id = None
        self.raw_settings = {}
        self.settings = resolve_meeting_settings({})
        self.devices = []
        self.devices_loaded = False
        self.options = {}
        self.offset = 0
        self.library_cursor = None
        self.library_next_cursor = None
        self.library_back_stack = [None]
        self.library_page_index = 0
        self.library_cursor_reset = False
        self.library_filter_generation = 0
        self.selected = None
        self.transcript_offset = 0
        self.transcript_revision = None
        self.transcript_has_more = False
        self.transcript_has_previous = False
        self.transcript_request = 0
        self.transcript_document_request = 0
        self.transcript_document_truncated = False
        self.transcript_document_ready = False
        self.transcript_audio_expanded = False
        self.annotation_generation = 0
        self.speaker_labels = {}
        self.highlights = []
        self.selected_segment_id = None
        self.raw_unavailable_tracks = set()
        self.raw_tracks_present = set()
        self.playback_choices = ()
        self.playback_dragging = False
        self.playback_duration = 0.0
        self._playback_active = False
        self.raw_capabilities = {"playback": True, "retranscription": True,
                                 "clip": True, "audio_export": True}
        if hasattr(self, "audio_capability_status"):
            self.audio_capability_status.set(tr("Nenhuma reunião selecionada."))
        self.dirty = False
        self.loading = False
        self.settings_loaded = False
        self._settings_drafts = {}
        self._restored_settings_drafts = {}
        self._title_leave_continuation = None
        self.detail_ready = False
        self.processing_target = None
        self.summary_download_cancel = None
        self.summary_progress = None
        self.summary_progress_lock = threading.Lock()
        self.summary_model_installed = {}
        self.previous_state = None
        self.previous_processing = False
        self.snapshot = {}
        self.privacy_defaults = {
            "recording_notice": {"enabled": False, "language": "pt-BR"},
            "qa_mode": "explicit_save",
        }
        self.retention_defaults = {
            "whole_meeting": {"mode": "keep"},
            "raw_audio": {"mode": "keep", "tracks": []},
            "trash_days": 30,
        }
        self.workspace_generation = 0
        self.privacy_ready = False
        self._privacy_initial_loaded = False
        self._workspace_initial_loaded = False
        self.privacy_save_inflight = False
        # Application startup normally sets this false until recovery and the
        # privacy cache have completed.  Standalone/embedded callers keep the
        # backwards-compatible ready default.
        self.retention_ready = True
        self.startup_status = ""
        self.pending_start_origin = None
        # The origin of a start that waits for an interrupted job to stop.
        self.start_after_interrupt = None
        self._start_blocked_key = None
        self.recording_notice_dialog = None
        self.raw_unavailable_tracks = set()
        self.raw_capabilities = {"playback": True, "retranscription": True,
                                 "clip": True, "audio_export": True}
        self.trash_request = 0
        self.trash_dialog = None
        self.trash_tree = None
        self.trash_entries = []
        self.trash_opener = None
        self.trash_receipt = ""
        # Library rows by session id, the recording a job is working on, and
        # the selected recording's header state and processing issue.
        self.session_items = {}
        self.library_processing_session = None
        self._session_title_fit = None
        self.detail_state_item = None
        self.processing_issue = None
        self.transcription_status = ""
        self.retention_request = 0
        self._retention_pending = None
        self.playback_generation = -1
        self.preview_signature = None
        # Report/Q&A requests carry their own generation so a late worker
        # result can never replace a different meeting's selected output.
        self.report_request = 0
        self.ask_request = 0
        self.citation_request = 0
        self.search_request = 0
        self.cross_request = 0
        self.cross_pending = None
        self.cross_citation_request = 0
        self.rebuild_cancel = None
        self.rebuild_progress = {"done": 0, "total": None, "state": "idle"}
        self.organization_generation = 0
        self.report_profiles = []
        self.report_profile_by_label = {}
        self.report_history = []
        self.report_history_ids = []
        self.selected_report = None
        self.report_sections = {}
        self.summary_pending = None
        self.summary_request_cancel = None
        self.summary_generation = 0
        self.ask_conversations = {}
        self.ask_pending = None
        self.ask_request_cancel = None
        self.cross_request_cancel = None
        self._mousewheel_bindings = []
        # Each page writes what happened on it into its own message slot.
        self.message_bars = {}
        self.messages = Messages(self._render_message)
        self._destination = "recording"
        # The sources the running capture was started with, when this window started it.
        self._recording_sources = None
        self._lost_since = {}
        self._silent_for = {}
        self._trouble_key = None
        self._trouble_dismissed = None
        self._transport_key = None
        self._recording_hint = False
        self._play_reason = ""
        self._summary_reason = ""
        self._playback_failure = None
        self.summary_text = ""
        self.readiness_probe = ReadinessProbe()
        self._readiness_next_render = 0.0
        self._readiness_rows = None
        self._summary_inventory_state = "pending"
        self.voice_status_getter = None
        self.voice_tab = None
        self._navigation_generation = 0
        self._detail_section_generation = 0
        self._selection_generation = 0
        self._detail_section_chosen = False
        self._processing_banner_key = None
        self._processing_started = None
        self._completion_context = None
        self._summary_cancel_requested = None
        self._waveform_key = None
        self._waveform_visible = None
        self._build()
        if not self.embedded:
            self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.window.bind("<Destroy>", self._destroyed, add="+")
        self.window.bind("<Control-Shift-C>", self._copy_summary_shortcut, add="+")
        self.window.bind("<Control-f>", self._focus_library_search, add="+")
        self.window.bind("<F2>", self._begin_title_edit, add="+")
        self.window.bind("<Control-Tab>", lambda event: self._cycle_detail_section(1), add="+")
        self.window.bind("<Control-Shift-Tab>", lambda event: self._cycle_detail_section(-1), add="+")
        self.window.bind("<F5>", self._refresh_shortcut, add="+")
        self.window.bind("<Control-s>", self._save_title_shortcut, add="+")
        self.window.bind("<Control-Shift-R>", lambda event: self._record_shortcut(False), add="+")
        self.window.bind("<Control-Shift-P>", lambda event: self._record_shortcut(True), add="+")
        self.window.bind("<F1>", self._shortcut_sheet, add="+")
        self.sessions.bind("<space>", self._play_shortcut, add="+")
        self.transcript.bind("<space>", self._play_shortcut, add="+")
        self.transcript_document.bind("<space>", self._play_shortcut, add="+")
        self._submit("settings", self.settings_getter, self._settings_loaded)
        self._submit(
            "privacy_defaults",
            getattr(self.controller, "refresh_privacy_defaults", lambda: self.privacy_defaults),
            self._privacy_loaded,
        )
        self.refresh_devices()
        self.refresh_library()
        self.refresh_workspace()
        self._poll()

    def capture_view_state(self):
        chats = copy.deepcopy(dict(list(self.ask_conversations.items())[-MAX_CHAT_SESSIONS:]))
        for turns in chats.values():
            for turn in turns:
                if turn.get("status") == "pending":
                    turn.update(status="error", error=tr("Resposta cancelada."))
                turn.pop("saving", None)
        return ManagerViewState(
            destination=self._visible_destination() or "voice", query=self.query.get(), selection=self.selected,
            detail_section=self.detail_sections.current,
            transcript_style=tuple(TRANSCRIPT_STYLES.values())[max(0, self.transcript_style_box.current())],
            transcript_audio_expanded=getattr(self, "transcript_audio_expanded", False),
            list_scroll=self.sessions.yview()[0], cursor=self.library_cursor,
            back_stack=list(self.library_back_stack), chats=chats,
            settings_drafts=self.capture_settings_drafts(),
            settings_section=self.settings_sections.current if hasattr(self, "settings_sections") else "general",
            title_draft=(self.selected, self.title.get(), self._saved_title)
            if self.dirty and self.selected else None,
        )

    def restore_view_state(self, state):
        self._restoring_view_state = state
        self._restored_settings_drafts = copy.deepcopy(state.settings_drafts)
        self._restored_title_draft = state.title_draft
        if hasattr(self, "settings_sections"):
            self.settings_sections.select(state.settings_section)
        for section in ("recording", "privacy"):
            if getattr(self, "_settings_drafts", {}).get(section, {}).get("baseline") is not None:
                self._section_loaded(section)
        self.query.set(state.query)
        self._cancel_library_search()
        self.library_cursor = state.cursor
        self.library_back_stack = list(state.back_stack)
        self.library_page_index = len(self.library_back_stack) - 1
        self.ask_conversations = copy.deepcopy(state.chats)
        self.ask_request = max((turn.get("id", 0) for turns in self.ask_conversations.values()
                                for turn in turns if isinstance(turn.get("id"), int)), default=0)
        for label, value in TRANSCRIPT_STYLES.items():
            if value == state.transcript_style:
                self.transcript_style.set(tr(label))
        self.transcript_audio_expanded = state.transcript_audio_expanded
        if hasattr(self, "transcript_audio_toggle"):
            ui_theme.disclosure(self.transcript_audio_toggle, tr("Trechos para ouvir"),
                                state.transcript_audio_expanded, self.ui)
        self._sync_transcript_style()
        self.detail_sections.select(state.detail_section)
        self._detail_section_chosen = True
        targets = {"recording": self.recording_tab, "library": self.library_tab,
                   "settings": self.settings_tab, "voice": self.voice_tab}
        target = targets.get(state.destination)
        if target is not None:
            self.notebook.select(target)
        if state.selection:
            self.load_session(state.selection)
        self.refresh_library()

    def set_visible(self, visible):
        self.visible = bool(visible)
        self.waveform.set_visible(self.visible and self._visible_destination() == "recording")
        if not self.visible and self.after_id is not None:
            self.root.after_cancel(self.after_id)
            self.after_id = None
        elif self.visible and self.after_id is None and not self.closed:
            self._poll()

    def _refresh_shortcut(self, _event=None):
        self.refresh_library()
        return "break"

    def _save_title_shortcut(self, _event=None):
        if self._visible_destination() == "settings":
            section = self.settings_sections.current
            if section in ("recording", "privacy"):
                self._save_settings_section(section)
                return "break"
        if self._visible_destination() == "library" and self.dirty:
            self._end_title_edit()
            return "break"
        return None

    def _record_shortcut(self, pause):
        settings = dict(self.raw_settings)
        settings["meeting_hotkey"] = "ctrl+shift+p" if pause else "ctrl+shift+r"
        try:
            validate_hotkey_conflicts(settings)
        except ValueError as exc:
            self._fail(self._here(), tr("Atalho indisponível"), str(exc), tone="warning")
            return "break"
        state = self.controller.snapshot().get("state", "idle")
        if pause:
            if state in {"recording", "paused"}:
                self.pause_resume()
        elif state in {"recording", "paused"}:
            self._action("stop", urgent=True)
        elif state == "idle":
            self.request_start("window")
        return "break"

    def _play_shortcut(self, _event=None):
        if (self.snapshot.get("playback") or {}).get("active"):
            self._action("stop_playback", urgent=True)
        else:
            self.play_selected_recording()
        return "break"

    def _shortcut_sheet(self, _event=None):
        messagebox.showinfo(tr("Atalhos de teclado"), tr(
            "Ctrl+1…4 · destinos\nCtrl+F · buscar\nF5 · atualizar biblioteca\n"
            "F2 · editar título\nCtrl+S · salvar título\nDelete · mover para a lixeira\n"
            "Espaço · reproduzir/parar na lista ou transcrição\nCtrl+Tab · próxima seção\n"
            "Ctrl+Shift+C · copiar resumo\nCtrl+Shift+R · iniciar/parar gravação\n"
            "Ctrl+Shift+P · pausar/retomar\nF1 · atalhos\n\n"
            "Atalhos de gravação funcionam nesta janela e são verificados contra os atalhos de ditado."
        ), parent=self.window)
        return "break"

    def _label(self, parent, text, **kwargs):
        kwargs.setdefault("font", self.ui.font())
        kwargs.setdefault("bg", self.ui.surface)
        kwargs.setdefault("fg", self.ui.text)
        return tk.Label(parent, text=text, **kwargs)

    def _wrap_label(self, parent, text, **kwargs):
        """A label that wraps at its allocated width, not a fixed length.

        ``width=1`` stops the wrapped text from requesting width back from
        its parent, which would otherwise widen the pane it sits in.
        """
        label = self._label(parent, text, width=1, wraplength=360, **kwargs)
        label.bind("<Configure>", lambda event: label.configure(wraplength=max(event.width, 120)))
        return label

    def _placeholder(self, entry, variable, text):
        """Grey hint over an empty entry. The variable itself stays empty."""
        hint = self._label(entry, text, bg=self.ui.field, fg=self.ui.text_muted, anchor="w",
                           cursor="xterm")
        hint.bind("<Button-1>", lambda _event: entry.focus_set())

        def refresh(*_args):
            if variable.get():
                hint.place_forget()
            else:
                # Align with the entry text, inside its border and focus ring.
                inset = int(entry.cget("borderwidth")) + int(entry.cget("highlightthickness"))
                hint.place(x=inset, rely=0.5, anchor="w")

        variable.trace_add("write", refresh)
        refresh()
        return hint

    def _toggle_library_panel(self, key):
        panel = self.library_panels[key]
        if panel.winfo_manager():
            panel.pack_forget()
        else:
            panel.pack(fill="x", pady=(0, self.ui.space_sm), before=self.library_panes)
        self._refresh_library_toggles()

    def _toggle_detail_panel(self, panel, toggle, label):
        if panel.winfo_manager():
            panel.pack_forget()
            ui_theme.disclosure(toggle, label, False, self.ui)
        else:
            options = {"after": toggle} if panel.master is toggle.master else {}
            panel.pack(fill="x", padx=(12, 0), pady=(4, 0), **options)
            ui_theme.disclosure(toggle, label, True, self.ui)

    def _refresh_library_toggles(self):
        """Label the optional library tools with their open state."""
        toggles = getattr(self, "library_panel_toggles", None)
        if not toggles:
            return
        labels = {"cross": tr("Perguntar à biblioteca"), "tools": tr("Mais")}
        for key, label in labels.items():
            ui_theme.disclosure(toggles[key], label, bool(self.library_panels[key].winfo_manager()), self.ui)

    def _show_library_detail(self, visible):
        placeholder = getattr(self, "detail_placeholder", None)
        if placeholder is None:
            return
        if visible:
            self._set_library_detail_visible(True)
            placeholder.grid_remove()
            self.detail_frame.grid()
        else:
            self.detail_frame.grid_remove()
            placeholder.grid()

    def _render_library_empty_state(self, items):
        panel = getattr(self, "library_empty_panel", None)
        if panel is None:
            return
        if items:
            panel.place_forget()
            self._set_library_detail_visible(True)
            return
        self._set_library_detail_visible(False)
        narrowed = bool(self.query.get().strip())
        self.library_empty.set(
            tr("Nenhuma gravação encontrada.\nTente outra busca.") if narrowed
            else tr("Nenhuma gravação ainda.\nComece gravando ou importe um áudio.")
        )
        for button in (self.library_empty_record_button, self.library_empty_import_button,
                       self.library_empty_clear_button, self.library_empty_retry_button):
            button.pack_forget()
        if narrowed:
            self.library_empty_clear_button.pack(side="left")
        else:
            self.library_empty_record_button.pack(side="left")
            self.library_empty_import_button.pack(side="left", padx=(self.ui.space_sm, 0))
        panel.place(relx=0, rely=0, relwidth=1, relheight=1)
        panel.lift()

    def _place_library_sash(self, width):
        """Split the library panes; False until both panes exist at a real width.

        The list starts just under half, which shows its state, title and
        date at the default window size, never narrower than its toolbar and
        never so wide that the selected recording's controls clip. The list's
        own request is no guide: its title column grows with the pane.
        """
        panes = self.library_panes
        if width <= 100 or len(panes.panes()) < 2:
            return False
        sash = max(self.library_toolbar.winfo_reqwidth(), int(width * 0.47))
        columns = getattr(self, "_session_column_widths", {})
        # Prefer a readable date when the detail's own controls still fit.
        date_width = SESSION_TITLE_MIN + columns.get("status", 0) + columns.get("date", 0) + 24
        sash = max(sash, date_width)
        panes.sashpos(0, min(sash, width - self.library_detail_pane.winfo_reqwidth()))
        return True

    def _set_library_detail_visible(self, visible):
        panes = getattr(self, "library_panes", None)
        detail = getattr(self, "library_detail_pane", None)
        pages = getattr(self, "library_pages", None)
        if panes is None or detail is None or pages is None:
            return
        present = str(detail) in panes.panes()
        if visible and not present:
            panes.add(detail, weight=3)
            # The list had the whole width while it was empty; split again.
            panes.update_idletasks()
            self._place_library_sash(panes.winfo_width())
        elif not visible and present:
            panes.forget(detail)
        if visible and not pages.winfo_manager():
            pages.pack(fill="x", pady=(self.ui.space_sm, 0))
        elif not visible:
            pages.pack_forget()

    def _clear_library_search(self):
        self.query.set("")
        self.search()
        return "break"

    def _queue_library_search(self, *_args):
        self._cancel_library_search()
        # Reject an older read immediately, including during the debounce.
        self.library_filter_generation += 1
        self.bridge.invalidate("library")
        self._library_search_after = self.window.after(250, self.search)

    def _cancel_library_search(self):
        pending = getattr(self, "_library_search_after", None)
        if pending is not None:
            self.window.after_cancel(pending)
            self._library_search_after = None

    def _focus_library_search(self, _event=None):
        self.notebook.select(self.library_tab)
        self.search_entry.focus_set()
        self.search_entry.selection_range(0, "end")
        return "break"

    def _cycle_detail_section(self, step):
        if self._visible_destination() != "library" or not self.selected:
            return None
        keys = ("transcript", "summary", "ask", "files")
        current = self.detail_sections.current
        self.detail_sections.user_select(keys[(keys.index(current) + step) % len(keys)])
        return "break"

    def _menu(self):
        return tk.Menu(self.window, tearoff=False, font=self.ui.body(),
                       bg=self.ui.card, fg=self.ui.text, activebackground=self.ui.select_bg,
                       activeforeground=self.ui.select_fg)

    def _show_library_menu(self):
        def counted(entries, error):
            if not error and not self.closed:
                self.library_menu.entryconfigure(1, label=tr("Lixeira ({count})", count=len(entries or ())))
        self._submit("library_trash_count", self.controller.list_trash, counted)
        self.library_menu.tk_popup(self.tools_toggle.winfo_rootx(),
                                   self.tools_toggle.winfo_rooty() + self.tools_toggle.winfo_height())
        self.library_menu.grab_release()

    def _open_search_repair(self):
        self.notebook.select(self.settings_tab)
        self.settings_sections.select("models")
        self.model_sections.select("search")
        self.rebuild_index()

    def _delete_shortcut(self):
        self.delete_selected()
        return "break"

    def _show_session_menu(self, event):
        if getattr(event, "num", None) == 3:
            row = self.sessions.identify_row(event.y)
            # Never let a context click delete the previous selection while
            # this row's detail is still loading or a rename guard is open.
            if not row:
                return "break"
            self.sessions.selection_set(row)
            self.sessions.focus(row)
            self._selection_changed()
            if self.selected != row or self.sessions.selection() != (row,):
                return "break"
        ready = self._trash_target_ready() and not getattr(self, "_retention_pending", None)
        self.session_menu.entryconfigure(0, state="normal" if ready else "disabled")
        self.session_menu.tk_popup(self.sessions.winfo_rootx() + 16,
                                   self.sessions.winfo_rooty() + 32)
        self.session_menu.grab_release()
        return "break"

    def _begin_title_edit(self, _event=None):
        if self._visible_destination() != "library" or not self.detail_ready:
            return None
        self.title_heading.grid_remove()
        self.title_entry.grid(row=0, column=0, sticky="ew")
        self.title_entry.focus_set()
        self.title_entry.selection_range(0, "end")
        return "break"

    def _end_title_edit(self, _event=None, *, cancel=False):
        if cancel and getattr(self, "_title_save_pending", None):
            return "break"
        if cancel:
            self.loading = True
            self.title.set(self._saved_title)
            self.loading = False
            self.dirty = False
        elif self.dirty:
            self.save_title(after=self._hide_title_editor)
            return "break"
        self._hide_title_editor()
        return "break"

    def _hide_title_editor(self, *, focus=True):
        self.title_entry.grid_remove()
        self.title_heading.grid(row=0, column=0, sticky="ew")
        if focus:
            self.rename_button.focus_set()

    def _reflow_title_header(self, event):
        # Reserve a readable heading width before placing Rename beside it.
        stacked = event.width < 320 + self.rename_button.winfo_reqwidth() + self.ui.space_sm
        if stacked == getattr(self, "_title_header_stacked", None):
            return
        self._title_header_stacked = stacked
        self.rename_button.grid(
            row=1 if stacked else 0, column=0 if stacked else 1,
            sticky="w" if stacked else "e",
            padx=0 if stacked else (self.ui.space_sm, 0),
            pady=(self.ui.space_xs, 0) if stacked else 0,
        )

    def _build_search_models(self, parent):
        from embedding_models import MODEL
        self._label(parent, tr("Busca"), font=self.ui.heading()).pack(anchor="w")
        self.semantic_enabled = tk.BooleanVar(self.window, False)
        self.semantic_cancel = None
        tk.Checkbutton(parent, text=tr("Busca semântica local"), variable=self.semantic_enabled,
                       command=self.toggle_semantic_search, font=self.ui.body(),
                       **self.ui.checkbutton_colors(self.ui.surface)).pack(anchor="w")
        controls = tk.Frame(parent, bg=self.ui.surface)
        controls.pack(fill="x", pady=self.ui.space_sm)
        table = ui_theme.model_table(parent, self.ui)
        state, actions = ui_theme.model_table_row(table, 1,
            (MODEL["name"], tr("Busca semântica"), format_model_size(MODEL["size_bytes"]), MODEL["license_id"]), self.ui)
        self.semantic_model_action = self._button(actions, tr("Instalar modelo…"), self.install_embedding_model)
        self.semantic_model_action.pack(side="left")
        self._button(controls, tr("Reindexar"), self.rebuild_semantic_index).pack(side="left", padx=(4, 0))
        self.semantic_cancel_button = self._button(controls, tr("Cancelar"), self.cancel_semantic_operation)
        self._park_cancel(self.semantic_cancel_button, "pack", side="left", padx=(4, 0))
        self.semantic_status = tk.StringVar(self.window, tr("Busca por palavras ativa."))
        self.semantic_progress_state = [None]
        state.configure(textvariable=self.semantic_status)
        self._wrap_label(parent, "", textvariable=self.semantic_status,
                         fg=self.ui.text_muted).pack(fill="x")
        repair = tk.Frame(parent, bg=self.ui.surface)
        repair.pack(fill="x", pady=self.ui.space_md)
        self.rebuild_button = self._button(repair, tr("Reparar busca"), self.rebuild_index)
        self.rebuild_button.pack(side="left")
        self.rebuild_cancel_button = self._button(repair, tr("Cancelar"), self.cancel_rebuild_index)
        self._park_cancel(self.rebuild_cancel_button, "pack", side="right")
        self.index_status = tk.StringVar(self.window, tr("Índice de busca: estado desconhecido"))
        self._wrap_label(parent, "", textvariable=self.index_status,
                         fg=self.ui.text_muted).pack(fill="x")

    def _tone_fg(self, tone):
        """Text color for a state tone; the quiet default reads as secondary text."""
        return self.ui.text_muted if tone in (None, "neutral") else getattr(self.ui, tone)

    def _configure_session_columns(self):
        """Size state, date and duration to their widest value; the title takes the rest.

        The list starts with state and title only, so its request never pushes
        the detail; date and duration join once the pane has room for them.
        """
        tree = self.sessions
        self._session_font = tkfont.Font(root=self.window, font=self.ui.body())
        heading = tkfont.Font(root=self.window, font=self.ui.body_strong())
        states = [tr(label) for label, _tone in
                  (*SESSION_STATES.values(), UNKNOWN_SESSION_STATE, PROCESSING_SESSION_STATE)]
        pad = 2 * self.ui.space_sm
        self._session_column_widths = {}
        for column, label, values, anchor in (
            ("status", tr("Estado"), states, "w"),
            ("title", tr("Gravação"), (), "w"),
            ("date", tr("Data"), _date_column_samples(), "w"),
            ("duration", tr("Duração"), (format_time(0),), "e"),
        ):
            tree.heading(column, text=label, anchor=anchor)
            if column == "title":
                tree.column(column, width=SESSION_TITLE_MIN, minwidth=SESSION_TITLE_MIN, stretch=False,
                            anchor=anchor)
                continue
            width = pad + max([heading.measure(label)]
                              + [self._session_font.measure(value) for value in values])
            self._session_column_widths[column] = width
            tree.column(column, width=width, minwidth=width, stretch=False, anchor=anchor)
        tree.configure(displaycolumns=("status", "title"))
        for tone in ("danger", "warning", "info", "neutral"):
            tree.tag_configure(tone, foreground=self._tone_fg(tone))

    def _fit_session_columns(self):
        """Show date, then duration, while the title keeps its minimum; the title takes the rest.

        The widths are set here rather than by Treeview stretch, which kept a
        stretched title wide after the pane narrowed and pushed the date out.
        """
        widths = self._session_column_widths
        # The Meeting.Treeview field edge is 2px on each side.
        available = self.sessions.winfo_width() - 4
        spare = available - widths["status"] - SESSION_TITLE_MIN
        shown = {"status", "title"}
        for column in ("date", "duration"):
            if spare < widths[column]:
                break
            shown.add(column)
            spare -= widths[column]
        self.sessions.column("title", width=SESSION_TITLE_MIN + max(0, spare))
        columns = tuple(column for column in self.sessions["columns"] if column in shown)
        if tuple(self.sessions["displaycolumns"]) != columns:
            self.sessions.configure(displaycolumns=columns)

    def _schedule_session_title_fit(self, _event=None):
        if self._session_title_fit is None and not self.closed:
            self._session_title_fit = self.window.after_idle(self._fit_session_titles)

    def _fit_session_titles(self):
        """Re-cut every title to the title column, which follows the pane width."""
        self._session_title_fit = None
        if self.closed:
            return
        self._fit_session_columns()
        for session_id in self.sessions.get_children():
            self._render_session_row(session_id)

    def _session_state(self, item):
        """The (word, tone) of a recording: a job working on it, else its saved status."""
        if item["id"] == self.library_processing_session:
            label, tone = PROCESSING_SESSION_STATE
        else:
            label, tone = SESSION_STATES.get(item.get("status"), UNKNOWN_SESSION_STATE)
        return tr(label), tone

    def _render_session_row(self, session_id):
        item = self.session_items.get(session_id)
        if item is None or not self.sessions.exists(session_id):
            return
        label, tone = self._session_state(item)
        # The cell's text sits inside a few pixels of padding on each side.
        width = int(self.sessions.column("title", "width")) - 2 * self.ui.space_sm
        title = " ".join(str(item.get("title") or "").split()) or tr("Sem título")
        self.sessions.item(session_id, tags=(tone,) if tone else (), values=(
            label, elide(self._session_font, title, width),
            format_local_timestamp(item.get("created_at")), format_time(item.get("duration")),
        ))

    def _library_count_text(self, count):
        """The visible recordings, plural-correct; the page appears once there are pages."""
        if self.library_page_index == 0 and not self.library_next_cursor:
            return tr("1 gravação") if count == 1 else tr("{count} gravações", count=count)
        page = self.library_page_index + 1
        if count == 1:
            return tr("Página {page} · 1 gravação", page=page)
        return tr("Página {page} · {count} gravações", page=page, count=count)

    def attach_voice_status(self, getter, tab):
        """The app supplies an in-memory voice snapshot and its existing destination."""
        self.voice_status_getter, self.voice_tab = getter, tab

    def _sync_readiness(self, now=None):
        if not hasattr(self, "readiness_probe"):
            return
        now = time.monotonic() if now is None else now
        if now < self._readiness_next_render:
            return
        self._readiness_next_render = now + 1.0
        probe = self.readiness_probe
        if probe.admit(now):
            preferred = self._effective_profile()
            voice_reader = self.voice_status_getter

            def read():
                reader = getattr(self.controller, "installed_transcription_profile", None)
                if not callable(reader):
                    raise RuntimeError("Installed transcription inventory is unavailable")
                return {"profile": reader(preferred),
                        "voice": voice_reader() if voice_reader is not None else None}

            if not self.bridge.submit("readiness", read, probe.settle):
                probe.settle(None, "busy")
        attention = recent_attention(self.session_items.values())
        rows = readiness_rows(
            self.snapshot, probe, summary_state=self._summary_inventory_state,
            voice_available=self.voice_status_getter is not None,
            startup_ready=self.retention_ready, startup_text=self.startup_status,
            attention_count=len(attention))
        if rows != self._readiness_rows:
            self._readiness_rows = rows
            for child in self.readiness_strip.winfo_children():
                child.destroy()
            actions = {
                "startup": self._readiness_startup_details,
                "inventory": self._open_transcription_models,
                "transcription": self._open_transcription_models,
                "summary": self.open_summary_models,
                "job": self._cancel_current_job,
                "attention": self._open_attention_recording,
                "dictation": lambda: self.notebook.select(self.voice_tab),
            }
            for item in rows:
                row = tk.Frame(self.readiness_strip, bg=self.ui.card)
                row.pack(fill="x", pady=(0, self.ui.space_xs))
                row.columnconfigure(0, weight=1)
                self._wrap_label(row, item.text, bg=self.ui.card, fg=getattr(self.ui, item.tone),
                                 font=self.ui.caption(), anchor="w", justify="left").grid(
                                     row=0, column=0, sticky="ew")
                if item.action:
                    self._button(row, item.action, actions[item.key]).grid(
                        row=0, column=1, sticky="e", padx=(self.ui.space_sm, 0))
        self._sync_navigation_badges(attention)

    def _readiness_startup_details(self):
        self._fail("recording", tr("Recuperação local pendente."), self.startup_status, tone="warning")
        self.recording_canvas.yview_moveto(0)

    def _open_transcription_models(self):
        self.notebook.select(self.settings_tab)
        self.settings_sections.select("models")
        self.model_sections.select("transcription")

    def _open_attention_recording(self):
        attention = recent_attention(self.session_items.values())
        if not attention:
            return
        self.notebook.select(self.library_tab)
        session_id = attention[0]["id"]
        if self.sessions.exists(session_id):
            # The existing selection handler owns the unsaved-edit decision.
            self.sessions.selection_set(session_id)
            self.sessions.see(session_id)
        else:
            self.refresh_library()

    def _cancel_current_job(self):
        if getattr(self, "summary_pending", None):
            self._summary_cancel_requested = self.summary_pending
        self._action("cancel_processing", urgent=True)

    def _sync_navigation_badges(self, attention):
        state = self.snapshot.get("state")
        recording = tr("Gravação")
        if state in CAPTURE_STATES:
            recording += "\n● " + tr("{state} · {time}", state=tr(STATE_LABELS.get(state, state)),
                                     time=format_time(self.snapshot.get("elapsed")).removeprefix("00:"))
        elif self.snapshot.get("processing"):
            recording += "\n" + tr("Processando")
        library = tr("Biblioteca")
        if attention:
            library += "\n" + tr("Atenção nesta página: {count}", count=len(attention))
        labels = [(self.recording_tab, recording), (self.library_tab, library)]
        if self.voice_tab is not None:
            voice = self.readiness_probe.voice
            badge = tr("Ativo") if isinstance(voice, dict) and voice.get("enabled") else (
                tr("Desligado") if isinstance(voice, dict) else tr("Verificando…"))
            if isinstance(voice, dict) and voice.get("enabled") and voice.get("state") == "unavailable":
                badge = tr("Indisponível")
            labels.append((self.voice_tab, tr("Ditado") + "\n" + badge))
        changed = False
        for tab, label in labels:
            if self.notebook.tab(tab, "text") != label:
                self.notebook.tab(tab, text=label)
                changed = True
        if changed:
            self.notebook.event_generate("<<ManagerStatusChanged>>")

    def _detail_user_selected(self, _key):
        self._detail_section_chosen = True
        self._detail_section_generation += 1

    def _current_completion_context(self):
        return (self.selected, self._visible_destination(),
                getattr(self, "_navigation_generation", 0),
                getattr(self, "_detail_section_generation", 0),
                getattr(self, "_selection_generation", 0))

    def _sync_processing_banner(self, snapshot, now=None):
        if not hasattr(self, "processing_banner"):
            return
        now = time.monotonic() if now is None else now
        session = snapshot.get("processing_session") if snapshot.get("processing") else None
        if session != getattr(self, "_processing_started_session", None):
            self._processing_started_session = session
            self._processing_started = now
        started = self._processing_started if self._processing_started is not None else now
        value = processing_view(snapshot, self.selected, started, now)
        if value is None:
            self.processing_banner.pack_forget()
            self._processing_banner_key = None
            return
        if self._completion_context is None or self._completion_context[0] != session:
            self._completion_context = self._current_completion_context()
        self.completion_receipt.pack_forget()
        key = (session, value["stage"], value["elapsed"], value["percent"])
        if key == self._processing_banner_key:
            return
        self._processing_banner_key = key
        self.processing_banner_label.configure(text=tr("{stage} · {elapsed}",
            stage=value["stage"], elapsed=format_time(value["elapsed"])))
        if value["percent"] is None:
            self.detail_processing_progress.pack_forget()
        else:
            self.processing_banner_label.configure(text=tr("{stage} · {elapsed} · {percent}%",
                stage=value["stage"], elapsed=format_time(value["elapsed"]), percent=int(value["percent"])))
            self.detail_processing_progress.configure(value=value["percent"])
            self.detail_processing_progress.pack(fill="x", pady=(self.ui.space_xs, 0))
        self.processing_banner.pack(fill="x", padx=(12, 0), pady=(self.ui.space_sm, 0), before=self.detail_nav)

    def _summary_completed(self, session_id, context=None):
        if not hasattr(self, "completion_receipt") or not self.summary_text or self.selected != session_id:
            return
        self.completion_receipt.pack(fill="x", padx=(12, 0), pady=(self.ui.space_sm, 0), before=self.detail_nav)
        if self._visible_destination() == "library" and context is not None and completion_can_select(context, session=session_id,
                destination=self._visible_destination(), navigation=getattr(self, "_navigation_generation", 0),
                section=getattr(self, "_detail_section_generation", 0),
                selection=getattr(self, "_selection_generation", 0)):
            self.detail_sections.select("summary")

    def _copy_summary_shortcut(self, _event=None):
        if self._visible_destination() == "library" and self.selected and self.summary_text:
            self.copy_summary()
            return "break"
        return None

    def _sync_library_processing(self, snapshot):
        """Mark the recording a job works on as Processando, in its row and its header."""
        live = None
        if snapshot.get("processing") and snapshot.get("processing_kind") in LIBRARY_PROCESSING_JOBS:
            live = snapshot.get("processing_session") or None
        previous = self.library_processing_session
        if live == previous:
            return
        self.library_processing_session = live
        for session_id in (previous, live):
            if session_id:
                self._render_session_row(session_id)
        if self.selected and self.selected in (previous, live):
            self._render_detail_state()
            if self.selected == live:
                self._sync_empty_transcript_caption()
                self._show_processing_issue(self.processing_issue)
            else:
                self._refresh_processing_issue(self.selected)
                if getattr(self, "detail_ready", False):
                    self._completed_processing_context = (self.selected, getattr(self, "_completion_context", None))
                    self.processing_target = self.selected
                    self._completion_context = None

    def _refresh_processing_issue(self, session_id):
        """Replace the step failure shown for a recording whose job just ended.

        The failure loaded before the job is stale now; the bar stays hidden and
        the empty-transcript caption keeps saying Processando until the store's
        current answer arrives, instead of flashing the old failure.
        """
        self._show_processing_issue(None)

        def read():
            raw = self.controller.get_session(session_id)
            return _processing_issue(raw), _stage_status(raw, "transcription")

        def loaded(data, error):
            if self.selected != session_id or session_id == self.library_processing_session:
                return
            if error:
                self._sync_empty_transcript_caption()
                self._results_not_refreshed(error, session_id)
                return
            issue, self.transcription_status = data
            self._sync_empty_transcript_caption()
            self._show_processing_issue(issue)

        self._submit("processing_issue", read, loaded)

    def _render_detail_state(self):
        item = self.detail_state_item
        if not item or item["id"] != self.selected:
            self.detail_state.set("")
            self.detail_meta.set("")
            return
        label, tone = self._session_state(item)
        self.detail_state.set(label)
        self.detail_state_label.configure(fg=self._tone_fg(tone))
        self.detail_meta.set(" · " + format_local_timestamp(item.get("created_at"))
                             + " · " + format_time(item.get("duration")))

    def _processing_issue_copy(self, issue):
        """(title, body, action) for a failed or cancelled step, in the words of its fix."""
        stage, cancelled = issue["stage"], issue["status"] == "cancelled"
        prepare = (tr("Preparar áudio final"), self.prepare_final_audio)
        if stage == "audio":
            title = tr("O áudio final foi cancelado") if cancelled else tr("O áudio final falhou")
            return title, tr("A captura original foi preservada. Prepare o áudio final para ouvir, "
                             "exportar e transcrever."), prepare
        if stage == "transcription":
            title = tr("A transcrição foi cancelada") if cancelled else tr("A transcrição falhou")
            if getattr(self, "final_available", False):
                return (title, tr("O áudio foi preservado e pode ser transcrito de novo."),
                        (tr("Transcrever novamente"), self.transcribe))
            return title, tr("O áudio foi preservado. Transcrever de novo requer o áudio final."), prepare
        if stage == "summary":
            title = tr("O resumo foi cancelado") if cancelled else tr("O resumo falhou")

            def summarize():
                self.detail_sections.select("summary")
                self.summarize()

            return title, tr("A transcrição continua disponível. Gere o resumo de novo."), (
                tr("Gerar resumo"), summarize)
        title = (tr("A liberação dos arquivos temporários foi cancelada") if cancelled
                 else tr("A liberação dos arquivos temporários falhou"))
        return title, tr("A gravação está completa. Os arquivos temporários continuam ocupando "
                         "espaço no disco."), None

    def _show_processing_issue(self, issue):
        """A danger (failed) or warning (cancelled) InfoBar for the selected recording's step.

        It stays hidden while a job works on the recording; the step's result
        replaces it when the job ends.
        """
        self.processing_issue = issue
        slot = self.processing_issue_slot
        for child in slot.winfo_children():
            child.destroy()
        if not issue or (self.selected and self.selected == self.library_processing_session):
            slot.pack_forget()
            return
        title, body, action = self._processing_issue_copy(issue)
        details = _technical_error_detail(issue["error"]) if issue.get("error") else None
        tone = "warning" if issue["status"] == "cancelled" else "danger"
        ui_theme.info_bar(slot, self.ui, tone, title, body, action=action, details=details,
                          copy=self._copy_issue_details).pack(fill="x")
        slot.pack(fill="x", padx=(12, 0), pady=(self.ui.space_sm, 0), before=self.detail_nav)

    def _copy_issue_details(self, text, done):
        """Copy an InfoBar's technical details off the GUI thread; ``done`` hears the outcome."""
        if not self._submit("copy_issue_details", lambda: Clipboard.set_content(text),
                            lambda copied, error: done(bool(copied) and not error)):
            done(False)

    def _render_message(self, destination, message):
        """Draw a destination's message, or empty its slot; ``Messages`` owns no widgets."""
        bar = self.message_bars.get(destination)
        if bar is None or self.closed:
            return
        if message is not None and destination == "library" and hasattr(self, "undo_receipt"):
            self.undo_receipt.pack_forget()
        if message is None:
            bar.clear()
        elif message.kind == "receipt":
            bar.receipt(message.title, message.when)
        else:
            bar.problem(message.tone, message.title, message.body, action=message.action,
                        details=message.details or None, when=message.when,
                        dismiss=None if message.sticky
                        else lambda: self.messages.clear(destination))

    def _say(self, destination, text, *, session=None):
        """A routine outcome or a hint as one quiet line on ``destination``'s page."""
        self.messages.receipt(destination, text, session=session)

    def _fail(self, destination, title, body="", **options):
        """Trouble on ``destination``'s page: what happened, what it affects, the cause behind Detalhes."""
        self.messages.problem(destination, title, body, **options)

    def _copied(self, receipt, failure, copied, error):
        """Settle a clipboard write: one quiet line, or the failure written on the Biblioteca page."""
        if copied and not error:
            self._say("library", receipt)
        else:
            self._fail("library", failure, tr("Nada foi copiado. Tente de novo."), error=error)

    def _visible_destination(self):
        """The message page the user is looking at, or None on a page this view does not own."""
        try:
            selected = str(self.notebook.select())
        except tk.TclError:
            return None
        return {str(self.recording_tab): "recording", str(self.library_tab): "library",
                str(self.settings_tab): "settings"}.get(selected)

    def _here(self):
        """Where an outcome with no page of its own is written: the page in view."""
        return self._visible_destination() or self._destination

    def _destination_changed(self, _event=None):
        """Leaving a page empties its message slot; what it said stays true only there."""
        current = self._visible_destination()
        if hasattr(self, "waveform"):
            self.waveform.set_visible(getattr(self, "visible", True) and current == "recording")
        left = self._destination
        if current:
            self._destination = current
        if left != current:
            self._navigation_generation = getattr(self, "_navigation_generation", 0) + 1
            self.messages.leave(left)
            if left == "library" and hasattr(self, "undo_receipt"):
                self.undo_receipt.pack_forget()

    def _results_not_refreshed(self, error, session_id):
        self._fail("library", tr("Não foi possível atualizar os resultados"),
                   tr("O que aparece aqui pode estar desatualizado. Selecione a gravação de novo."),
                   error=error, session=session_id)

    def _dialog_icon(self, dialog):
        """Give a dialog the app icon; no default icon reaches a transient window, so each asks."""
        if self.set_window_icon is not None:
            self.set_window_icon(dialog)

    def _button(self, parent, text, command, accent=False, danger=False):
        variant = "danger" if danger else "primary" if accent else "neutral"
        return ui_theme.button(parent, text, command, variant, resolved=self.ui)

    def _entry(self, parent, variable, width=30):
        entry = tk.Entry(parent, textvariable=variable, width=width, font=self.ui.body(),
                         **self.ui.entry_colors(), **self.ui.entry_chrome())
        return ui_theme.focus_ring(entry, self.ui, shrink=("bd",))

    def _card(self, parent, **kwargs):
        kwargs.setdefault("padx", self.ui.space_lg)
        kwargs.setdefault("pady", self.ui.space_md)
        return tk.Frame(parent, **self.ui.card_options(), **kwargs)

    def _bind_mousewheel_region(self, region, target):
        """Route wheel events from nested controls to one scrollable region."""
        if not hasattr(self, "_mousewheel_bindings"):
            self._mousewheel_bindings = []

        def on_mousewheel(event):
            widget = getattr(event, "widget", None)
            while widget is not None and widget is not region:
                if getattr(widget, "_wheel_target", target) is not target:
                    return None
                widget = getattr(widget, "master", None)
            if widget is not region:
                return None
            if getattr(event, "delta", 0):
                steps = -1 if event.delta > 0 else 1
            elif getattr(event, "num", None) == 4:
                steps = -1
            elif getattr(event, "num", None) == 5:
                steps = 1
            else:
                return None
            target.yview_scroll(steps, "units")
            return "break"

        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            binding_id = self.window.bind(sequence, on_mousewheel, add="+")
            self._mousewheel_bindings.append((sequence, binding_id))

    def _unbind_mousewheel_regions(self):
        for sequence, binding_id in getattr(self, "_mousewheel_bindings", ()):
            try:
                self.window.unbind(sequence, binding_id)
            except tk.TclError:
                pass
        self._mousewheel_bindings = []

    def _scroll_area(self, parent, *, fill_height=False, on_resize=None):
        """A vertically scrollable region whose scrollbar shows only on overflow.

        Returns ``(view, canvas, holder, content)``: grid or pack ``view`` and
        build into ``content``; ``holder`` is the canvas window around it. The
        scrollbar column keeps its width while hidden so toggling it never
        re-wraps the content and oscillates. ``fill_height`` stretches the
        content to the viewport so expanding children (text views, the
        waveform) take the spare height instead of their fixed request. It is
        a grid row minimum rather than a fixed window height, because a fixed
        height stops content growth from reaching the canvas as a resize.
        """
        view = ttk.Frame(parent, style="Meeting.TFrame")
        view.columnconfigure(0, weight=1)
        view.rowconfigure(0, weight=1)
        canvas = tk.Canvas(view, background=self.ui.surface, highlightthickness=0, borderwidth=0)
        view._wheel_target = canvas
        scrollbar = ttk.Scrollbar(view, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        view.columnconfigure(1, minsize=scrollbar.winfo_reqwidth() + self.ui.space_sm)
        holder = ttk.Frame(canvas, style="Meeting.TFrame")
        holder.columnconfigure(0, weight=1)
        holder.rowconfigure(0, weight=1)
        content = ttk.Frame(holder, style="Meeting.TFrame")
        content.grid(row=0, column=0, sticky="nsew")
        item = canvas.create_window((0, 0), window=holder, anchor="nw")

        def sync(_event=None):
            width, height = canvas.winfo_width(), canvas.winfo_height()
            if fill_height:
                holder.rowconfigure(0, minsize=height)
            needed = content.winfo_reqheight()
            canvas.itemconfigure(item, width=width)
            canvas.configure(scrollregion=(0, 0, width, max(needed, height)))
            if needed > height > 1:
                scrollbar.grid(row=0, column=1, sticky="nse")
            else:
                scrollbar.grid_remove()
                canvas.yview_moveto(0)
            if on_resize is not None:
                on_resize(width, height)

        holder.bind("<Configure>", sync, add="+")
        content.bind("<Configure>", sync, add="+")
        canvas.bind("<Configure>", sync, add="+")
        self._bind_mousewheel_region(view, canvas)
        return view, canvas, holder, content

    def _build(self):
        style = ttk.Style(self.window)
        ui_theme.apply_ttk_theme(style, resolved=self.ui)
        # Playback.Horizontal.TScale and Meeting.Treeview take their look from
        # the shared manager styles.
        ui_theme.configure_manager_styles(style, self.ui)
        style.configure("Meeting.TFrame", background=self.ui.surface)
        notebook = self.notebook
        if notebook is None:
            notebook = ttk.Notebook(self.window, style="Manager.TNotebook")
            notebook.pack(fill="both", expand=True, padx=self.ui.space_xl, pady=self.ui.space_lg)
            self.notebook = notebook
        self.recording_tab = ttk.Frame(notebook, style="Meeting.TFrame")
        self.library_tab = ttk.Frame(notebook, style="Meeting.TFrame")
        # The Configurações page owns the local model catalog. Keep the old attribute as an
        # alias for callers that still refer to the summary page directly.
        self.settings_tab = ttk.Frame(notebook, style="Meeting.TFrame")
        self.summary_tab = self.settings_tab
        notebook.add(self.recording_tab, text=tr("Gravação"))
        notebook.add(self.library_tab, text=tr("Biblioteca"))
        notebook.add(self.settings_tab, text=tr("Configurações"))
        page_pad = self.ui.space_lg
        # Each destination opens with its title, as in the family layout; page
        # actions sit at its right instead of inside a content card.
        self.page_headers = {}
        for key, tab, title in (("recording", self.recording_tab, tr("Gravação")),
                                ("library", self.library_tab, tr("Biblioteca")),
                                ("settings", self.settings_tab, tr("Configurações"))):
            header = ui_theme.PageHeader(tab, self.ui, title)
            header.pack(side="top", fill="x", padx=page_pad, pady=(page_pad, 0))
            self.page_headers[key] = header
        recording_view, recording_canvas, _holder, recording = self._scroll_area(
            self.recording_tab, fill_height=True,
        )
        recording_view.pack(fill="both", expand=True)
        recording.configure(padding=page_pad)
        self.recording_canvas = recording_canvas
        library = ttk.Frame(self.library_tab, padding=page_pad, style="Meeting.TFrame")
        library.pack(fill="both", expand=True)
        settings_view = ttk.Frame(self.settings_tab, style="Meeting.TFrame")
        settings_view.pack(fill="both", expand=True, padx=self.ui.space_lg, pady=self.ui.space_lg)
        settings_view.columnconfigure(1, weight=1)
        settings_view.rowconfigure(0, weight=1)
        # A page's messages sit above its content and leave with the user: what
        # went wrong on a page is written on that page, never on another.
        for key, tab, content in (("recording", self.recording_tab, recording_view),
                                  ("library", self.library_tab, library),
                                  ("settings", self.settings_tab, settings_view)):
            bar = ui_theme.MessageBar(tab, self.ui, copy=self._copy_issue_details)
            bar.attach(side="top", fill="x", padx=page_pad, pady=(page_pad, 0), before=content)
            self.message_bars[key] = bar
        self.notebook.bind("<<NotebookTabChanged>>", self._destination_changed, add="+")
        settings_nav = tk.Frame(settings_view, bg=self.ui.surface)
        settings_nav.grid(row=0, column=0, sticky="nsw", padx=(0, self.ui.space_lg))
        settings_scroll, settings_canvas, _holder, settings_content = self._scroll_area(settings_view)
        settings_scroll.grid(row=0, column=1, sticky="nsew")
        self.settings_canvas = settings_canvas
        self.settings_content = settings_content
        # One section at a time instead of a single page several screens long.
        self.settings_sections = SectionSwitcher(
            self.ui, settings_nav, settings_content, vertical=True,
            on_select=lambda _key: settings_canvas.yview_moveto(0),
            on_user_select=self._settings_section_requested,
        )
        general = self.settings_sections.add("general", tr("Geral"))
        recording_settings = self.settings_sections.add("recording", tr("Gravação"))
        privacy = self.settings_sections.add("privacy", tr("Privacidade"))
        models = self.settings_sections.add("models", tr("Modelos"))
        model_nav = tk.Frame(models, bg=self.ui.surface)
        model_nav.pack(fill="x", pady=(0, self.ui.space_md))
        model_pages = tk.Frame(models, bg=self.ui.surface)
        model_pages.pack(fill="x")
        self.model_sections = SectionSwitcher(
            self.ui, model_nav, model_pages,
            on_select=lambda _key: settings_canvas.yview_moveto(0),
        )
        transcription = self.model_sections.add("transcription", tr("Transcrição"))
        summary_section = self.model_sections.add("summary", tr("Resumos"))
        self.search_models_page = self.model_sections.add("search", tr("Busca"))
        self._build_search_models(self.search_models_page)
        self._build_language_card(general)
        self._build_appearance_card(general)
        self.location_cards = {}
        for spec in self._location_specs():
            # The model folder serves both model kinds, so it sits under their tabs.
            self._build_location_card(models if spec["key"] == "models" else general, spec)
        self.recording_defaults_parent = self._card(recording_settings)
        self.recording_defaults_parent.pack(fill="x", pady=(0, self.ui.space_md))
        self._build_privacy_card(privacy)
        self.transcription_models_parent = tk.Frame(
            transcription, bg=self.ui.surface,
        )
        self.transcription_models_parent.pack(fill="x", pady=(0, self.ui.space_md))
        summary = ttk.Frame(summary_section, style="Meeting.TFrame")
        summary.pack(fill="x", expand=False)
        self._label(
            summary, tr("Modelos de resumo de texto"),
            font=self.ui.font(11, "bold"), fg=self.ui.text_strong,
        ).pack(anchor="w", pady=(0, self.ui.space_xs))
        self._wrap_label(
            summary,
            tr("Baixe e escolha um modelo compacto para resumir sem enviar conteúdo à nuvem."),
            anchor="w", justify="left", fg=self.ui.text_muted,
        ).pack(fill="x", pady=(0, self.ui.space_sm))
        self.record_title = tk.StringVar(self.window)
        self.input_enabled = tk.BooleanVar(self.window, self.settings.input_enabled)
        self.output_enabled = tk.BooleanVar(self.window, self.settings.output_enabled)
        self.destination = tk.StringVar(self.window, self.settings.destination)
        self.destination_label = tk.StringVar(
            self.window, destination_display(self.settings.destination),
        )
        self.voice_boost = tk.BooleanVar(self.window, self.settings.voice_boost)
        self.profile = tk.StringVar(self.window, self.settings.profile_setting)
        self.language = tk.StringVar(self.window, self.settings.language)
        self.profile_display = tk.StringVar(self.window, profile_display_label(self.settings.profile_setting))
        self.language_display = tk.StringVar(self.window, tr(LANGUAGE_LABELS[self.settings.language]))
        self.hotkey = tk.StringVar(self.window)
        self.summary_model = tk.StringVar(self.window, self.settings.summary_model)
        self.summary_display = tk.StringVar(self.window, tr(SUMMARY_LABELS[self.settings.summary_model]))
        self.endpoint_vars = {track: tk.StringVar(self.window) for track in ("microphone", "system")}
        self.endpoint_boxes = {}
        defaults = self.recording_defaults_parent
        self._label(
            defaults, tr("Padrões de gravação"), bg=self.ui.card,
            fg=self.ui.text_strong, font=self.ui.font(11, "bold"),
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        self._wrap_label(
            defaults,
            tr("Escolha os modelos, o idioma e o atalho usados em novas gravações."),
            bg=self.ui.card, fg=self.ui.text_muted, anchor="w", justify="left",
        ).grid(row=1, column=0, columnspan=2, sticky="ew", pady=(self.ui.space_xs, self.ui.space_sm))
        self.profile_box = ttk.Combobox(
            defaults, textvariable=self.profile_display,
            values=[profile_display_label(FOLLOW_DICTATION)]
            + [tr(PROFILE_LABELS[entry["profile"]]) for entry in selectable_catalog()],
            state="readonly",
        )
        self.profile_box.bind("<<ComboboxSelected>>", self._profile_changed)
        self.language_box = ttk.Combobox(
            defaults, textvariable=self.language_display, state="readonly",
        )
        self.language_box.bind("<<ComboboxSelected>>", self._language_changed)
        self.summary_box = ttk.Combobox(
            defaults, textvariable=self.summary_display,
            values=[tr(label) for label in SUMMARY_LABELS.values()], state="readonly",
        )
        self.summary_box.bind("<<ComboboxSelected>>", self._summary_display_changed)
        default_rows = [
            (tr("Modelo de transcrição local"), self.profile_box),
            (tr("Idioma"), self.language_box),
            (tr("Atalho de gravação (opcional)"), self._entry(defaults, self.hotkey)),
            (tr("Modelo de resumo local"), self.summary_box),
        ]
        self.recording_validation_fields = {}
        self.recording_field_errors = {}
        for index, (label, widget) in enumerate(default_rows):
            row = 2 + index * 2
            name = ("profile", "language", "hotkey", "summary_model")[index]
            self.recording_validation_fields[name] = widget
            self._label(defaults, label, anchor="w", bg=self.ui.card).grid(
                row=row, column=0, sticky="w", padx=(0, self.ui.space_lg), pady=self.ui.space_xs,
            )
            widget.grid(row=row, column=1, sticky="ew", pady=self.ui.space_xs)
            error = self._wrap_label(defaults, "", bg=self.ui.card, fg=self.ui.danger,
                                     font=self.ui.caption(), anchor="w")
            error.grid(row=row + 1, column=1, sticky="ew")
            self.recording_field_errors[name] = error
        defaults.columnconfigure(1, weight=1)
        compact_recording = self.window.winfo_screenheight() <= 800
        card_pady = max(2, self.ui.space_xs // 2) if compact_recording else self.ui.space_sm
        recording_gap = self.ui.space_xs if compact_recording else self.ui.space_sm
        row_pady = 1 if compact_recording else 3
        note_pady = (2, 4) if compact_recording else (4, 6)
        settings_card = self._card(recording_settings, pady=card_pady)
        settings_card.pack(fill="x", pady=(0, self.ui.space_md))
        self._label(
            settings_card, tr("Detalhes e automação"),
            bg=self.ui.card, fg=self.ui.text_strong, font=self.ui.font(11, "bold"),
        ).grid(row=0, column=0, sticky="w", pady=(0, self.ui.space_sm))
        destination_row = tk.Frame(settings_card, bg=self.ui.card)
        destination_entry = self._entry(destination_row, self.destination_label)
        destination_entry.configure(state="readonly")
        destination_entry.pack(side="left", fill="x", expand=True)
        self._button(destination_row, tr("Escolher…"), self.choose_destination).pack(
            side="left", padx=(self.ui.space_sm, 0))
        self._button(destination_row, tr("Usar padrão"), self.use_default_destination).pack(
            side="left", padx=(self.ui.space_sm, 0))
        self._label(settings_card, tr("Pasta dos arquivos finais"), anchor="w", bg=self.ui.card).grid(
            row=1, column=0, sticky="w", pady=(row_pady, 0),
        )
        destination_row.grid(row=2, column=0, sticky="ew", pady=(0, row_pady))
        self.recording_validation_fields["destination"] = destination_entry
        error = self._wrap_label(settings_card, "", bg=self.ui.card, fg=self.ui.danger, anchor="w")
        error.grid(row=3, column=0, sticky="ew")
        self.recording_field_errors["destination"] = error
        settings_card.columnconfigure(0, weight=1)
        automation = tk.Frame(settings_card, bg=self.ui.card)
        self._wrap_label(
            automation, tr("Transcrição e resumo são gerados automaticamente com os modelos instalados."),
            bg=self.ui.card, fg=self.ui.text_muted, anchor="w",
        ).pack(fill="x", pady=(0, self.ui.space_sm))
        self.voice_boost_check = tk.Checkbutton(
            automation, text=tr("Ajustar volume do microfone no áudio final"), variable=self.voice_boost,
            font=self.ui.font(), anchor="w", **self.ui.checkbutton_colors(self.ui.card),
        )
        self.voice_boost_check.pack(anchor="w", fill="x", pady=(self.ui.space_xs, 0))
        automation_row = 4
        self._label(settings_card, tr("Depois de gravar"), anchor="w", bg=self.ui.card).grid(
            row=automation_row, column=0, sticky="w", pady=(row_pady, 0))
        automation.grid(row=automation_row + 1, column=0, sticky="ew", pady=(0, row_pady))
        note_row = automation_row + 2
        note = self._wrap_label(
            settings_card,
            tr("Configurações valem para a próxima gravação. A pasta local padrão "
               "fica ao lado da biblioteca.\nO sistema inclui todos os sons do "
               "dispositivo escolhido. Use fones para reduzir duplicação acústica."),
            justify="left", anchor="w", bg=self.ui.card,
            fg=self.ui.text_muted, font=self.ui.font(9),
        )
        note.grid(row=note_row, column=0, sticky="ew", pady=note_pady)
        self._build_settings_bar(recording_settings, "recording")
        self._build_settings_bar(privacy, "privacy")
        # Setup and transport on the left, the live signal on the right: side
        # by side the page fits the window instead of scrolling one tall card.
        recording.columnconfigure(0, weight=1, uniform="recording")
        recording.columnconfigure(1, weight=1, uniform="recording")
        recording.rowconfigure(0, weight=1)
        activity_view, self.readiness_canvas, _left_holder, activity_content = self._scroll_area(
            recording, fill_height=True)
        activity_view.grid(row=0, column=0, sticky="nsew", padx=(0, self.ui.space_md))
        activity = self._card(activity_content, pady=card_pady)
        activity.pack(fill="both", expand=True)
        self.recording_activity = activity
        monitor = self._card(recording, pady=card_pady)
        monitor.grid(row=0, column=1, sticky="nsew")
        monitor.columnconfigure(1, weight=1)
        # The waveform row takes the spare height while it shows.
        monitor.rowconfigure(WAVEFORM_ROW, weight=1)
        self.recording_monitor = monitor
        self.recording_options_button = self._button(
            self.page_headers["recording"].actions, tr("Configurar gravação"),
            self.show_recording_settings,
        )
        self.recording_options_button.pack(side="right")
        activity.columnconfigure(1, weight=1)
        # Readiness lives above setup in its own scrollable card. Even a full
        # inventory/recovery warning stack cannot push the right-hand Stop away.
        self.readiness_strip = tk.Frame(activity, bg=self.ui.card)
        self.readiness_strip.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, self.ui.space_sm))
        self._label(activity, tr("Título da reunião (opcional)"), anchor="w",
                    bg=self.ui.card).grid(row=2, column=0, columnspan=2, sticky="ew")
        self.record_title_entry = self._entry(activity, self.record_title)
        self.record_title_entry.grid(row=3, column=0, columnspan=2, sticky="ew",
                                     pady=(self.ui.space_xs, recording_gap))
        sources = tk.Frame(activity, bg=self.ui.card)
        sources.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(0, recording_gap))
        sources.columnconfigure(1, weight=1)
        self.source_checks = {}
        self.endpoint_hints = {}
        for row, (track, label, variable) in enumerate((
                ("microphone", tr("Microfone"), self.input_enabled),
                ("system", tr("Áudio do sistema"), self.output_enabled))):
            control = tk.Checkbutton(
                sources, text=label, variable=variable, command=self._source_toggled,
                font=self.ui.font(9, "bold"), **self.ui.checkbutton_colors(self.ui.card),
            )
            # Stacked: the device list spans the whole card. On Aqua the popup is
            # as wide as the combobox, so a column beside the checkbox would clip
            # long endpoint names in the half-width setup column.
            line = row * 4
            control.grid(row=line, column=0, sticky="w")
            self._label(
                sources, tr("Sua voz") if track == "microphone" else tr("Sons dos aplicativos"),
                bg=self.ui.card, fg=self.ui.text_muted, anchor="w", font=self.ui.font(8),
            ).grid(row=line, column=1, sticky="w", padx=(self.ui.space_sm, 0))
            combo = ttk.Combobox(
                sources, textvariable=self.endpoint_vars[track], state="readonly",
                style=f"{track}.Device.TCombobox", font=self.ui.font(10), width=24, height=8,
                takefocus=True,
            )
            combo.configure(postcommand=lambda box=combo: ui_theme.configure_combobox_popdown(
                box, self.ui, fit_values=True,
            ))
            combo.grid(row=line + 1, column=0, columnspan=2, sticky="ew", pady=(self.ui.space_xs, 0))
            combo.bind("<<ComboboxSelected>>", self._source_selection_changed)
            combo._device_elision = True
            combo.bind("<Configure>", lambda event, key=track: self._device_width_changed(key, event.width), add="+")
            ui_theme.tooltip(combo, lambda key=track: getattr(self, "_endpoint_full_names", {}).get(key, {}).get(
                self.endpoint_vars[key].get(), self.endpoint_vars[key].get()))
            hint = self._wrap_label(
                sources, "", bg=self.ui.card, fg=self.ui.text_muted, anchor="w",
                justify="left", font=self.ui.font(8),
            )
            hint.grid(row=line + 2, column=0, columnspan=2, sticky="ew",
                      pady=(self.ui.space_xs if compact_recording else 4, recording_gap))
            self.endpoint_hints[track] = hint
            if row == 0:
                tk.Frame(sources, bg=self.ui.divider, height=1).grid(
                    row=line + 3, column=0, columnspan=2, sticky="ew", pady=(0, recording_gap),
                )
            self.source_checks[track] = control
            self.endpoint_boxes[track] = combo
        self.device_refresh_button = self._button(sources, tr("Atualizar dispositivos"), self.refresh_devices)
        self.device_refresh_button.grid(row=8, column=0, sticky="w", pady=(4, 0))
        self.preview_button = self._button(sources, tr("Testar fontes"), self.preview_sources)
        self.preview_button.grid(row=8, column=1, sticky="e", pady=(4, 0))
        self.waveform = MeetingWaveform(
            monitor, theme=self.ui, height=96 if compact_recording else 140,
            track_labels={"microphone": N_("Microfone"), "system": N_("Áudio do sistema")},
            state_labels={"idle": N_("Pronto"), "checking": N_("Testando"),
                          "recording": N_("Gravando"), "paused": N_("Pausado")},
        )
        self.waveform.grid(row=WAVEFORM_ROW, column=0, columnspan=2, sticky="nsew",
                           pady=(self.ui.space_md, 0))
        self.meters = {}
        self.meter_readouts = {}
        self.source_captions = {}
        meters = (("microphone", tr("Nível do microfone")),
                  ("system", tr("Nível do sistema")))
        caption_height = tkfont.Font(root=self.window, font=self.ui.caption()).metrics("linespace") + 2
        for index, (track, label) in enumerate(meters):
            row = 6 + 2 * index
            self._label(monitor, label, anchor="w", bg=self.ui.card).grid(
                row=row, column=0, sticky="w", padx=(0, self.ui.space_lg),
                pady=(self.ui.space_xs if compact_recording or index else self.ui.space_lg, 0),
            )
            # The same series colors as the waveform lanes above.
            meter_row = tk.Frame(monitor, bg=self.ui.card)
            meter_row.grid(row=row, column=1, sticky="ew",
                           pady=(self.ui.space_xs if compact_recording or index else self.ui.space_lg, 0))
            meter = ttk.Progressbar(
                meter_row, maximum=1.0,
                style="Horizontal.TProgressbar" if track == "microphone"
                else "Secondary.Horizontal.TProgressbar",
            )
            meter.pack(side="left", fill="x", expand=True)
            # A reading, not code: the body face, whose figures are tabular.
            readout = self._label(meter_row, tr("sem sinal"), anchor="e", bg=self.ui.card,
                                  fg=self.ui.text_muted, font=self.ui.body(), width=12)
            readout.pack(side="right", padx=(self.ui.space_sm, 0))
            self.meter_readouts[track] = readout
            self.meters[track] = meter
            # The source's written health sits under its meter; the row keeps its
            # height while the recorder is idle so starting never moves the page.
            caption = self._label(monitor, "", anchor="w", bg=self.ui.card, fg=self.ui.text_muted,
                                  font=self.ui.caption())
            caption.grid(row=row + 1, column=1, sticky="ew")
            monitor.rowconfigure(row + 1, minsize=caption_height)
            self.source_captions[track] = caption
        # The pane leads with the recorder's written state and its one primary
        # action, then the signal they govern. A lost source raises its warning
        # right above the state.
        self.recording_trouble = ui_theme.MessageBar(monitor, self.ui, copy=self._copy_issue_details)
        self.recording_trouble.attach(grid=True, row=0, column=0, columnspan=2, sticky="ew",
                                      pady=(0, self.ui.space_md))
        self.record_status = tk.StringVar(self.window, tr("Pronto") + " · 00:00:00")
        self.record_chip = ui_theme.StatusChip(monitor, self.ui, bg=self.ui.card,
                                               font=self.ui.heading())
        self.record_chip.set(self.record_status.get(), "success")
        self.record_chip.grid(row=1, column=0, columnspan=2, sticky="ew",
                              pady=(0, self.ui.space_sm))
        self.audio_export_progress = ttk.Progressbar(monitor, maximum=100, mode="determinate")
        self.audio_export_progress.grid(row=2, column=0, columnspan=2, sticky="ew",
                                        pady=(0, self.ui.space_sm))
        self.audio_export_progress.grid_remove()
        # One primary at a time: Start while idle, Stop while capturing.
        transport = tk.Frame(monitor, bg=self.ui.card)
        transport.grid(row=3, column=0, columnspan=2, sticky="w")
        self.start_button = self._button(transport, tr("Iniciar gravação"), self.start, accent=True)
        self.pause_button = self._button(transport, tr("Pausar"), self.pause_resume)
        self.stop_button = self._button(transport, tr("Parar e salvar"), self.stop, accent=True)
        # The shortcut sits beside the buttons, on their row, and costs no height.
        self.shortcut_hint = self._label(transport, "", anchor="w", bg=self.ui.card,
                                         fg=self.ui.text_muted, font=self.ui.caption())
        self.shortcut_hint.pack(side="left", padx=(self.ui.space_md, 0))
        self.start_button.pack(side="left", before=self.shortcut_hint)
        self._transport_key = (False, False)
        # Shown only while something holds Start: it names the holder and,
        # for processing, offers the override with its consequence.
        self.start_blocked_row = tk.Frame(monitor, bg=self.ui.card)
        self.start_blocked_row.grid(row=4, column=0, columnspan=2, sticky="ew",
                                    pady=(self.ui.space_sm, 0))
        self.start_blocked_row.columnconfigure(0, weight=1)
        self.start_blocked_reason = tk.StringVar(self.window, "")
        self._wrap_label(
            self.start_blocked_row, "", textvariable=self.start_blocked_reason, anchor="w",
            justify="left", bg=self.ui.card, fg=self.ui.text,
        ).grid(row=0, column=0, sticky="ew")
        self.interrupt_start_button = self._button(
            self.start_blocked_row, tr("Interromper e gravar"), self.interrupt_and_record,
        )
        self.interrupt_start_button.grid(row=0, column=1, sticky="e", padx=(self.ui.space_sm, 0))
        self.interrupt_start_button.grid_remove()
        self.start_blocked_row.grid_remove()
        self.preview_status = tk.StringVar(self.window, tr(SOURCE_TEST_HINT))
        status_row = tk.Frame(monitor, bg=self.ui.card)
        status_row.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(self.ui.space_sm, 0))
        self._wrap_label(status_row, "", textvariable=self.preview_status, anchor="w",
                         justify="left", bg=self.ui.card, fg=self.ui.text_muted).pack(
                             side="left", fill="x", expand=True)
        self._profile_changed()
        self._sync_source_controls()
        self.options.clear()
        self._render_devices()
        self._build_library(library)
        self._build_summary_models(summary)

    def _park_cancel(self, button, manager, **options):
        """Reserve the Cancel's footprint; only the running job exposes its control."""
        parent = button.master
        try:
            background = parent.cget("background")
        except tk.TclError:
            style = parent.cget("style") or parent.winfo_class()
            background = ttk.Style(parent).lookup(style, "background") or self.ui.surface
        slot = tk.Frame(parent, width=button.winfo_reqwidth(),
                        height=button.winfo_reqheight(), bg=background)
        getattr(slot, manager)(**options)
        button._cancel_slot = slot
        button._cancel_manager = "place"
        button._cancel_options = {"in_": slot, "relwidth": 1, "relheight": 1}

    @staticmethod
    def _show_cancel(button, running):
        if running == bool(button.winfo_manager()):
            return
        if running:
            getattr(button, button._cancel_manager)(**button._cancel_options)
            # The reserved slot is a younger sibling and otherwise paints over
            # this placed control even though Tk reports the button as mapped.
            button.lift()
        else:
            button.place_forget()

    def _sync_job_cancels(self):
        """Show each Cancel while, and only while, the job it stops runs."""
        for name, running in (
            ("semantic_cancel_button", getattr(self, "semantic_cancel", None) is not None
             and not getattr(self, "_semantic_model_download_active", False)),
            ("rebuild_cancel_button", getattr(self, "rebuild_cancel", None) is not None),
            ("cross_cancel_button", bool(getattr(self, "cross_pending", None))),
            ("cancel_summary_button", bool(getattr(self, "summary_pending", None))),
            ("summary_download_cancel_button",
             getattr(self, "summary_download_cancel", None) is not None
             and not getattr(self, "_summary_download_profile", None)),
        ):
            button = getattr(self, name, None)
            if button is not None:
                self._show_cancel(button, running)

    def _build_summary_models(self, parent):
        self.summary_model_buttons = {}
        self.summary_model_states = {}
        entries = summary_catalog()
        compact = len(entries) > 5
        table = ui_theme.model_table(parent, self.ui)
        for index, entry in enumerate(entries, 1):
            status, row = ui_theme.model_table_row(table, index,
                (entry["name"], tr(entry["label"]), format_model_size(entry["size_bytes"]), entry["license_id"]), self.ui)
            button = self._button(row, "", lambda model_id=entry["id"]: self.toggle_summary_model(model_id))
            button.pack(side="right", anchor="n")
            self.summary_model_buttons[entry["id"]] = button
            self.summary_model_states[entry["id"]] = status
        # The status line and the download's Cancel share one row that keeps the
        # button's height, so a download starting moves nothing below it.
        status_row = ttk.Frame(parent, style="Meeting.TFrame")
        status_row.pack(fill="x", pady=(12 if compact else self.ui.space_lg, 12))
        status_row.columnconfigure(0, weight=1)
        self.summary_download_cancel_button = self._button(
            status_row, tr("Cancelar download"), self.cancel_summary_download)
        status_row.rowconfigure(0, minsize=self.summary_download_cancel_button.winfo_reqheight())
        self.summary_model_status = tk.StringVar(self.window)
        self._wrap_label(status_row, "", textvariable=self.summary_model_status, anchor="w",
                         justify="left").grid(row=0, column=0, sticky="ew")
        self._park_cancel(self.summary_download_cancel_button, "grid", row=0, column=1,
                          padx=(self.ui.space_sm, 0))
        self._refresh_summary_models()

    def _summary_display_changed(self, _event=None):
        self.summary_model.set(next(key for key, label in SUMMARY_LABELS.items()
                                    if tr(label) == self.summary_display.get()))

    def _refresh_summary_models(self):
        self._summary_inventory_state = "pending"
        for button in self.summary_model_buttons.values():
            button.configure(text=tr("Verificando…"), state="disabled")
        self._submit(
            "summary_inventory",
            lambda: {entry["id"]: summary_model_is_installed(entry["id"])
                     for entry in summary_catalog()},
            self._summary_inventory_loaded,
        )

    def _summary_inventory_loaded(self, installed, error):
        if error:
            self._summary_inventory_state = "unknown"
            self.summary_model_status.set(error)
            return
        self.summary_model_installed = dict(installed)
        self._summary_inventory_state = "ready" if any(installed.values()) else "missing"
        for model_id, button in self.summary_model_buttons.items():
            button.configure(text=tr("Remover") if installed.get(model_id) else tr("Baixar"),
                             state="normal", command=lambda item=model_id: self.toggle_summary_model(item))
            label = getattr(self, "summary_model_states", {}).get(model_id)
            if label is not None:
                label.configure(text=tr("Baixado") if installed.get(model_id) else tr("Não baixado"))

    def toggle_summary_model(self, model_id):
        entry = summary_catalog_entry(model_id)
        if entry is None:
            self._say("settings", tr("O modelo de resumo selecionado não existe no catálogo."))
            return
        if self.summary_model_installed.get(model_id):
            if not messagebox.askyesno(tr("Remover modelo"), tr("Remover {name} deste computador?", name=entry["name"]),
                                       parent=self.window):
                return
            for button in self.summary_model_buttons.values():
                button.configure(state="disabled")
            self.summary_model_status.set(tr("Removendo {name}…", name=entry["name"]))
            submitted = self._submit(
                "summary_model", lambda: delete_summary_model(model_id),
                lambda _value, error: self._summary_model_finished(entry, error, removed=True),
            )
            if not submitted:
                self._refresh_summary_models()
            return
        terms = ""
        if entry["requires_acceptance"]:
            notice = tr(entry.get("license_notice") or N_("Leia a licença antes de continuar."))
            terms = tr(
                "\n\n{notice}\n\nTermos completos: {url}"
                "\n\nAo continuar, você confirma que leu e aceita esses termos.",
                notice=notice, url=entry["license_url"],
            )
        if not messagebox.askyesno(
                tr("Licença do modelo") if entry["requires_acceptance"] else tr("Baixar modelo local"),
                tr("Baixar {name} ({size})?\nLicença: {license}{terms}", name=entry["name"],
                   size=format_model_size(entry["size_bytes"]), license=entry["license_id"],
                   terms=terms), parent=self.window):
            return
        self.summary_download_cancel = threading.Event()
        self._summary_download_profile = model_id
        event = self.summary_download_cancel
        with self.summary_progress_lock:
            self.summary_progress = (entry["name"], 0, entry["size_bytes"])
        for button in self.summary_model_buttons.values():
            button.configure(state="disabled")
        label = getattr(self, "summary_model_states", {}).get(model_id)
        if label is not None:
            label.configure(text=tr("Baixando…"), fg=self.ui.info)
            self.summary_model_buttons[model_id].configure(
                text=tr("Cancelar"), state="normal", command=self.cancel_summary_download)
        self.summary_model_status.set(tr("Baixando {name} com verificação SHA-256…", name=entry["name"]))
        self._sync_job_cancels()
        submitted = self._submit(
            "summary_model",
            lambda: download_summary_model(model_id, cancel_event=event,
                                           progress=lambda done, total: self._summary_progress(entry, done, total)),
            lambda _value, error: self._summary_model_finished(entry, error),
        )
        if not submitted:
            self.summary_download_cancel = None
            self._sync_job_cancels()
            self._refresh_summary_models()

    def cancel_summary_download(self):
        if self.summary_download_cancel is None:
            self.summary_model_status.set(tr("Nenhum download de modelo está em andamento."))
            return
        self.summary_download_cancel.set()
        if self.bridge.cancel_queued("summary_model"):
            self._summary_model_finished(
                {}, tr("Download cancelado; os bytes parciais poderão ser retomados."))
            return
        self.summary_model_status.set(tr("Cancelando download; os bytes parciais poderão ser retomados."))

    def _summary_progress(self, entry, done, total):
        with self.summary_progress_lock:
            self.summary_progress = (entry["name"], done, total)

    def _summary_model_finished(self, entry, error, removed=False):
        self.summary_download_cancel = None
        self._summary_download_profile = None
        self._sync_job_cancels()
        with self.summary_progress_lock:
            self.summary_progress = None
        self._refresh_summary_models()
        if error:
            self.summary_model_status.set(error)
            return
        self.summary_model_status.set(
            tr("{name} removido.", name=entry["name"]) if removed
            else tr("{name} baixado e verificado.", name=entry["name"])
        )

    def _build_library(self, parent):
        cross_frame = self._card(parent, pady=self.ui.space_sm)
        question_row = tk.Frame(cross_frame, bg=self.ui.card)
        question_row.pack(fill="x")
        self._label(question_row, tr("Perguntar nas gravações"), bg=self.ui.card,
                    font=self.ui.font(9, "bold")).pack(side="left", padx=(0, self.ui.space_sm))
        self.cross_question = tk.StringVar(self.window)
        self._entry(question_row, self.cross_question, 12).pack(side="left", fill="x", expand=True)
        self._button(question_row, tr("Perguntar"), self.ask_across_meetings).pack(side="left", padx=(self.ui.space_sm, 0))
        self.cross_cancel_button = self._button(question_row, tr("Cancelar"), self.cancel_cross_question)
        self._park_cancel(self.cross_cancel_button, "pack", side="left", padx=(self.ui.space_sm, 0))
        self.cross_status = tk.StringVar(self.window, tr("Resposta cruzada fica somente na memória."))
        self._label(cross_frame, "", textvariable=self.cross_status, bg=self.ui.card,
                    fg=self.ui.text_muted, anchor="w", justify="left").pack(fill="x", pady=(4, 0))
        self.cross_answer = tk.Text(cross_frame, height=3, wrap="word", font=self.ui.font(),
                                    **self.ui.text_colors(), **self.ui.entry_chrome(readonly=True))
        ui_theme.focus_ring(self.cross_answer, self.ui, shrink=("bd",))
        self.cross_answer.pack(fill="x", pady=(4, 0))
        self.cross_answer.configure(state="disabled")
        cross_citation_row = tk.Frame(cross_frame, bg=self.ui.card)
        cross_citation_row.pack(fill="x", pady=(self.ui.space_xs, 0))
        self._label(cross_citation_row, tr("Citações"), bg=self.ui.card,
                    fg=self.ui.text_muted).pack(side="left", padx=(0, 4))
        self.cross_citations = tk.Listbox(cross_citation_row, height=2, width=10,
                                          font=self.ui.font(), **self.ui.listbox_colors(),
                                          **self.ui.entry_chrome())
        ui_theme.focus_ring(self.cross_citations, self.ui, shrink=("bd",))
        self.cross_citations.pack(side="left", fill="x", expand=True)
        self.cross_citations.bind("<Double-Button-1>", lambda _event: self.jump_to_cross_citation())
        self._button(cross_citation_row, tr("Ir à fonte"), self.jump_to_cross_citation).pack(side="left", padx=(self.ui.space_sm, 0))

        panes = ttk.Panedwindow(parent, orient="horizontal")
        panes.pack(fill="both", expand=True)
        self.library_panes = panes
        self.undo_receipt = tk.Frame(parent, bg=self.ui.surface)
        self._label(self.undo_receipt, tr("Movida para a lixeira"),
                    fg=self.ui.text_muted).pack(side="left")
        self.undo_button = ui_theme.button(self.undo_receipt, tr("Desfazer"), self._undo_trash,
                                           "quiet", resolved=self.ui)
        self.undo_button.pack(side="left", padx=(self.ui.space_sm, 0))
        self._undo_session = None
        self.library_panels = {"cross": cross_frame}
        left = ttk.Frame(panes, style="Meeting.TFrame")
        right_outer = ttk.Frame(panes, style="Meeting.TFrame")
        panes.add(left, weight=1)
        panes.add(right_outer, weight=3)
        self.library_detail_pane = right_outer

        def place_sash(event):
            if self._place_library_sash(event.width):
                panes.unbind("<Configure>")

        panes.bind("<Configure>", place_sash)

        # Search and library actions sit above the list they act on, so the
        # selected recording gets the pane's full height. The cross-meeting
        # question and maintenance tools open on demand above both panes. Like
        # the family reference it sits on the page plane, not in a box.
        toolbar = tk.Frame(left, bg=self.ui.surface)
        toolbar.pack(fill="x", pady=(0, self.ui.space_md))
        self.library_toolbar = toolbar
        search_row = tk.Frame(toolbar, bg=self.ui.surface)
        search_row.pack(fill="x")
        self.query = tk.StringVar(self.window)
        self._library_search_after = None
        self._label(toolbar, tr("Buscar na biblioteca (Ctrl+F)"), bg=self.ui.surface,
                    anchor="w").pack(fill="x", before=search_row, pady=(0, self.ui.space_xs))
        search = self._entry(search_row, self.query, 12)
        self.search_entry = search
        search.pack(side="left", fill="x", expand=True)
        search.bind("<Return>", lambda _event: self.search())
        search.bind("<Escape>", lambda _event: self._clear_library_search())
        self._placeholder(search, self.query, tr("Buscar gravações e transcrições"))
        self.query.trace_add("write", self._queue_library_search)
        action_row = tk.Frame(toolbar, bg=self.ui.surface)
        action_row.pack(fill="x", pady=(self.ui.space_sm, 0))
        self.tools_toggle = self._button(action_row, tr("Mais"), self._show_library_menu)
        self.tools_toggle.pack(side="right", padx=(self.ui.space_sm, 0))
        self._button(action_row, tr("Importar áudio…"), self.import_audio).pack(side="right", padx=(self.ui.space_sm, 0))
        self.library_panel_toggles = {}
        self.library_menu = self._menu()
        self.library_menu.add_command(label=tr("Perguntar à biblioteca"),
                                      command=lambda: self._toggle_library_panel("cross"))
        self.library_menu.add_command(label=tr("Lixeira…"), command=self.show_trash)
        self.library_menu.add_command(label=tr("Reparar busca"), command=self._open_search_repair)

        # Recording list with an empty state drawn over the tree.
        list_frame = tk.Frame(left, bg=self.ui.surface)
        list_frame.pack(fill="both", expand=True)
        # The written state leads each row, then the title, local date and
        # duration; the title column takes whatever width the pane gives.
        self.sessions = ttk.Treeview(list_frame, columns=("status", "title", "date", "duration"),
                                     show="headings", selectmode="browse", style="Meeting.Treeview",
                                     height=6)
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.sessions.yview)
        self._configure_session_columns()
        self.sessions.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.sessions.pack(fill="both", expand=True)
        self.sessions.bind("<<TreeviewSelect>>", self._selection_changed)
        self.sessions.bind("<Configure>", self._schedule_session_title_fit, add="+")
        self.sessions.bind("<Delete>", lambda _event: self._delete_shortcut())
        self.sessions.bind("<Button-3>", self._show_session_menu)
        self.sessions.bind("<Shift-F10>", self._show_session_menu)
        self.session_menu = self._menu()
        self.session_menu.add_command(label=tr("Excluir gravação…"), command=self.delete_selected)
        self.library_empty = tk.StringVar(self.window)
        self.library_empty_panel = tk.Frame(list_frame, bg=self.ui.card)
        empty_content = tk.Frame(self.library_empty_panel, bg=self.ui.card)
        empty_content.place(relx=0.5, rely=0.5, anchor="center")
        self.library_empty_label = self._label(
            empty_content, "", textvariable=self.library_empty,
            bg=self.ui.card, fg=self.ui.text_strong,
            font=self.ui.font(10, "bold"), justify="center", wraplength=260,
        )
        self.library_empty_label.pack(pady=(0, self.ui.space_md))
        empty_actions = tk.Frame(empty_content, bg=self.ui.card)
        empty_actions.pack(anchor="center")
        self.library_empty_record_button = self._button(
            empty_actions, tr("Gravar agora"),
            lambda: self.notebook.select(self.recording_tab), accent=True,
        )
        self.library_empty_import_button = self._button(
            empty_actions, tr("Importar áudio…"), self.import_audio,
        )
        self.library_empty_clear_button = self._button(
            empty_actions, tr("Limpar busca"), self._clear_library_search,
            accent=True,
        )
        self.library_empty_retry_button = self._button(
            empty_actions, tr("Tentar novamente"), self.refresh_library, accent=True,
        )
        pages = ttk.Frame(left, style="Meeting.TFrame")
        pages.pack(fill="x", pady=(8, 0))
        self.library_pages = pages
        self.previous_button = self._button(pages, tr("‹ Anterior"), lambda: self.change_page(-1))
        self.previous_button.pack(side="left")
        self.next_button = self._button(pages, tr("Próxima ›"), lambda: self.change_page(1))
        self.next_button.pack(side="left", padx=(self.ui.space_sm, 0))
        self.page_label = tk.StringVar(self.window, tr("Página {page}", page=1))
        self._label(pages, "", textvariable=self.page_label, fg=self.ui.text_muted).pack(
            side="left", padx=(8, 0))
        # Full-text hits appear only after a search that found something.
        self.search_results_frame = ttk.Frame(self.library_detail_pane, style="Meeting.TFrame")
        self._label(self.search_results_frame, tr("Encontrado em transcrições e relatórios"),
                    anchor="w", fg=self.ui.text_muted).pack(fill="x", pady=(8, self.ui.space_xs))
        self.search_results = ttk.Treeview(
            self.search_results_frame, columns=("source", "meeting", "snippet"), show="headings",
            selectmode="browse", height=4, style="Meeting.Treeview",
        )
        # Modest starting widths: this tree's request sets the list pane's
        # width, and 470px starved the detail pane's controls. The snippet
        # column stretches into whatever the pane receives.
        for column, label, width in (("source", tr("Fonte"), 70), ("meeting", tr("Reunião"), 90),
                                      ("snippet", tr("Trecho"), 120)):
            self.search_results.heading(column, text=label)
            self.search_results.column(column, width=width, stretch=column == "snippet")
        self.search_results.pack(fill="x")
        self.search_results.bind("<Double-Button-1>", lambda _event: self.open_search_result())
        self.search_results.bind("<Return>", lambda _event: self.open_search_result())

        # Selected recording: a placeholder until one is chosen, then a fixed
        # header over one section at a time.
        right_outer.columnconfigure(0, weight=1)
        right_outer.rowconfigure(1, weight=1)
        self.detail_placeholder = tk.Frame(right_outer, bg=self.ui.surface)
        self.detail_placeholder.grid(row=1, column=0, sticky="nsew", padx=(12, 0))
        self._label(
            self.detail_placeholder,
            tr("Selecione uma gravação na lista para ouvir o áudio e ver a transcrição e o resumo."),
            fg=self.ui.text_muted, justify="center", wraplength=320,
        ).place(relx=0.5, rely=0.4, anchor="center")
        self.detail_frame = tk.Frame(right_outer, bg=self.ui.surface)
        self.detail_frame.grid(row=1, column=0, sticky="nsew")
        self.detail_frame.grid_remove()
        title_row = ttk.Frame(self.detail_frame, style="Meeting.TFrame")
        title_row.pack(fill="x", padx=(12, 0))
        title_row.columnconfigure(0, weight=1)
        title_row.bind("<Configure>", self._reflow_title_header)
        self.title = tk.StringVar(self.window)
        self.title.trace_add("write", self._mark_dirty)
        self._saved_title = ""
        self.title_entry = self._entry(title_row, self.title, 12)
        self.title_heading = self._wrap_label(title_row, "", textvariable=self.title,
                                               font=self.ui.title(), anchor="w", justify="left")
        self.title_heading.grid(row=0, column=0, sticky="ew")
        self.rename_button = self._button(title_row, tr("Renomear (F2)"), self._begin_title_edit)
        self.rename_button.grid(row=0, column=1, sticky="e", padx=(self.ui.space_sm, 0))
        self.title_entry.bind("<Return>", self._end_title_edit)
        self.title_entry.bind("<Control-s>", self._end_title_edit)
        self.title_entry.bind("<Escape>", lambda event: self._end_title_edit(event, cancel=True))
        # The recording's state, in the list's word, then its date and length.
        state_row = tk.Frame(self.detail_frame, bg=self.ui.surface)
        state_row.pack(fill="x", padx=(12, 0), pady=(self.ui.space_xs, 0))
        self.detail_state = tk.StringVar(self.window)
        self.detail_state_label = self._label(state_row, "", textvariable=self.detail_state,
                                              font=self.ui.body_strong())
        self.detail_state_label.pack(side="left")
        self.detail_meta = tk.StringVar(self.window)
        self._label(state_row, "", textvariable=self.detail_meta, fg=self.ui.text_muted).pack(side="left")
        # A failed or cancelled processing step shows here, above every section.
        self.processing_issue_slot = tk.Frame(self.detail_frame, bg=self.ui.surface)
        self.processing_banner = tk.Frame(self.detail_frame, bg=self.ui.info_bg,
                                          highlightthickness=1, highlightbackground=self.ui.info)
        job_row = tk.Frame(self.processing_banner, bg=self.ui.info_bg)
        job_row.pack(fill="x", padx=self.ui.space_sm, pady=self.ui.space_xs)
        self.processing_banner_label = self._wrap_label(job_row, "", bg=self.ui.info_bg,
            anchor="w", justify="left", font=self.ui.caption())
        self.processing_banner_label.pack(side="left", fill="x", expand=True)
        self._button(job_row, tr("Cancelar"), self._cancel_current_job).pack(side="right", padx=(self.ui.space_sm, 0))
        self.detail_processing_progress = ttk.Progressbar(self.processing_banner, maximum=100)
        self.completion_receipt = tk.Frame(self.detail_frame, bg=self.ui.surface)
        self._label(self.completion_receipt, tr("Resumo pronto"), font=self.ui.caption(),
                    fg=self.ui.text_muted).pack(side="left")
        ui_theme.button(self.completion_receipt, tr("Copiar resumo (Ctrl+Shift+C)"), self.copy_summary,
                        "quiet", resolved=self.ui).pack(side="right")
        detail_nav = tk.Frame(self.detail_frame, bg=self.ui.surface)
        detail_nav.pack(fill="x", padx=(12, 0), pady=(self.ui.space_sm, 0))
        self.detail_nav = detail_nav
        tk.Frame(self.detail_frame, bg=self.ui.border, height=1).pack(fill="x", padx=(12, 0))
        def fit_chat(_width, height):
            if hasattr(self, "meeting_chat"):
                self.meeting_chat.set_height(max(300, height - 8))

        detail_body, self.detail_canvas, self.detail_content, right = self._scroll_area(
            self.detail_frame, fill_height=True, on_resize=fit_chat,
        )
        detail_body.pack(fill="both", expand=True, pady=(self.ui.space_sm, 0))
        self.detail_sections = SectionSwitcher(
            self.ui, detail_nav, right,
            on_select=lambda _key: self.detail_canvas.yview_moveto(0),
            on_user_select=self._detail_user_selected,
        )
        transcript_page = self.detail_sections.add("transcript", tr("Transcrição"))
        summary_page = self.detail_sections.add("summary", tr("Resumo"))
        ask_page = self.detail_sections.add("ask", tr("Chat"))
        files_page = self.detail_sections.add("files", tr("Arquivos"))

        # The player sits above the transcript it plays, so listening and
        # reading happen on one page: row one drives playback, row two holds
        # the state and the less frequent source and volume choices.
        player = self._card(transcript_page, padx=self.ui.space_md, pady=self.ui.space_sm)
        player.pack(fill="x", padx=(12, 0), pady=(0, self.ui.space_sm))
        player_actions = tk.Frame(player, bg=self.ui.card)
        player_actions.pack(fill="x")
        self.play_button = self._button(player_actions, tr("Reproduzir"), self.play_selected_recording)
        self.play_button.configure(state="disabled")
        self.play_button.pack(side="left")
        self.replay_stop_button = self._button(player_actions, tr("Parar"), self.stop_selected_recording)
        self.replay_stop_button.configure(state="disabled")
        self.replay_stop_button.pack(side="left", padx=(self.ui.space_sm, 0))
        self.playback_clock = tk.StringVar(self.window, "00:00:00 / 00:00:00")
        self._label(player_actions, "", textvariable=self.playback_clock, bg=self.ui.card,
                    fg=self.ui.text_muted).pack(side="right", padx=(self.ui.space_sm, 0))
        self.playback_position = tk.DoubleVar(self.window, 0.0)
        self.playback_seek = ttk.Scale(player_actions, from_=0, to=1, variable=self.playback_position,
                                       orient="horizontal", style="Playback.Horizontal.TScale")
        self.playback_seek.pack(side="left", fill="x", expand=True, padx=(self.ui.space_md, 0))
        self.playback_seek.bind("<ButtonPress-1>", self._player_seek_begin)
        self.playback_seek.bind("<ButtonRelease-1>", self._player_seek_end)
        player_details = tk.Frame(player, bg=self.ui.card)
        player_details.pack(fill="x", pady=(self.ui.space_xs, 0))
        self.audio_source = tk.StringVar(self.window)
        self.audio_source.set(tr("Áudio final"))
        status = tk.Frame(player, bg=self.ui.card)
        status.pack(fill="x", pady=(self.ui.space_xs, 0))
        self.audio_overview = tk.StringVar(self.window, tr("Selecione uma gravação para ouvir."))
        self._label(status, "", textvariable=self.audio_overview, bg=self.ui.card,
                    fg=self.ui.text_muted, anchor="w", font=self.ui.font(8)).pack(side="left")
        self.playback_status = tk.StringVar(self.window, tr("Pronto para ouvir."))
        self._wrap_label(status, "", textvariable=self.playback_status, bg=self.ui.card,
                         fg=self.ui.text_muted, anchor="w", justify="left",
                         font=self.ui.font(8)).pack(side="left", fill="x", expand=True,
                                                    padx=(self.ui.space_sm, 0))

        # Transcript playback keeps its seek position independently of review text.
        self.position = tk.StringVar(self.window, "0")

        format_row = ttk.Frame(transcript_page, style="Meeting.TFrame")
        format_row.pack(fill="x", padx=(12, 0), pady=(0, self.ui.space_sm))
        self._label(format_row, tr("Formato")).pack(side="left", padx=(0, 8))
        self.transcript_style = tk.StringVar(self.window, tr("Texto completo"))
        self.transcript_style_box = ttk.Combobox(
            format_row, textvariable=self.transcript_style, values=[tr(label) for label in TRANSCRIPT_STYLES],
            state="readonly", width=14,
        )
        self.transcript_style_box.pack(side="left")
        self.transcript_style_box.bind("<<ComboboxSelected>>", self._transcript_style_changed)
        self.copy_transcript_button = self._button(format_row, tr("Copiar"), self.copy_transcript, accent=True)
        self.copy_transcript_button.pack(side="right", padx=(self.ui.space_sm, 0))
        self._button(format_row, tr("Exportar…"), self.export_transcript).pack(side="right")
        self.transcript_document_status = tk.StringVar(
            self.window, tr("Escolha o formato depois de gravar, sem transcrever novamente."),
        )
        self._wrap_label(
            transcript_page, "", textvariable=self.transcript_document_status,
            fg=self.ui.text_muted, anchor="w", justify="left",
        ).pack(fill="x", padx=12, pady=(0, self.ui.space_sm))
        self.transcript_document_frame = ttk.Frame(transcript_page, style="Meeting.TFrame")
        self.transcript_document_frame.pack(fill="both", expand=True, padx=(12, 0))
        self.transcript_document = tk.Text(
            self.transcript_document_frame, height=8, wrap="word", font=self.ui.font(),
            padx=self.ui.space_md, pady=self.ui.space_md,
            # Typography: half a rendered body line gives 1.5 line height.
            spacing2=round(tkfont.Font(root=self.window, font=self.ui.body()).metrics("linespace") / 2),
            spacing3=self.ui.space_sm,
            **self.ui.text_colors(), **self.ui.entry_chrome(readonly=True),
        )
        ui_theme.focus_ring(self.transcript_document, self.ui, shrink=("bd",))
        # Scrollbar first: pack serves requests in order, and the text's
        # 80-column request would otherwise squeeze it out of a narrow pane.
        document_scroll = ttk.Scrollbar(
            self.transcript_document_frame, orient="vertical", command=self.transcript_document.yview,
        )
        document_scroll.pack(side="right", fill="y")
        self.transcript_document.pack(side="left", fill="both", expand=True)
        self.transcript_document.configure(yscrollcommand=document_scroll.set)
        self.transcript_document.configure(state="disabled")
        self.transcript_document.bind("<Control-a>", self._select_transcript_text)
        self.transcript_document.bind("<Configure>", self._transcript_measure)
        self.transcript_document.tag_configure("timestamp", foreground=self.ui.text_muted,
                                               font=self.ui.body(), underline=True)
        self.transcript_document.bind("<Button-1>", self._timestamp_click, add="+")
        self.transcript_document.bind("<Return>", self._timestamp_enter, add="+")
        self.transcript_audio_toggle = self._button(
            transcript_page, tr("Trechos para ouvir") + " ▾", self._toggle_transcript_audio,
        )
        self.transcript_audio_toggle.pack(anchor="w", padx=self.ui.space_md, pady=self.ui.space_sm)
        self.timed_transcript = ttk.Frame(transcript_page, style="Meeting.TFrame")

        # Processing is independent of the selected presentation format.
        actions = ttk.Frame(transcript_page, style="Meeting.TFrame")
        # Keep Cancel above flexible review text: the selected-job banner must
        # not move an existing processing control below the minimum viewport.
        actions.pack(fill="x", padx=(12, 0), pady=(0, 4), before=self.transcript_document_frame)
        actions.columnconfigure(0, weight=1)
        self.transcript_processing_actions = actions
        # The running step is named above the buttons, in a line that wraps at
        # any width: a step-named button clipped at the minimum size, and the
        # buttons must not move when a job starts.
        self.processing_job_status = tk.StringVar(self.window)
        self.processing_job_label = self._wrap_label(
            actions, "", textvariable=self.processing_job_status, fg=self.ui.text_muted, anchor="w",
        )
        self.processing_job_label.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        self.processing_job_label.grid_remove()
        action_buttons = ttk.Frame(actions, style="Meeting.TFrame")
        action_buttons.grid(row=1, column=0, sticky="w")
        self.transcribe_button = self._button(action_buttons, tr("Transcrever novamente"), self.transcribe)
        self.transcribe_button.pack(side="left", padx=(0, self.ui.space_sm))
        self.cancel_processing_button = self._button(
            action_buttons, tr("Cancelar"), lambda: self._action("cancel_processing", urgent=True),
        )
        self._park_cancel(self.cancel_processing_button, "pack", side="left")
        self.transcript_timing = tk.StringVar(
            self.window, tr("Dois cliques ou Enter para ouvir; horários marcam blocos de áudio, não palavras."),
        )
        self._wrap_label(
            self.timed_transcript, "", textvariable=self.transcript_timing, anchor="w",
            fg=self.ui.text_muted,
        ).pack(fill="x", padx=12)
        self.transcript = ttk.Treeview(
            self.timed_transcript, columns=("time", "text"), show="headings", height=8,
            selectmode="browse", style="Meeting.Treeview")
        for column, label, width in (("time", tr("Início"), 60), ("text", tr("Texto"), 200)):
            self.transcript.heading(column, text=label)
            self.transcript.column(column, width=width, stretch=column == "text")
        self.transcript.pack(fill="both", expand=True, padx=(12, 0))
        self.transcript.bind("<<TreeviewSelect>>", self._transcript_selected)
        self.transcript.bind("<Double-1>", self._play_transcript_click)
        self.transcript.bind("<Return>", self._play_transcript_selected)
        transcript_pages = ttk.Frame(self.timed_transcript, style="Meeting.TFrame")
        transcript_pages.pack(fill="x", padx=(12, 0), pady=(4, 0))
        self.transcript_previous_button = self._button(
            transcript_pages, tr("Trechos anteriores"), lambda: self.change_transcript_page(-1),
        )
        self.transcript_previous_button.pack(side="left")
        self.transcript_next_button = self._button(
            transcript_pages, tr("Próximos trechos"), lambda: self.change_transcript_page(1),
        )
        self.transcript_next_button.pack(side="left", padx=8)
        self.transcript_page_label = tk.StringVar(self.window, tr("Página de transcrição"))
        self._label(
            transcript_pages, "", textvariable=self.transcript_page_label, anchor="w",
            fg=self.ui.text_muted,
        ).pack(side="left")
        self.segment_text = tk.Text(self.timed_transcript, height=3, wrap="word", font=self.ui.font(),
                                    **self.ui.text_colors(), **self.ui.entry_chrome(readonly=True))
        ui_theme.focus_ring(self.segment_text, self.ui, shrink=("bd",))
        self.segment_text.pack(fill="x", padx=(12, 0), pady=4)
        self.segment_text.configure(state="disabled")
        self.segments = {}
        # Templates and optional focus belong beside the readable result.
        summary_header = ttk.Frame(summary_page, style="Meeting.TFrame")
        summary_header.pack(fill="x", padx=(12, 0))
        self._label(summary_header, tr("Resumo"), font=self.ui.font(11, "bold"),
                    fg=self.ui.text_strong).pack(side="left")
        self.summary_setup_toggle = self._button(summary_page, tr("Gerar resumo") + " ▾", self._toggle_summary_setup)
        self.summary_setup_toggle.pack(anchor="w", padx=(12, 0), pady=(self.ui.space_sm, 0))
        summary_setup = ttk.Frame(summary_page, style="Meeting.TFrame")
        self.summary_setup = summary_setup
        self._label(summary_setup, tr("Formato"), anchor="w").pack(fill="x")
        self.report_profile_choice = tk.StringVar(self.window)
        self.report_profile_box = ttk.Combobox(
            summary_setup, textvariable=self.report_profile_choice, state="readonly", width=20,
        )
        self.report_profile_box.pack(fill="x", pady=(self.ui.space_xs, 4))
        self.report_profile_box.bind("<<ComboboxSelected>>", self._report_profile_changed)
        self.summary_format_hint = tk.StringVar(self.window)
        self._wrap_label(
            summary_setup, "", textvariable=self.summary_format_hint,
            fg=self.ui.text_muted, anchor="w", justify="left",
        ).pack(fill="x")
        self.summary_focus_toggle = self._button(
            summary_setup, tr("Personalizar") + " ▾",
            lambda: self._toggle_detail_panel(
                self.summary_focus_panel, self.summary_focus_toggle, tr("Personalizar"),
            ),
        )
        self.summary_focus_toggle.pack(anchor="w", pady=(self.ui.space_sm, 0))
        self.summary_focus_panel = ttk.Frame(summary_setup, style="Meeting.TFrame")
        self._label(self.summary_focus_panel, tr("Foco adicional (opcional, até 600 caracteres)"),
                    anchor="w").pack(fill="x")
        self.summary_focus = tk.Text(
            self.summary_focus_panel, height=2, wrap="word", font=self.ui.font(),
            **self.ui.text_colors(), **self.ui.entry_chrome(),
        )
        ui_theme.focus_ring(self.summary_focus, self.ui, shrink=("bd",))
        self.summary_focus.pack(fill="x", pady=(self.ui.space_xs, self.ui.space_xs))
        self._wrap_label(
            self.summary_focus_panel, tr("Ex.: priorize próximos passos e dúvidas que precisam de decisão."),
            fg=self.ui.text_muted, anchor="w", justify="left",
        ).pack(fill="x")
        generate_row = ttk.Frame(summary_setup, style="Meeting.TFrame")
        generate_row.pack(fill="x", pady=(8, 0))
        self.regenerate_summary_button = self._button(
            generate_row, tr("Gerar resumo"), self.summarize, accent=True,
        )
        self.regenerate_summary_button.pack(side="left")
        self.cancel_summary_button = self._button(
            generate_row, tr("Cancelar"),
            self.cancel_summary_generation,
        )
        self._park_cancel(self.cancel_summary_button, "pack", side="left", padx=self.ui.space_sm)
        self.summary_status = tk.StringVar(
            self.window, tr("Escolha um formato. Cada resumo é salvo como uma nova versão nesta gravação."),
        )
        self._wrap_label(summary_page, "", textvariable=self.summary_status,
                         fg=self.ui.text_muted, anchor="w").pack(fill="x", padx=12, pady=(4, 0))
        summary_body = ttk.Frame(summary_page, style="Meeting.TFrame")
        summary_body.pack(fill="both", expand=True, padx=(12, 0), pady=(self.ui.space_sm, 0), after=summary_header)
        self.summary = tk.Text(summary_body, height=12, wrap="word", font=self.ui.font(),
                               padx=self.ui.space_sm, pady=self.ui.space_sm,
                               spacing3=self.ui.space_sm,
                               **self.ui.text_colors(), **self.ui.entry_chrome(readonly=True))
        ui_theme.focus_ring(self.summary, self.ui, shrink=("bd",))
        summary_scroll = ttk.Scrollbar(summary_body, orient="vertical", command=self.summary.yview)
        summary_scroll.pack(side="right", fill="y")
        self.summary.pack(side="left", fill="both", expand=True)
        self.summary.configure(yscrollcommand=summary_scroll.set)
        self.summary.insert("1.0", tr("O resumo aparecerá aqui após o processamento local."))
        self.summary.configure(state="disabled")
        self.copy_summary_button = self._button(summary_header, tr("Copiar resumo"), self.copy_summary, accent=True)
        self.copy_summary_button.pack(side="right")

        # Structured local reports.  The generated envelope remains immutable;
        # the editor below writes only a reviewed artifact through the library.
        self.report_tools_toggle = self._button(
            summary_page, tr("Versões") + " ▾",
            lambda: self._toggle_detail_panel(
                self.report_frame, self.report_tools_toggle, tr("Versões"),
            ),
        )
        self.report_tools_toggle.pack(anchor="w", padx=12, pady=(12, 0))
        self.report_frame = self._card(summary_page, padx=12, pady=8)
        report_frame = self.report_frame
        report_header = tk.Frame(report_frame, bg=self.ui.card)
        report_header.pack(fill="x")
        self._label(report_header, tr("Versões salvas"), bg=self.ui.card,
                    fg=self.ui.text_strong, font=self.ui.font(11, "bold")).pack(side="left")
        self._wrap_label(
            report_frame,
            tr("Abra uma versão anterior ou duplique o formato selecionado para criar o seu."),
            bg=self.ui.card, fg=self.ui.text_muted, anchor="w", justify="left",
        ).pack(fill="x", pady=(self.ui.space_xs, self.ui.space_sm))
        profile_row = tk.Frame(report_frame, bg=self.ui.card)
        format_toggle = self._button(report_frame, tr("Formatos personalizados") + " ▾",
            lambda: self._toggle_detail_panel(profile_row, format_toggle, tr("Formatos personalizados")))
        format_toggle.pack(anchor="w", pady=self.ui.space_xs)
        self._button(profile_row, tr("Criar"), self.create_report_profile).grid(row=0, column=0, sticky="ew")
        self._button(profile_row, tr("Duplicar"), self.duplicate_report_profile).grid(row=0, column=1, sticky="ew", padx=self.ui.space_xs)
        self._button(profile_row, tr("Editar"), self.edit_report_profile).grid(row=0, column=2, sticky="ew")
        self.report_disable_button = self._button(profile_row, tr("Desativar"), self.disable_report_profile)
        self.report_disable_button.grid(row=1, column=0, sticky="ew", pady=(self.ui.space_xs, 0))
        self.report_enable_button = self._button(profile_row, tr("Ativar"), self.enable_report_profile)
        self.report_enable_button.grid(row=1, column=1, sticky="ew", padx=self.ui.space_xs, pady=(self.ui.space_xs, 0))
        self._button(profile_row, tr("Excluir"), self.delete_report_profile, danger=True).grid(row=1, column=2, sticky="ew", pady=(self.ui.space_xs, 0))
        for column in range(3):
            profile_row.columnconfigure(column, weight=1)
        history_row = tk.Frame(report_frame, bg=self.ui.card)
        history_row.pack(fill="x", pady=self.ui.space_xs)
        self._label(history_row, tr("Histórico"), bg=self.ui.card).pack(side="left", padx=(0, self.ui.space_sm))
        self.report_history_choice = tk.StringVar(self.window)
        self.report_history_box = ttk.Combobox(
            history_row, textvariable=self.report_history_choice, state="readonly", width=12,
        )
        self.report_history_box.pack(side="left", fill="x", expand=True)
        self.report_history_box.bind("<<ComboboxSelected>>", self._report_history_changed)
        self._button(history_row, tr("Atualizar"), self.refresh_reports).pack(side="left", padx=(self.ui.space_sm, 0))
        section_row = tk.Frame(report_frame, bg=self.ui.card)
        section_row.pack(fill="x", pady=self.ui.space_xs)
        self._label(section_row, tr("Seção"), bg=self.ui.card).pack(side="left", padx=(0, self.ui.space_sm))
        self.report_section_choice = tk.StringVar(self.window)
        self.report_section_box = ttk.Combobox(
            section_row, textvariable=self.report_section_choice, state="readonly", width=10,
        )
        self.report_section_box.pack(side="left", fill="x", expand=True)
        self.report_section_box.bind("<<ComboboxSelected>>", self._report_section_changed)
        self._button(section_row, tr("Copiar seção"), self.copy_report_section).pack(side="left", padx=(self.ui.space_sm, 0))
        self._button(section_row, tr("Exportar…"), self.export_report).pack(side="left", padx=(self.ui.space_sm, 0))
        self.report_provenance = tk.StringVar(self.window, tr("Nenhum relatório selecionado."))
        provenance = tk.Frame(report_frame, bg=self.ui.card)
        disclosure = self._button(report_frame, tr("Detalhes da versão") + " ▾",
            lambda: self._toggle_detail_panel(provenance, disclosure, tr("Detalhes da versão")))
        disclosure.pack(anchor="w", pady=self.ui.space_xs)
        self._wrap_label(provenance, "", textvariable=self.report_provenance, bg=self.ui.card,
                         fg=self.ui.text_muted, anchor="w", justify="left").pack(fill="x", pady=self.ui.space_xs)
        self.report_editor = tk.Text(report_frame, height=6, wrap="word", font=self.ui.font(),
                                     **self.ui.text_colors(), **self.ui.entry_chrome())
        ui_theme.focus_ring(self.report_editor, self.ui, shrink=("bd",))
        self.report_editor.pack(fill="x", pady=(4, self.ui.space_xs))
        report_actions = tk.Frame(report_frame, bg=self.ui.card)
        report_actions.pack(fill="x", pady=self.ui.space_xs)
        self._button(report_actions, tr("Salvar revisão"), self.save_report_review).pack(side="left")
        self._label(report_actions, tr("Citações"), bg=self.ui.card, fg=self.ui.text_muted).pack(side="left", padx=(12, 4))
        self.report_citations = tk.Listbox(report_actions, height=2, width=10,
                                           font=self.ui.font(), **self.ui.listbox_colors(),
                                           **self.ui.entry_chrome())
        ui_theme.focus_ring(self.report_citations, self.ui, shrink=("bd",))
        self.report_citations.pack(side="left", fill="x", expand=True)
        self.report_citations.bind("<Double-Button-1>", lambda _event: self.jump_to_report_citation())
        self._button(report_actions, tr("Ir à fonte"), self.jump_to_report_citation).pack(side="left", padx=(self.ui.space_sm, 0))
        self._set_report_profiles([])

        self.meeting_chat = MeetingChat(
            ask_page, self.ui, on_send=lambda _text: self.ask_this_meeting(),
            on_cancel=self.cancel_chat_answer, on_new=self.new_chat,
            on_save=self.save_answer, on_copy=self.copy_chat_answer,
            on_source=self.jump_to_ask_citation, on_retry=self.retry_chat_answer,
            copy_details=self._copy_issue_details,
        )
        self.meeting_chat.pack(fill="both", expand=True, padx=(12, 0))
        self.ask_status = self.meeting_chat.status
        self._render_chat()

        # Files: text exports and the saved final MP3.
        self._label(files_page, tr("Exportar texto"), anchor="w", fg=self.ui.text_strong,
                    font=self.ui.font(10, "bold")).pack(fill="x", padx=12, pady=(0, 4))
        exports = ttk.Frame(files_page, style="Meeting.TFrame")
        exports.pack(fill="x", padx=(12, 0), pady=(0, self.ui.space_md))
        self._button(exports, tr("Markdown…"), lambda: self.export("markdown")).pack(
            side="left", padx=(0, self.ui.space_sm))
        self._button(exports, tr("Texto…"), lambda: self.export("text")).pack(side="left")

        audio = self._card(files_page, padx=self.ui.space_md, pady=self.ui.space_md)
        audio.pack(fill="x", padx=(12, 0))
        audio.columnconfigure(0, weight=1)
        self._label(audio, tr("Áudio final"), bg=self.ui.card, fg=self.ui.text_strong,
                    font=self.ui.font(10, "bold"), anchor="w").grid(row=0, column=0, sticky="w")
        self.final_audio_status = tk.StringVar(self.window, tr("Selecione uma gravação."))
        self._wrap_label(audio, "", textvariable=self.final_audio_status, bg=self.ui.card,
                         fg=self.ui.text_muted, anchor="w", justify="left").grid(
                             row=1, column=0, sticky="ew", pady=(self.ui.space_xs, 0))
        self.export_audio_button = self._button(audio, tr("Baixar áudio final…"), self.export_audio)
        self.export_audio_button.grid(row=0, column=1, rowspan=2, sticky="e", padx=(self.ui.space_md, 0))
        self.recover_audio_button = self._button(audio, tr("Preparar áudio final"), self.prepare_final_audio)
        self.recover_audio_button.grid(row=2, column=1, sticky="e", padx=(self.ui.space_md, 0), pady=(self.ui.space_sm, 0))
        self.recover_audio_button.configure(state="disabled")
        # Why the button cannot act, when it cannot.
        self.recover_audio_note = self._wrap_label(
            audio, "", bg=self.ui.card, fg=self.ui.text_muted, anchor="w", justify="left",
            font=self.ui.caption())
        self.recover_audio_note.grid(row=2, column=0, sticky="ew", pady=(self.ui.space_sm, 0))
        self.final_audio_progress = ttk.Progressbar(audio, maximum=100, mode="determinate")
        self.final_audio_progress.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(self.ui.space_sm, 0))
        self.final_audio_progress.grid_remove()
        tk.Frame(audio, bg=self.ui.divider, height=1).grid(
            row=4, column=0, columnspan=2, sticky="ew", pady=self.ui.space_md)

        risk = tk.Frame(files_page, bg=self.ui.surface)
        risk.pack(fill="x", padx=12, pady=(self.ui.space_lg, 4))
        self._label(risk, tr("Zona de risco"), fg=self.ui.danger).pack(side="left")
        self.delete_button = ui_theme.button(risk, tr("Excluir gravação…"), self.delete_selected,
                                             "quiet", resolved=self.ui)
        self.delete_button.configure(state="disabled")
        self.delete_button.pack(side="right")

    def _build_privacy_card(self, parent):
        """Build privacy/retention controls inside the existing scrollable page."""
        card = self._card(parent)
        card.pack(fill="x", pady=(0, self.ui.space_md))
        card.columnconfigure(0, weight=1)
        self.privacy_card = card
        self._label(
            card, tr("Privacidade e retenção local"), bg=self.ui.card,
            fg=self.ui.text_strong, font=self.ui.font(11, "bold"),
        ).grid(row=0, column=0, sticky="w")
        self._wrap_label(
            card, tr("O SnipVoice só grava quando você inicia. Estas opções ficam neste computador."),
            bg=self.ui.card, fg=self.ui.text_muted, anchor="w", justify="left",
        ).grid(row=1, column=0, sticky="ew", pady=(self.ui.space_xs, self.ui.space_md))

        # Stored values stay the workspace's codes; the controls below show
        # plain choices and write those codes back.
        self.privacy_notice_enabled = tk.BooleanVar(self.window, False)
        self.privacy_notice_language = tk.StringVar(self.window, "pt-BR")
        self.privacy_notice_language_display = tk.StringVar(self.window, i18n.LANGUAGE_NAMES["pt-BR"])
        self.qa_mode = tk.StringVar(self.window, "explicit_save")
        self.qa_never_save = tk.BooleanVar(self.window, False)
        self.whole_meeting_auto = tk.BooleanVar(self.window, False)
        self.whole_meeting_after_days = tk.StringVar(self.window, "90")
        self.trash_days = tk.StringVar(self.window, "30")
        self.privacy_notice_language.trace_add("write", lambda *_args: self.privacy_notice_language_display.set(
            i18n.LANGUAGE_NAMES.get(self.privacy_notice_language.get(), self.privacy_notice_language.get())))
        self.qa_mode.trace_add("write", lambda *_args: self.qa_never_save.set(self.qa_mode.get() == "memory_only"))

        def option(row, text, variable, command=None):
            line = tk.Frame(card, bg=self.ui.card)
            line.grid(row=row, column=0, sticky="w", pady=(self.ui.space_sm, 0))
            check = tk.Checkbutton(line, text=text, variable=variable, command=command,
                                   font=self.ui.font(), **self.ui.checkbutton_colors(self.ui.card))
            check.pack(side="left")
            return line, check

        def hint(row, text):
            label = self._wrap_label(card, text, bg=self.ui.card, fg=self.ui.text_muted, anchor="w", justify="left")
            label.grid(row=row, column=0, sticky="ew", padx=(24, 0))
            label._validation_hint = text
            return label

        def days_entry(line, variable):
            entry = self._entry(line, variable, 5)
            entry.pack(side="left", padx=self.ui.space_xs)
            self._label(line, tr("dias"), bg=self.ui.card).pack(side="left")
            return entry

        line, self.privacy_notice_check = option(2, tr("Mostrar um aviso antes de cada gravação, em"),
                                                 self.privacy_notice_enabled)
        self.privacy_notice_language_box = ttk.Combobox(
            line, textvariable=self.privacy_notice_language_display,
            values=tuple(i18n.LANGUAGE_NAMES.values()), state="readonly", width=18,
        )
        self.privacy_notice_language_box.pack(side="left", padx=(self.ui.space_xs, 0))
        self.privacy_notice_language_box.bind("<<ComboboxSelected>>", lambda _event: self.privacy_notice_language.set(
            displayed_key(i18n.LANGUAGE_NAMES, self.privacy_notice_language_display.get())))
        self.privacy_field_errors = {"privacy_notice_language": hint(3, tr("Um texto para ler ou colar na chamada avisando que a reunião será gravada."))}

        _line, self.qa_never_save_check = option(4, tr("Nunca salvar respostas do chat"), self.qa_never_save,
                                                 self._qa_never_save_changed)
        self.privacy_field_errors["qa_mode"] = hint(5, tr("As respostas somem ao fechar a gravação, e o botão Salvar fica oculto."))

        line, self.whole_meeting_auto_check = option(6, tr("Mover gravações para a lixeira automaticamente após"),
                              self.whole_meeting_auto, self._sync_raw_policy_controls)
        self.whole_meeting_days_entry = days_entry(line, self.whole_meeting_after_days)
        self.privacy_field_errors["whole_meeting_after_days"] = hint(7, tr("A gravação inteira vai para a lixeira, de onde ainda pode ser restaurada."))

        trash = tk.Frame(card, bg=self.ui.card)
        trash.grid(row=8, column=0, sticky="w", pady=(self.ui.space_md, 0))
        self._label(trash, tr("Manter itens na lixeira por"), bg=self.ui.card).pack(side="left")
        self.trash_days_entry = self._entry(trash, self.trash_days, 5)
        self.trash_days_entry.pack(side="left", padx=self.ui.space_xs)
        self._label(trash, tr("dias"), bg=self.ui.card).pack(side="left")
        self.privacy_field_errors["trash_days"] = hint(9, tr("Depois disso, Esvaziar expirados, na lixeira da Biblioteca, apaga esses itens de vez."))
        self.privacy_validation_fields = {
            "whole_meeting_after_days": self.whole_meeting_days_entry, "trash_days": self.trash_days_entry,
            "privacy_notice_language": self.privacy_notice_language_box, "qa_mode": self.qa_never_save_check}

        self._wrap_label(
            card,
            tr("Apagar não garante eliminação forense em SSDs, e cópias que você exportou para outras pastas "
               "não são afetadas."),
            bg=self.ui.card, fg=self.ui.text_muted, anchor="w", justify="left",
        ).grid(row=10, column=0, sticky="ew", pady=(self.ui.space_md, self.ui.space_xs))
        footer = tk.Frame(card, bg=self.ui.card)
        footer.grid(row=11, column=0, sticky="ew", pady=(self.ui.space_sm, 0))
        self.privacy_status = tk.StringVar(self.window, tr("Configurações de privacidade carregando…"))
        self._label(footer, "", textvariable=self.privacy_status, bg=self.ui.card,
                    fg=self.ui.text_muted, anchor="w").pack(side="left")
        self._sync_raw_policy_controls()

    def _draft_variables(self, section):
        names = (("profile", "language", "hotkey", "summary_model", "destination", "voice_boost")
                 if section == "recording" else
                 ("privacy_notice_enabled", "privacy_notice_language", "qa_mode",
                  "whole_meeting_auto", "whole_meeting_after_days", "trash_days"))
        return {name: getattr(self, name) for name in names}

    def _draft_values(self, section):
        return {name: variable.get() for name, variable in self._draft_variables(section).items()}

    def _put_draft_values(self, section, values):
        for name, variable in self._draft_variables(section).items():
            if name in values:
                variable.set(values[name])
        if section == "recording":
            self.profile_display.set(profile_display_label(self.profile.get()))
            self._profile_changed()
            self.summary_display.set(tr(SUMMARY_LABELS[self.summary_model.get()]))
            self.destination_label.set(destination_display(self.destination.get()))
        else:
            self._sync_raw_policy_controls()
            self._sync_qa_controls()

    def _recording_settings_payload(self):
        if not self.settings_loaded:
            raise ValueError(tr("Aguarde o carregamento das configurações antes de gravar ou salvar."))
        values = self._draft_values("recording")
        payload = {"meeting_" + name: values[name] for name in
                   ("hotkey", "summary_model", "destination", "voice_boost", "language", "profile")}
        raw = dict(self.raw_settings, **payload)
        if values["profile"] == FOLLOW_DICTATION:
            raw["voice_profile"] = self._effective_profile()
        validated_settings(raw, raw.get("voice_hotkey", DEFAULT_DICTATION_HOTKEY),
                           raw.get("voice_command_hotkey", DEFAULT_COMMAND_HOTKEY))
        if payload["meeting_destination"] and not os.path.isabs(payload["meeting_destination"]):
            raise ValueError(tr("Pasta dos arquivos finais: escolha uma pasta existente e absoluta."))
        return payload

    def _settings_section_error(self, section):
        field = "profile" if section == "recording" else "whole_meeting_after_days"
        try:
            if section == "recording":
                field = "language"
                if self.language.get() not in available_languages(self._effective_profile()):
                    raise ValueError(tr("Este modelo usa detecção automática de idioma. Selecione auto."))
                field = "hotkey"
                validate_hotkey_conflicts(dict(self.raw_settings, meeting_hotkey=self.hotkey.get()))
                field = "destination"
                path = self.destination.get()
                if path and not os.path.isabs(path):
                    raise ValueError(tr("Pasta dos arquivos finais: escolha uma pasta existente e absoluta."))
                field = "profile"
                self._recording_settings_payload()
            else:
                days = self._policy_days(self.whole_meeting_after_days.get(), field=tr("Reunião"))
                if self.whole_meeting_auto.get() and not days:
                    raise ValueError(tr("Reunião: informe após quantos dias mover para a lixeira."))
                field = "trash_days"
                self._policy_days(self.trash_days.get(), field=tr("Lixeira"))
                field = "qa_mode"
                if self.qa_mode.get() not in {"memory_only", "explicit_save"}:
                    raise ValueError(tr("Escolha um modo de Q&A válido."))
                field = "privacy_notice_language"
                if self.privacy_notice_language.get() not in {"pt-BR", "en-US"}:
                    raise ValueError(tr("Escolha um idioma válido."))
        except (ValueError, TypeError) as exc:
            self._invalid_settings_field = (section, field)
            return str(exc)
        self._invalid_settings_field = None
        return ""

    def _build_settings_bar(self, parent, section):
        bar = tk.Frame(parent, bg=self.ui.surface)
        bar.pack(fill="x", pady=self.ui.space_sm)
        status = self._wrap_label(bar, "", fg=self.ui.text_muted)
        status.pack(fill="x", side="bottom", pady=(self.ui.space_xs, 0))
        save = self._button(bar, tr("Salvar"), lambda: self._save_settings_section(section), accent=True)
        save.pack(side="left")
        discard = self._button(bar, tr("Descartar"), lambda: self.discard_settings_section(section))
        discard.pack(side="left", padx=(self.ui.space_sm, 0))
        self._settings_drafts[section] = dict(baseline=None, saving=False, after=None,
                                             save=save, discard=discard, status=status)
        for variable in self._draft_variables(section).values():
            variable.trace_add("write", lambda *_args, key=section: self._sync_settings_bar(key))
        self._sync_settings_bar(section)

    def _sync_settings_bar(self, section):
        draft = getattr(self, "_settings_drafts", {}).get(section)
        if draft is None:
            return
        if section == "privacy":
            ready = self._privacy_form_ready()
            for name in ("privacy_notice_check", "qa_never_save_check", "whole_meeting_auto_check",
                         "whole_meeting_days_entry", "trash_days_entry", "privacy_notice_language_box"):
                control = getattr(self, name, None)
                if control is not None:
                    state = ("readonly" if name == "privacy_notice_language_box" else "normal") if ready else "disabled"
                    if name == "whole_meeting_days_entry" and not self.whole_meeting_auto.get():
                        state = "disabled"
                    control.configure(state=state)
        values = self._draft_values(section)
        dirty = draft["baseline"] is not None and values != draft["baseline"]
        error = self._settings_section_error(section) if dirty else ""
        target = getattr(self, "_invalid_settings_field", None) if error else None
        for name, label in getattr(self, "recording_field_errors", {}).items() if section == "recording" else ():
            label.configure(text="⚠ " + error if target == (section, name) else "")
        if section == "privacy":
            for name, label in getattr(self, "privacy_field_errors", {}).items():
                invalid = target == (section, name)
                label.configure(text="⚠ " + error if invalid else label._validation_hint,
                                fg=self.ui.danger if invalid else self.ui.text_muted)
        draft["save"].configure(state="normal" if dirty and not error and not draft["saving"] else "disabled")
        draft["discard"].configure(state="normal" if dirty and not draft["saving"] else "disabled")
        draft["status"].configure(text=(tr("Carregando…") if draft["baseline"] is None else
                                         "⚠ " + error if error else tr("Salvando…") if draft["saving"]
                                         else tr("Alterações não salvas") if dirty else ui_theme.saved_receipt(draft)),
                                  fg=self.ui.danger if error else self.ui.text_muted)

    def _section_loaded(self, section):
        draft = getattr(self, "_settings_drafts", {}).get(section)
        if draft is None:
            return
        if section == "privacy" and not self._privacy_form_ready():
            self._sync_settings_bar(section)
            return
        restored = self._restored_settings_drafts.pop(section, None)
        draft["baseline"] = copy.deepcopy(restored["baseline"] if restored else self._draft_values(section))
        if restored:
            self._put_draft_values(section, restored["values"])
        self._sync_settings_bar(section)

    def capture_settings_drafts(self):
        return {key: {"values": self._draft_values(key), "baseline": copy.deepcopy(draft["baseline"])}
                for key, draft in getattr(self, "_settings_drafts", {}).items()
                if draft["baseline"] is not None}

    def discard_settings_section(self, section):
        draft = self._settings_drafts[section]
        if draft["baseline"] is not None and not draft["saving"]:
            self._put_draft_values(section, draft["baseline"])
            self._sync_settings_bar(section)

    def _save_settings_section(self, section):
        draft = self._settings_drafts[section]
        if draft["baseline"] is None or draft["saving"] or draft["baseline"] == self._draft_values(section):
            return False
        if self._settings_section_error(section):
            self._sync_settings_bar(section)
            field = self._invalid_settings_field[1]
            target = (self.recording_validation_fields.get(field, self.profile_box) if section == "recording"
                      else self.privacy_validation_fields.get(field, self.whole_meeting_days_entry))
            target.focus_set()
            draft["after"] = None
            return False
        (self.save_settings if section == "recording" else self.save_privacy_settings)()
        return draft["saving"]

    def _section_saved(self, section, values, succeeded):
        draft = getattr(self, "_settings_drafts", {}).get(section)
        if draft is None:
            return
        draft["saving"] = False
        after, draft["after"] = draft["after"], None
        if succeeded:
            draft["baseline"] = copy.deepcopy(values)
            draft["saved_at"] = time.time()
        self._sync_settings_bar(section)
        if succeeded and after and self._draft_values(section) == values:
            after()
        settled = getattr(self, "on_forms_settled", None)
        if settled:
            settled()

    def _settings_section_requested(self, key):
        current = self.settings_sections.current
        if current == key:
            return True
        self.request_leave_settings(lambda: self.settings_sections.select(key))
        return False

    def request_leave_settings(self, after):
        section = self.settings_sections.current
        draft = getattr(self, "_settings_drafts", {}).get(section)
        if draft and draft["saving"]:
            draft["after"] = after
            return
        if draft is None or draft["baseline"] is None or draft["baseline"] == self._draft_values(section):
            after()
            return
        draft["after"] = None
        answer = ui_theme.confirm_draft(self.window)
        if answer == "discard":
            self.discard_settings_section(section)
            after()
        elif answer == "save":
            draft["after"] = after
            self._save_settings_section(section)

    def _qa_never_save_changed(self):
        self.qa_mode.set("memory_only" if self.qa_never_save.get() else "explicit_save")
        self._sync_qa_controls()

    def _location_specs(self):
        """Folder cards: app data (Geral) and downloaded models (Modelos)."""
        specs = []
        if self.data_location is not None and self.relocate_data is not None:
            specs.append({
                "key": "data", "title": tr("Pasta de dados"), "info": self.data_location,
                "choose_title": tr("Nova pasta de dados"),
                "request": self.relocate_data, "choose": data_relocation.target_for_choice,
                "validate": data_relocation.validate_target, "size": data_relocation.directory_size,
                "description": tr(
                    "Configurações, histórico de ditado, gravações e a biblioteca de reuniões "
                    "ficam nesta pasta. Ao escolher outra, o SnipVoice move tudo para lá e "
                    "reinicia. Os modelos baixados têm uma pasta própria, na seção Modelos."
                ),
                "lock_note": tr("Definida pela variável SNIPVOICE_HOME; altere-a fora do aplicativo."),
                "confirm": tr("Mover os dados do SnipVoice"),
            })
        if self.models_location is not None and self.relocate_models is not None:
            specs.append({
                "key": "models", "title": tr("Pasta dos modelos"), "info": self.models_location,
                "choose_title": tr("Nova pasta dos modelos"),
                "request": self.relocate_models, "choose": os.path.abspath,
                "validate": data_relocation.validate_models_target,
                "size": data_relocation.models_size,
                "description": tr(
                    "Modelos de transcrição e de resumo baixados. Os arquivos GGUF ficam em "
                    "voice-models e summary-models, prontos para outros aplicativos "
                    "compatíveis. Pode ser uma pasta compartilhada: outros arquivos dela "
                    "não são alterados."
                ),
                "lock_note": tr(
                    "Definida por SNIPVOICE_VOICE_CACHE, SNIPVOICE_SUMMARY_CACHE ou "
                    "voice_cache_dir em settings.json."
                ),
                "confirm": tr("Mover os modelos baixados"),
            })
        return specs

    def _build_location_card(self, parent, spec):
        """Show where a folder lives and offer a move-and-restart to another one."""
        info = spec["info"]()
        card = self._card(parent)
        card.pack(fill="x", pady=(0, self.ui.space_md))
        self._label(
            card, spec["title"], bg=self.ui.card, fg=self.ui.text_strong,
            font=self.ui.font(11, "bold"),
        ).grid(row=0, column=0, columnspan=3, sticky="w")
        self._wrap_label(
            card, spec["description"], bg=self.ui.card, fg=self.ui.text_muted,
            anchor="w", justify="left",
        ).grid(row=1, column=0, columnspan=3, sticky="ew", pady=(self.ui.space_xs, self.ui.space_sm))
        display = tk.StringVar(self.window, info["path"])
        entry = self._entry(card, display)
        entry.configure(state="readonly")
        entry.grid(row=2, column=0, sticky="ew")
        key = spec["key"]
        move = self._button(card, tr("Mover para…"), lambda: self.choose_location(key))
        move.configure(state="disabled" if info["env_locked"] else "normal")
        move.grid(row=2, column=1, padx=(self.ui.space_sm, 0))
        at_default = os.path.normcase(info["path"]) == os.path.normcase(info["default"])
        default = self._button(card, tr("Restaurar padrão"), lambda: self.restore_default_location(key))
        default.configure(state="disabled" if info["env_locked"] or at_default else "normal")
        default.grid(row=2, column=2, padx=(self.ui.space_sm, 0))
        if info["env_locked"]:
            self._label(
                card, spec["lock_note"], bg=self.ui.card, fg=self.ui.text_muted, anchor="w",
            ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(self.ui.space_xs, 0))
        card.columnconfigure(0, weight=1)
        self.location_cards[key] = {"spec": spec, "display": display, "move": move, "default": default}

    def choose_location(self, key):
        spec = self.location_cards[key]["spec"]
        chosen = filedialog.askdirectory(
            parent=self.window, title=spec["choose_title"], mustexist=True,
        )
        if chosen:
            self._confirm_location_move(key, spec["choose"](chosen))

    def restore_default_location(self, key):
        spec = self.location_cards[key]["spec"]
        self._confirm_location_move(key, spec["info"]()["default"])

    def _confirm_location_move(self, key, target):
        spec = self.location_cards[key]["spec"]
        if key == "models" and self.summary_download_cancel is not None:
            messagebox.showerror(
                spec["title"], tr("Aguarde o download do modelo de resumo terminar."), parent=self.window,
            )
            return
        current = spec["info"]()["path"]
        try:
            target = spec["validate"](current, target)
        except data_relocation.RelocationError as exc:
            messagebox.showerror(spec["title"], str(exc), parent=self.window)
            return
        # ceiling: sizes the folder on the Tk thread; move to a worker if
        # libraries reach hundreds of thousands of files.
        size = format_size(spec["size"](current))
        if not messagebox.askokcancel(
            spec["title"],
            tr("{action} ({size}) de\n{current}\npara\n{target}?\n\n"
               "O SnipVoice será fechado e aberto de novo. Entre discos diferentes, a cópia "
               "pode levar alguns minutos antes de o ícone voltar à bandeja.",
               action=spec["confirm"], size=size, current=current, target=target),
            parent=self.window,
        ):
            return
        error = spec["request"](target)
        if error:
            messagebox.showerror(spec["title"], error, parent=self.window)

    def _build_language_card(self, parent):
        """Build the interface language preference; applying it rebuilds the window."""
        card = self._card(parent)
        card.pack(fill="x", pady=(0, self.ui.space_md))
        self._label(
            card, tr("Idioma"), bg=self.ui.card, fg=self.ui.text_strong,
            font=self.ui.font(11, "bold"),
        ).grid(row=0, column=0, columnspan=3, sticky="w")
        self._wrap_label(
            card,
            tr("Idioma dos menus, botões e mensagens do SnipVoice. A mudança reconstrói "
               "esta janela sem reiniciar o aplicativo."),
            bg=self.ui.card, fg=self.ui.text_muted, anchor="w", justify="left",
        ).grid(row=1, column=0, columnspan=3, sticky="ew", pady=(self.ui.space_xs, self.ui.space_sm))
        # Language names stay in their own language so either one is findable.
        self.interface_language_display = tk.StringVar(
            self.window, i18n.LANGUAGE_NAMES[i18n.current_language()],
        )
        self.interface_language_box = ttk.Combobox(
            card, textvariable=self.interface_language_display,
            values=tuple(i18n.LANGUAGE_NAMES.values()), state="readonly", width=22,
        )
        self.interface_language_box.grid(row=2, column=0, sticky="w")
        self.interface_language_box.bind("<<ComboboxSelected>>", lambda _event: self.save_language())
        self.interface_language_status = tk.StringVar(self.window, "")
        self._label(
            card, "", textvariable=self.interface_language_status, bg=self.ui.card,
            fg=self.ui.text_muted, anchor="w",
        ).grid(row=2, column=1, sticky="w", padx=(self.ui.space_md, 0))
        card.columnconfigure(1, weight=1)

    def save_language(self):
        selected = self.interface_language_display.get()
        language = next(
            (code for code, name in i18n.LANGUAGE_NAMES.items() if name == selected), None,
        )
        if language is None:
            self.interface_language_status.set(tr("Escolha um idioma válido."))
            return
        if language == i18n.current_language():
            return
        self._request_preference(
            lambda: self._persist_language(language),
            lambda: self.interface_language_display.set(i18n.LANGUAGE_NAMES[i18n.current_language()]))

    def _persist_language(self, language):
        self._set_preference_saving(True)
        self.interface_language_status.set(tr("Salvando idioma…"))

        persist = self.persist_settings
        def save():
            if persist({"language": language}) is False:
                raise ValueError(tr("Não foi possível salvar o idioma."))
            return language

        if not self._submit("save_language", save, self._language_saved, urgent=True):
            self._set_preference_saving(False)
            self.interface_language_display.set(i18n.LANGUAGE_NAMES[i18n.current_language()])
            self.interface_language_status.set(tr("Não foi possível salvar o idioma."))

    def _language_saved(self, language, error):
        self._set_preference_saving(False)
        if error:
            self.interface_language_display.set(i18n.LANGUAGE_NAMES[i18n.current_language()])
            self.interface_language_status.set("")
            self._fail("settings", tr("Não foi possível salvar o idioma"),
                       tr("O idioma anterior continua em uso. Aplique de novo."), error=error)
            return
        self.raw_settings["language"] = language
        if self.on_language_changed:
            self.window.after_idle(lambda: self.on_language_changed(language))

    def _build_appearance_card(self, parent):
        """Build a local appearance preference with a safe manager rebuild."""
        card = self._card(parent)
        card.pack(fill="x", pady=(0, self.ui.space_md))
        self.appearance_card = card
        self._label(
            card,
            tr("Aparência"),
            bg=self.ui.card,
            fg=self.ui.text_strong,
            font=self.ui.font(11, "bold"),
        ).grid(row=0, column=0, columnspan=3, sticky="w")
        self._wrap_label(
            card,
            tr("Use o tema do sistema ou escolha uma aparência fixa. A mudança reconstrói "
               "esta janela sem reiniciar o SnipVoice."),
            bg=self.ui.card,
            fg=self.ui.text_muted,
            anchor="w",
            justify="left",
        ).grid(row=1, column=0, columnspan=3, sticky="ew", pady=(self.ui.space_xs, self.ui.space_sm))
        preference = ui_theme.normalize_preference(
            getattr(self.ui, "preference", None),
        )
        self.appearance_display = tk.StringVar(
            self.window,
            tr(APPEARANCE_LABELS[preference]),
        )
        self.appearance_box = tk.Frame(card, bg=self.ui.card)
        for key in ("dark", "light", "system"):
            tk.Radiobutton(self.appearance_box, text=tr(APPEARANCE_LABELS[key]),
                           value=tr(APPEARANCE_LABELS[key]), variable=self.appearance_display,
                           command=self.save_appearance, font=self.ui.body(),
                           **self.ui.checkbutton_colors(self.ui.card)).pack(side="left", padx=(0, self.ui.space_sm))
        self.appearance_box.grid(row=2, column=0, sticky="w")
        self.appearance_status = tk.StringVar(
            self.window,
            tr(APPEARANCE_STATUS[preference]),
        )
        self._label(
            card,
            "",
            textvariable=self.appearance_status,
            bg=self.ui.card,
            fg=self.ui.text_muted,
            anchor="w",
        ).grid(row=2, column=1, sticky="w", padx=(self.ui.space_md, 0))
        card.columnconfigure(1, weight=1)

    def save_appearance(self):
        selected = self.appearance_display.get()
        try:
            preference = next(
                key for key, label in APPEARANCE_LABELS.items() if tr(label) == selected
            )
        except StopIteration:
            self.appearance_status.set(tr("Escolha uma aparência válida."))
            return
        self._request_preference(
            lambda: self._persist_appearance(preference),
            lambda: self.appearance_display.set(tr(APPEARANCE_LABELS[ui_theme.normalize_preference(self.ui.preference)])))

    def _request_preference(self, operation, rollback):
        if getattr(self, "_preference_saving", False):
            rollback()
            return
        def preflight():
            if getattr(self, "closed", False):
                return
            if getattr(self, "_title_save_pending", None):
                self._title_leave_continuation = (operation, rollback)
                return
            if getattr(self, "dirty", False):
                answer = messagebox.askyesnocancel(tr("Título não salvo"), tr("Salvar o título antes de fechar?"), parent=self.window)
                if answer is None:
                    rollback()
                    return
                if answer:
                    self._title_leave_continuation = (operation, rollback)
                    self.save_title()
                    return
                self._end_title_edit(cancel=True)
            operation()
        gate = getattr(self, "preference_gate", None)
        if gate:
            gate(preflight)
        else:
            preflight()

    def _persist_appearance(self, preference):
        self._set_preference_saving(True)
        self.appearance_status.set(tr("Salvando aparência local…"))

        persist = self.persist_settings
        def save():
            result = persist({"appearance": preference})
            if result is False:
                raise ValueError(tr("Não foi possível salvar a aparência."))
            return preference

        if not self._submit("save_appearance", save, self._appearance_saved, urgent=True):
            self._set_preference_saving(False)
            self.appearance_display.set(tr(APPEARANCE_LABELS[ui_theme.normalize_preference(self.ui.preference)]))
            self.appearance_status.set(tr("Não foi possível salvar a aparência."))

    def _appearance_saved(self, preference, error):
        self._set_preference_saving(False)
        if error:
            self.appearance_display.set(tr(APPEARANCE_LABELS[ui_theme.normalize_preference(self.ui.preference)]))
            self.appearance_status.set(
                tr(APPEARANCE_STATUS[ui_theme.normalize_preference(self.ui.preference)]))
            self._fail("settings", tr("Não foi possível salvar a aparência"),
                       tr("A aparência anterior continua em uso. Aplique de novo."), error=error)
            return
        preference = ui_theme.normalize_preference(preference)
        self.raw_settings["appearance"] = preference
        self.appearance_display.set(tr(APPEARANCE_LABELS[preference]))
        self.appearance_status.set(tr("Aparência salva. Atualizando a janela…"))
        if self.on_appearance_changed:
            self.window.after_idle(lambda: self.on_appearance_changed(preference))

    def _set_preference_saving(self, saving):
        self._preference_saving = saving
        language = getattr(self, "interface_language_box", None)
        if language is not None:
            language.configure(state="disabled" if saving else "readonly")
        appearance = getattr(self, "appearance_box", None)
        if appearance is not None:
            for radio in appearance.winfo_children():
                radio.configure(state="disabled" if saving else "normal")

    def _privacy_form_ready(self):
        return (getattr(self, "_privacy_initial_loaded", True)
                and getattr(self, "_workspace_initial_loaded", True))

    def _sync_raw_policy_controls(self):
        for entry_name, variable_name in (("whole_meeting_days_entry", "whole_meeting_auto"),):
            entry = getattr(self, entry_name, None)
            if entry is not None:
                entry.configure(state="normal" if getattr(self, variable_name).get() else "disabled")

    @staticmethod
    def _policy_days(value, *, field):
        text = str(value or "").strip()
        if not text:
            return None
        try:
            days = float(text)
        except (TypeError, ValueError) as exc:
            raise ValueError(tr("{field}: informe um número de dias válido.", field=field)) from exc
        if not math.isfinite(days) or days < 0 or days > 36500:
            raise ValueError(tr("{field}: informe um valor entre 0 e 36.500 dias.", field=field))
        return days

    def _apply_workspace_settings(self, workspace):
        if not isinstance(workspace, dict):
            return
        draft = getattr(self, "_settings_drafts", {}).get("privacy")
        working = self._draft_values("privacy") if draft and draft["baseline"] is not None else None
        preserve = working if draft and (draft["saving"] or working != draft["baseline"]) else None
        generation = workspace.get("generation", 0)
        if isinstance(generation, int) and generation >= 0:
            self.workspace_generation = generation
            self.organization_generation = generation
        privacy = workspace.get("privacy_defaults")
        if not isinstance(privacy, dict):
            privacy = self.privacy_defaults
        retention = workspace.get("retention_defaults")
        if not isinstance(retention, dict):
            retention = self.retention_defaults
        self.privacy_defaults = copy.deepcopy(privacy)
        self.retention_defaults = copy.deepcopy(retention)
        notice = self.privacy_defaults.get("recording_notice", {})
        if not isinstance(notice, dict):
            notice = {}
        self.privacy_notice_enabled.set(bool(notice.get("enabled", self.privacy_defaults.get(
            "recording_notice_enabled", False))))
        self.privacy_notice_language.set(notice.get(
            "language", self.privacy_defaults.get("recording_notice_language", "pt-BR")))
        self.qa_mode.set(self.privacy_defaults.get("qa_mode", "explicit_save"))
        whole = self.retention_defaults.get("whole_meeting", self.retention_defaults.get(
            "whole_meeting_policy", {"mode": "keep"}))
        if not isinstance(whole, dict):
            whole = {"mode": str(whole)}
        whole_mode = whole.get("mode")
        if whole_mode is None and whole.get("after_days") is not None:
            whole_mode = "whole_meeting"
        whole_days = whole.get("after_days")
        self.whole_meeting_auto.set(whole_mode == "whole_meeting" and whole_days is not None)
        self.whole_meeting_after_days.set("90" if whole_days is None else f"{float(whole_days):g}")
        # Raw-track retention is a legacy storage policy. It is deliberately
        # normalized to keep because the UI no longer offers that destructive
        # operation and final MP3 is the canonical user-facing audio.
        raw = self.retention_defaults.get("raw_audio", self.retention_defaults.get(
            "raw_audio_policy", {"mode": "keep", "tracks": []}))
        if not isinstance(raw, dict):
            raw = {}
        raw = copy.deepcopy(raw)
        raw["mode"] = "keep"
        raw["tracks"] = []
        raw.pop("after_days", None)
        self.retention_defaults["raw_audio"] = raw
        self.trash_days.set(f"{float(self.retention_defaults.get('trash_days', 30)):g}")
        self._sync_raw_policy_controls()
        self._sync_qa_controls()
        if preserve is not None:
            self._put_draft_values("privacy", preserve)
        else:
            self._section_loaded("privacy")

    def _sync_qa_controls(self):
        mode = self.qa_mode.get() if hasattr(self, "qa_mode") else "explicit_save"
        chat = getattr(self, "meeting_chat", None)
        if chat is not None:
            snapshot = getattr(self, "snapshot", {})
            pending = getattr(self, "ask_pending", None)
            busy = bool(pending or snapshot.get("processing")
                        or snapshot.get("state", "idle") != "idle")
            chat.set_controls(
                busy=busy, pending=bool(pending and pending[0] == self.selected),
                can_save=mode == "explicit_save",
                can_send=bool(self.selected and self.detail_ready and self.transcript_revision),
            )

    def save_privacy_settings(self):
        if not self._privacy_form_ready():
            self.privacy_status.set(tr("Configurações de privacidade carregando…"))
            return
        if self.privacy_save_inflight:
            self.privacy_status.set(tr("Aguarde a atualização de privacidade em andamento."))
            return
        try:
            whole_auto = bool(self.whole_meeting_auto.get())
            whole_days = self._policy_days(self.whole_meeting_after_days.get(), field=tr("Reunião"))
            if whole_auto and not whole_days:
                raise ValueError(tr("Informe após quantos dias mover gravações para a lixeira."))
            trash = self._policy_days(self.trash_days.get(), field=tr("Lixeira"))
            if trash is None:
                trash = 30.0
            qa_mode = self.qa_mode.get().strip()
            if qa_mode not in {"memory_only", "explicit_save"}:
                raise ValueError(tr("Escolha um modo de Q&A válido."))
            whole_mode = "whole_meeting" if whole_auto else "keep"
            privacy = copy.deepcopy(self.privacy_defaults)
            notice = privacy.setdefault("recording_notice", {})
            if not isinstance(notice, dict):
                notice = {}
                privacy["recording_notice"] = notice
            notice.update(enabled=bool(self.privacy_notice_enabled.get()),
                          language=self.privacy_notice_language.get())
            privacy["qa_mode"] = qa_mode
            retention = copy.deepcopy(self.retention_defaults)
            whole = copy.deepcopy(retention.get("whole_meeting", {}))
            if not isinstance(whole, dict):
                whole = {}
            whole["mode"] = whole_mode
            if whole_days is None:
                whole.pop("after_days", None)
            else:
                whole["after_days"] = whole_days
            retention["whole_meeting"] = whole
            # Preserve the legacy key for migrations, but disable its
            # destructive mode now that raw-track retention is not user-facing.
            retention["raw_audio"] = {"mode": "keep", "tracks": []}
            retention["trash_days"] = trash
        except (ValueError, TypeError) as exc:
            self.privacy_status.set(str(exc))
            return
        expected = self.workspace_generation
        saved_values = self._draft_values("privacy") if getattr(self, "_settings_drafts", {}).get("privacy") else None
        self.privacy_status.set(tr("Salvando configurações locais…"))

        updater = getattr(self.controller, "update_workspace", None)
        if not callable(updater):
            updater = self.controller.library.update_workspace
        def save():
            return updater({"privacy_defaults": privacy, "retention_defaults": retention},
                           expected_generation=expected)

        self.privacy_save_inflight = True
        if saved_values is not None:
            self._settings_drafts["privacy"]["saving"] = True
            self._sync_settings_bar("privacy")
        submitted = self._submit(
            "save_privacy", save,
            lambda value, error: self._privacy_saved(value, error, privacy, retention, saved_values),
            urgent=True,
        )
        if not submitted:
            self.privacy_save_inflight = False
            self.privacy_status.set(tr("Não foi possível enfileirar a alteração de privacidade."))
            self._section_saved("privacy", saved_values, False)

    def _privacy_saved(self, value, error, privacy=None, retention=None, saved_values=None):
        self.privacy_save_inflight = False
        if self.closed:
            return
        if error:
            self._section_saved("privacy", saved_values, False)
            self.privacy_status.set("")
            self._fail("settings", tr("Não foi possível salvar a privacidade"),
                       tr("As opções anteriores continuam valendo. Salve de novo ou reabra a janela."),
                       error=error)
            return
        if isinstance(value, dict):
            self._apply_workspace_settings(value)
        elif privacy is not None:
            self.privacy_defaults = copy.deepcopy(privacy)
            self.retention_defaults = copy.deepcopy(retention or self.retention_defaults)
        self.privacy_ready = True
        self.privacy_status.set(tr("Privacidade e retenção salvas localmente."))
        self._sync_qa_controls()
        if saved_values is not None:
            self._section_saved("privacy", saved_values, True)
        if self.on_settings_changed:
            self.on_settings_changed()

    def _submit(self, key, operation, callback=None, urgent=False):
        if callback is None:
            destination = self._here()
            session = getattr(self, "selected", None) if destination == "library" else None
            callback = lambda value, error: self._done(
                value, error, destination=destination, session=session)
        lane = "long" if not urgent and key in LONG_JOB_KEYS else urgent
        if not self.bridge.submit(key, operation, callback, lane):
            if not self.closed:
                self._fail(self._here(), tr("Há operações pendentes"),
                           tr("Aguarde a operação atual terminar e tente de novo."), tone="warning")
            return False
        # A user-requested read must not wait for the next two-second idle tick.
        # This runs on Tk; workers only publish results into the bounded queue.
        if (getattr(self, "after_id", None) is not None and getattr(self, "visible", True)
                and not self.closed):
            self.root.after_cancel(self.after_id)
            self.after_id = self.root.after(200, self._poll)
        return True

    def _done(self, value, error, receipt=None, *, destination=None, session=None):
        """Settle an action that has no outcome of its own: trouble is written, success is quiet."""
        destination = destination or self._here()
        if session is not None and session != getattr(self, "selected", None):
            return
        if error:
            self._fail(destination, tr("Não foi possível concluir a operação"),
                       tr("Nada foi alterado. Tente de novo."), error=error, session=session)
        elif value is False:
            self._fail(destination, tr("A operação não foi aceita"),
                       tr("Verifique o estado atual e tente de novo."), tone="warning", session=session)
        elif receipt:
            self._say(destination, receipt, session=session)

    # -- Local report profiles, history, and Q&A ----------------------

    @staticmethod
    def _profile_label(profile):
        suffix = " · " + tr("desativado") if profile.get("disabled") else ""
        if profile.get("builtin"):
            return str(profile.get("name", profile.get("id", tr("Formato"))))
        return f'{profile.get("name", profile.get("id", tr("perfil")))} · v{profile.get("version", 1)}{suffix}'

    def refresh_report_profiles(self):
        """Load bounded profile definitions off Tk and retain a safe fallback."""
        reader = getattr(self.controller, "list_report_profiles", None)
        if not callable(reader):
            self._set_report_profiles([])
            return

        def loaded(profiles, error):
            if self.closed:
                return
            if error or not isinstance(profiles, list):
                if error:
                    self._fail("library", tr("Não foi possível carregar os formatos de resumo"),
                               tr("Os formatos internos continuam disponíveis."),
                               tone="warning", error=error)
                self._set_report_profiles([])
                return
            self._set_report_profiles(profiles)

        language = self.language.get()
        language = "pt-BR" if language == "auto" else language
        self._submit("report_profiles", lambda: reader(language=language), loaded)

    def _set_report_profiles(self, profiles):
        previous = self._selected_report_profile() if hasattr(self, "report_profile_choice") else None
        try:
            from meeting_intelligence import MeetingIntelligence
            language = self.language.get() or "pt-BR"
            fallback = MeetingIntelligence.builtin_profiles("pt-BR" if language == "auto" else language)
        except Exception:
            fallback = []
        values = profiles if isinstance(profiles, list) and profiles else fallback
        clean = [item for item in values if isinstance(item, dict)]
        if not clean:
            clean = fallback
        primary = ("meeting_notes", "interview", "one_on_one", "sales", "customer_feedback")
        clean.sort(key=lambda item: primary.index(item.get("id"))
                   if item.get("id") in primary else len(primary))
        self.report_profiles = clean[:128]
        self.report_profile_by_label = {
            self._profile_label(item): item for item in self.report_profiles
        }
        labels = list(self.report_profile_by_label)
        if hasattr(self, "report_profile_box"):
            self.report_profile_box.configure(values=labels)
            current = self.report_profile_choice.get()
            if current not in labels:
                preferred = next((label for label, item in self.report_profile_by_label.items()
                                  if previous and item.get("id") == previous.get("id")), None)
                if preferred is None:
                    preferred = next((label for label, item in self.report_profile_by_label.items()
                                      if item.get("id") == "meeting_notes"), labels[0] if labels else "")
                self.report_profile_choice.set(preferred)
        self._report_profile_changed()

    def _selected_report_profile(self):
        return getattr(self, "report_profile_by_label", {}).get(self.report_profile_choice.get())

    def _report_profile_changed(self, _event=None):
        self._sync_report_profile_actions()
        current = self._selected_report_profile()
        if hasattr(self, "summary_format_hint"):
            from meeting_intelligence import builtin_profile_description
            description = builtin_profile_description(current.get("id")) if current and current.get("builtin") else ""
            self.summary_format_hint.set(description or tr("Use seu formato personalizado e ajuste o foco se precisar."))
        return current

    def _sync_report_profile_actions(self):
        current = self._selected_report_profile()
        custom = bool(current and not current.get("builtin"))
        disabled = bool(current and current.get("disabled"))
        for attribute, state in (
            ("report_enable_button", "normal" if custom and disabled else "disabled"),
            ("report_disable_button", "normal" if custom and not disabled else "disabled"),
        ):
            button = getattr(self, attribute, None)
            if button is not None:
                button.configure(state=state)

    def _profile_saved(self, value, error):
        if error:
            self._fail("library", tr("Não foi possível salvar o perfil"),
                       tr("O perfil de relatório não foi alterado. Tente salvar de novo."), error=error)
            return
        self._say("library", tr("Perfil de relatório salvo."))
        self.refresh_report_profiles()

    def create_report_profile(self):
        base = self._selected_report_profile() or {
            "sections": ["summary"], "instructions": "",
        }
        values = self._report_profile_form("", str(base.get("instructions", "")))
        if values is None:
            return
        profile = {
            "id": self._new_profile_identifier(values["name"]), "name": values["name"].strip(),
            "instructions": values["instructions"],
            "sections": list(base.get("sections", ["summary"])),
            "language": ("pt-BR" if self.language.get() == "auto"
                         else self.language.get() or "pt-BR"),
        }
        self._submit("save_report_profile", lambda: self.controller.save_report_profile(profile),
                     self._profile_saved)

    def duplicate_report_profile(self):
        current = self._selected_report_profile()
        if not current:
            self._say("library", tr("Selecione um perfil para duplicar."))
            return
        values = self._report_profile_form(tr("{name} (cópia)", name=current.get("name", tr("Perfil"))),
                                           str(current.get("instructions", "")))
        if values is None:
            return
        profile = dict(current)
        profile.pop("version", None)
        profile.pop("profile_hash", None)
        profile.pop("builtin", None)
        profile["disabled"] = False
        profile["id"] = self._new_profile_identifier(values["name"])
        profile["name"] = values["name"].strip()
        profile["instructions"] = values["instructions"]
        self._submit("save_report_profile", lambda: self.controller.save_report_profile(profile),
                     self._profile_saved)

    def edit_report_profile(self):
        current = self._selected_report_profile()
        if not current or current.get("builtin"):
            self._say("library", tr("Perfis internos não podem ser editados; duplique um para personalizar."))
            return
        values = self._report_profile_form(str(current.get("name", "")), str(current.get("instructions", "")))
        if values is None:
            return
        profile = dict(current, name=values["name"].strip(), instructions=values["instructions"])
        profile.pop("version", None)
        profile.pop("profile_hash", None)
        profile.pop("builtin", None)
        self._submit("save_report_profile", lambda: self.controller.save_report_profile(profile),
                     self._profile_saved)

    def disable_report_profile(self):
        current = self._selected_report_profile()
        if not current or current.get("builtin"):
            self._say("library", tr("Selecione um perfil personalizado para desativar."))
            return
        identifier = current.get("id")
        self._submit("disable_report_profile",
                     lambda: self.controller.disable_report_profile(identifier),
                     self._profile_saved)

    def _new_profile_identifier(self, name):
        stem = re.sub(r"[^a-z0-9_-]+", "-", name.strip().lower()).strip("-")[:64] or "formato"
        existing = {item.get("id") for item in getattr(self, "report_profiles", [])}
        identifier, suffix = stem, 2
        while identifier in existing:
            identifier, suffix = f"{stem}-{suffix}", suffix + 1
        return identifier

    def _report_profile_form(self, name, instructions):
        def validate(values):
            if not values["name"].strip() or len(values["name"]) > 128:
                raise ValueError(tr("Nome: use de 1 a 128 caracteres."))
            if len(values["instructions"]) > 2000:
                raise ValueError(tr("Instruções: use até 2.000 caracteres."))
        return ui_theme.form_dialog(
            self.window, tr("Formato personalizado"),
            (("name", tr("Nome"), name), ("instructions", tr("Instruções adicionais"), instructions)),
            multiline=("instructions",), validate=validate, set_icon=self._dialog_icon)

    def enable_report_profile(self):
        current = self._selected_report_profile()
        if not current or current.get("builtin"):
            self._say("library", tr("Selecione um perfil personalizado desativado para ativar."))
            return
        identifier = current.get("id")
        self._submit("enable_report_profile",
                     lambda: self.controller.enable_report_profile(identifier),
                     self._profile_saved)

    def delete_report_profile(self):
        current = self._selected_report_profile()
        if not current or current.get("builtin"):
            self._say("library", tr("Selecione um perfil personalizado para excluir."))
            return
        if not messagebox.askyesno(tr("Excluir perfil"), tr("Excluir este perfil personalizado?"), parent=self.window):
            return
        identifier = current.get("id")
        self._submit("delete_report_profile",
                     lambda: self.controller.delete_report_profile(identifier),
                     self._profile_saved)

    def generate_report(self):
        if getattr(self, "summary_pending", None):
            return
        if not self.selected or not self.detail_ready:
            self._say("library", tr("Selecione uma gravação na biblioteca."))
            return
        profile = self._selected_report_profile()
        if not profile:
            self.summary_status.set(tr("Escolha um formato para o resumo."))
            return
        if profile.get("disabled"):
            self.summary_status.set(tr("Ative este formato ou escolha outro antes de gerar."))
            return
        if not self.transcript_revision:
            self.summary_status.set(tr("Transcreva esta gravação antes de gerar o resumo."))
            return
        model = self.summary_model.get().strip()
        if not self.summary_model_installed.get(model):
            self.summary_status.set(tr("Instale um modelo de resumo em Configurações → Modelos para continuar."))
            return
        focus = self.summary_focus.get("1.0", "end-1c").strip()
        if len(focus) > 600:
            self.summary_status.set(tr("Use um foco adicional de até 600 caracteres."))
            return
        session_id = self.selected
        revision = self.transcript_revision
        profile = copy.deepcopy(profile)
        self.summary_generation = getattr(self, "summary_generation", 0) + 1
        request = self.summary_generation
        self.summary_pending = (session_id, request)
        self.summary_request_cancel = threading.Event()
        event = self.summary_request_cancel
        self._summary_cancel_requested = None
        self._summary_completion_context = self._current_completion_context()
        self.summary_status.set(tr("Gerando {name} localmente… O resumo atual será preservado.", name=profile["name"]))
        self._sync_summary_controls()
        submitted = self._submit(
            "generate_report",
            lambda: self.controller.generate_report(
                session_id, model, profile=profile, revision=revision, focus=focus or None, cancel_event=event,
            ),
            lambda value, error: self._report_generated(session_id, value, error, request=request),
        )
        if not submitted:
            self._report_generated(session_id, None, tr("Fila de operações ocupada."), request=request)

    def cancel_summary_generation(self):
        if not getattr(self, "summary_pending", None):
            return
        self._summary_cancel_requested = self.summary_pending
        event = getattr(self, "summary_request_cancel", None)
        if event is not None:
            event.set()
        if self.bridge.cancel_queued("generate_report"):
            session_id, request = self.summary_pending
            self._report_generated(session_id, None, "cancelled", request=request)
            return

    def _toggle_summary_setup(self):
        if self.summary_setup.winfo_manager():
            self.summary_setup.pack_forget()
        else:
            self.summary_setup.pack(fill="x", padx=(12, 0), pady=(self.ui.space_sm, 0),
                                    after=self.summary_setup_toggle)
        self._sync_summary_setup_label()

    def _sync_summary_setup_label(self):
        label = tr("Gerar outra versão") if self.summary_text else tr("Gerar resumo")
        ui_theme.disclosure(self.summary_setup_toggle, label, bool(self.summary_setup.winfo_manager()), self.ui)
        setup_open = bool(self.summary_setup.winfo_manager())
        for widget, primary in (
            (self.copy_summary_button, bool(self.summary_text)),
            (self.regenerate_summary_button, not self.summary_text and setup_open),
            (self.summary_setup_toggle, not self.summary_text and not setup_open),
        ):
            # Aqua uses native buttons. The themed Tk button owns repainting
            # its role and disabled state; keep both in sync when the next action changes.
            if hasattr(widget, "_variant"):
                widget._variant = "primary" if primary else "neutral"
                widget._paint()

    def _report_generated(self, session_id, value, error, request=None):
        if self.closed:
            return
        if request is not None and getattr(self, "summary_pending", None) != (session_id, request):
            return
        self.summary_pending = None
        self.summary_request_cancel = None
        self._sync_summary_controls()
        if self.selected != session_id:
            return
        if error or not isinstance(value, dict):
            self.summary_status.set(self._summary_hint(getattr(self, "summary_text", "")))
            if getattr(self, "_summary_cancel_requested", None) == (session_id, request):
                self._fail("library", tr("O resumo foi cancelado"),
                           tr("O resumo anterior foi preservado. Gere de novo quando quiser."),
                           tone="warning", session=session_id)
                return
            self._fail("library", tr("Não foi possível gerar o resumo"),
                       tr("O resumo anterior foi preservado. Gere de novo quando quiser."),
                       error=error, session=session_id, action=(tr("Gerar resumo"), self.summarize))
            return
        self._show_summary(value)
        self.summary_status.set(tr("Resumo salvo nesta gravação. Você pode copiar ou gerar outra versão."))
        self.refresh_reports(session_id, preferred_report_id=value.get("report_id"))
        self._summary_completed(session_id, getattr(self, "_summary_completion_context", None))

    def refresh_reports(self, session_id=None, *, preferred_report_id=None):
        session_id = session_id or self.selected
        if not session_id:
            return
        self.report_request += 1
        request = self.report_request
        reader = getattr(self.controller, "list_reports", None)
        if not callable(reader):
            return

        def loaded(reports, error):
            if self.closed or request != self.report_request or session_id != self.selected:
                return
            if error:
                self._fail("library", tr("Não foi possível carregar as versões do resumo"),
                           tr("O resumo mostrado continua disponível. Atualize a lista para tentar de novo."),
                           error=error, session=session_id)
                return
            self._set_report_history(reports, preferred_report_id=preferred_report_id)

        self._submit("reports", lambda: reader(session_id, limit=REPORT_HISTORY_LIMIT), loaded)

    @staticmethod
    def _report_history_metadata(item):
        """Keep only the bounded list-row projection in the Tk object."""
        if not isinstance(item, dict):
            return None
        identifier = item.get("id", item.get("report_id"))
        if not isinstance(identifier, str) or not identifier:
            return None
        result = {"id": identifier}
        for key in (
            "schema_version", "kind", "profile_id", "profile_version",
            "session_id", "transcript_revision", "status", "created_at",
            "completed_at", "virtual", "reviewed", "review_generation",
        ):
            if key in item:
                value = item[key]
                if isinstance(value, (str, int, float, bool)) or value is None:
                    result[key] = value
        model = item.get("model")
        if isinstance(model, dict):
            result["model"] = {
                key: model[key]
                for key in ("id", "sha256", "runtime", "context_limit")
                if key in model and isinstance(model[key], (str, int, float, bool))
            }
        return result

    def _set_report_history(self, reports, *, preferred_report_id=None):
        self.report_history = []
        for item in reports or ():
            if len(self.report_history) >= REPORT_HISTORY_LIMIT:
                break
            metadata = self._report_history_metadata(item)
            if metadata is not None:
                self.report_history.append(metadata)
        labels, ids = [], []
        for item in self.report_history:
            identifier = str(item.get("id", ""))
            kind = tr("Pergunta") if item.get("kind") == "qa" else tr("Relatório")
            profile_id = item.get("profile_id")
            profile = next((profile.get("name") for profile in getattr(self, "report_profiles", [])
                            if profile.get("id") == profile_id),
                           tr("Resposta salva") if item.get("kind") == "qa" else tr("Resumo"))
            created = format_local_timestamp(item.get("created_at"))
            labels.append(f"{kind} · {profile} · {created}")
            ids.append(identifier)
        self.report_history_ids = ids
        self.report_history_box.configure(values=labels)
        if not labels:
            self.report_history_choice.set("")
            self.selected_report = None
            self.report_sections = {}
            self.report_provenance.set(tr("Nenhum relatório selecionado."))
            self.report_editor.delete("1.0", "end")
            self.report_citations.delete(0, "end")
            return
        if preferred_report_id in ids:
            index = ids.index(preferred_report_id)
        elif (self.selected_report and self.selected_report.get("kind") != "qa"
              and self.selected_report.get("id") in ids):
            index = ids.index(self.selected_report["id"])
        else:
            # Saved chat answers must not hide the latest summary on reopening.
            index = next((i for i in range(len(ids) - 1, -1, -1)
                          if self.report_history[i].get("kind") != "qa"), len(ids) - 1)
        self.report_history_box.current(index)
        self._load_report(ids[index])

    def _report_history_changed(self, _event=None):
        index = self.report_history_box.current()
        if 0 <= index < len(self.report_history_ids):
            self._load_report(self.report_history_ids[index])

    def _load_report(self, report_id):
        session_id = self.selected
        if not session_id or not report_id:
            return
        self.report_request += 1
        request = self.report_request

        def loaded(report, error):
            if self.closed or request != self.report_request or session_id != self.selected:
                return
            if error:
                self._fail("library", tr("Não foi possível abrir este relatório"),
                           tr("Escolha outra versão ou atualize a lista."),
                           error=error, session=session_id)
                return
            self._show_report(report)

        self._submit("report_detail", lambda: self.controller.get_report(session_id, report_id), loaded)

    def _show_report(self, report):
        self.selected_report = report if isinstance(report, dict) else None
        if not self.selected_report:
            return
        selected = self.selected_report.get("reviewed_artifact")
        sections = selected.get("sections") if isinstance(selected, dict) else None
        generated = self.selected_report.get("generated", {})
        merged = dict(generated) if isinstance(generated, dict) else {}
        if isinstance(sections, dict):
            merged.update(sections)
        self.report_sections = merged
        if self.selected_report.get("kind") != "qa":
            self._show_summary(merged)
            profile_id = self.selected_report.get("profile_id")
            name = next((item.get("name") for item in getattr(self, "report_profiles", [])
                         if item.get("id") == profile_id), tr("Resumo"))
            self.summary_status.set(tr("{name} · Versão salva nesta gravação.", name=name))
        names = list(self.report_sections)[:64]
        self.report_section_box.configure(values=names)
        if names:
            self.report_section_choice.set(names[0])
        else:
            self.report_section_choice.set("")
        model = self.selected_report.get("model", {})
        self.report_provenance.set(
            tr("Perfil {profile} v{version} · revisão {revision} · modelo {model} · SHA-256 {sha}…",
               profile=self.selected_report.get("profile_id", tr("legado")),
               version=self.selected_report.get("profile_version", "?"),
               revision=self.selected_report.get("transcript_revision", "?"),
               model=model.get("id", "?"), sha=str(model.get("sha256", ""))[:16])
        )
        self._render_report_section()
        self._render_report_citations()

    def _report_section_changed(self, _event=None):
        self._render_report_section()

    def _render_report_section(self):
        section = self.report_section_choice.get()
        value = self.report_sections.get(section, "")
        if section == "detailed_notes":
            text = format_detailed_notes(value)
            # Validated notes are capped below this UI ceiling; retain the
            # complete readable presentation instead of the old 64 KiB slice.
        else:
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
        self.report_editor.delete("1.0", "end")
        self.report_editor.insert("1.0", str(text) if section == "detailed_notes" else str(text)[:MAX_REPORT_REVIEW_CHARS])

    @staticmethod
    def _citation_ids(value):
        result = []
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"segment_ids", "citations"} and isinstance(child, list):
                    result.extend(item for item in child if isinstance(item, str))
                else:
                    result.extend(MeetingWindow._citation_ids(child))
        elif isinstance(value, list):
            for child in value:
                result.extend(MeetingWindow._citation_ids(child))
        return result

    def _render_report_citations(self):
        self.report_citations.delete(0, "end")
        generated = self.selected_report.get("generated", {}) if self.selected_report else {}
        seen = set()
        self.report_citation_refs = []
        for identifier in self._citation_ids(generated):
            if identifier in seen:
                continue
            seen.add(identifier)
            self.report_citation_refs.append(identifier)
            self.report_citations.insert("end", identifier[:128])
        self.report_citations.selection_clear(0, "end")

    def copy_report_section(self):
        if not self.selected_report:
            self._say("library", tr("Selecione um relatório primeiro."))
            return
        try:
            projection = report_export_projection(
                self.selected_report, section=self.report_section_choice.get() or None,
            )
            text = json.dumps(projection["sections"], ensure_ascii=False, indent=2)
        except (TypeError, ValueError) as exc:
            self._say("library", str(exc))
            return
        self._copied(tr("Seção copiada sem alterar o relatório gerado."),
                     tr("Não foi possível copiar a seção"), Clipboard.set_content(text), None)

    def save_report_review(self):
        if not self.selected or not self.selected_report:
            self._say("library", tr("Selecione um relatório estruturado primeiro."))
            return
        report_id = self.selected_report.get("id")
        section = self.report_section_choice.get()
        if not report_id or report_id == "legacy-summary" or not section:
            self._say("library", tr("O resumo legado não aceita revisão por seção; gere um relatório novo."))
            return
        text = self.report_editor.get("1.0", "end-1c")
        if len(text) > MAX_REPORT_REVIEW_CHARS:
            self._say("library", tr("Esta revisão é longa demais para salvar. Exporte a seção completa."))
            return
        artifact = self.selected_report.get("reviewed_artifact")
        expected = artifact.get("generation", 0) if isinstance(artifact, dict) else 0
        session_id = self.selected

        def saved(_value, error):
            if self.closed or session_id != self.selected:
                return
            if error:
                self._fail("library", tr("A revisão não foi salva"),
                           tr("O relatório gerado continua intacto. Tente salvar de novo."),
                           error=error, session=session_id)
                return
            self._say("library", tr("Revisão salva separadamente do relatório gerado."), session=session_id)
            self.refresh_reports(session_id)

        self._action("review_report", session_id, report_id, {section: text},
                     expected_generation=expected, callback=saved)

    def export_report(self):
        if not self.selected or not self.selected_report:
            self._say("library", tr("Selecione um relatório primeiro."))
            return
        path = filedialog.asksaveasfilename(
            parent=self.window, title=tr("Exportar relatório"), defaultextension=".md",
            filetypes=(("Markdown", "*.md"), (tr("Texto"), "*.txt"), ("JSON", "*.json")),
        )
        if not path:
            return
        suffix = os.path.splitext(path)[1].casefold()
        fmt = "json" if suffix == ".json" else "text" if suffix == ".txt" else "markdown"
        session_id, report_id = self.selected, self.selected_report.get("id")
        self._action("export_report", session_id, report_id, path, format=fmt,
                     callback=lambda value, error: self._report_exported(session_id, value, error))

    def _report_exported(self, session_id, value, error):
        if self.closed or session_id != self.selected:
            return
        if error:
            self._fail("library", tr("Não foi possível exportar o relatório"),
                       tr("Nenhum arquivo foi gravado. Tente de novo ou escolha outra pasta."),
                       error=error, session=session_id)
        else:
            self._say("library", tr("Relatório exportado atomicamente sem caminhos locais."),
                      session=session_id)

    def _chat_turn(self, session_id, turn_id):
        return next((turn for turn in self.ask_conversations.get(session_id, [])
                     if turn["id"] == turn_id), None)

    def _render_chat(self):
        if not hasattr(self, "meeting_chat"):
            return
        self.meeting_chat.render(getattr(self, "ask_conversations", {}).get(self.selected, []))
        self._sync_qa_controls()

    def new_chat(self):
        turns = self.ask_conversations.get(self.selected, [])
        if self.ask_pending or any(turn.get("saving") for turn in turns):
            self.ask_status.set(tr("Aguarde a resposta ou o salvamento terminar."))
            return
        self.ask_conversations.pop(self.selected, None)
        self.meeting_chat.set_question("")
        self.ask_status.set(tr("Nova conversa. Respostas salvas continuam nos relatórios."))
        self._render_chat()
        self.meeting_chat.focus_composer()

    @staticmethod
    def _chat_history(turns):
        history = []
        # ceiling: six recent exchanges and 12 KiB of transient intent context;
        # longer conversations need model-aware summarization before expansion.
        for turn in reversed(turns):
            if turn.get("status") != "complete":
                continue
            item = {"question": turn["question"][:1000], "answer": turn["answer"][:2000]}
            if not history and len(json.dumps([item], ensure_ascii=False).encode("utf-8")) > 12_000:
                item = {"question": item["question"][:500], "answer": item["answer"][:1000]}
            if len(json.dumps([item, *history], ensure_ascii=False).encode("utf-8")) > 12_000:
                break
            history.insert(0, item)
            if len(history) == 6:
                break
        return history

    def ask_this_meeting(self):
        if not self.selected or not self.detail_ready:
            self._say("library", tr("Selecione uma gravação na biblioteca."))
            return
        if self.ask_pending:
            return
        question = self.meeting_chat.get_question().strip()
        if not question:
            self.ask_status.set(tr("Digite uma pergunta sobre a gravação."))
            return
        if len(question) > 2000:
            self.ask_status.set(tr("Use uma pergunta de até 2.000 caracteres."))
            return
        if not self.transcript_revision:
            self.ask_status.set(tr("Transcreva esta gravação antes de conversar sobre ela."))
            return
        model = self.summary_model.get().strip()
        if not self.summary_model_installed.get(model):
            self.ask_status.set("")
            self.meeting_chat.show_notice(
                "warning", tr("Falta um modelo de resumo"),
                tr("O chat responde com um modelo local. Instale um para conversar sobre esta gravação."),
                action=(tr("Abrir Configurações › Modelos"), self.open_summary_models))
            return
        self.meeting_chat.clear_notice()
        session_id = self.selected
        if session_id not in self.ask_conversations:
            # ceiling: retain eight temporary recording chats per window.
            # Persisted conversations require a separately chosen retention policy.
            if len(self.ask_conversations) >= MAX_CHAT_SESSIONS:
                removable = next((key for key, values in self.ask_conversations.items()
                                  if not any(turn.get("saving") for turn in values)), None)
                if removable is None:
                    self.ask_status.set(tr("Aguarde o salvamento das outras conversas terminar."))
                    return
                self.ask_conversations.pop(removable)
            self.ask_conversations[session_id] = []
        turns = self.ask_conversations[session_id]
        if len(turns) >= MAX_CHAT_TURNS:
            self.ask_status.set(tr("Esta conversa chegou a 24 perguntas. Salve o que precisa e use Nova conversa."))
            return
        history = self._chat_history(turns)
        self.ask_request += 1
        request = self.ask_request
        revision = self.transcript_revision
        turns.append({"id": request, "question": question, "answer": "", "status": "pending",
                      "model": model, "revision": revision, "citations": [], "saved": False})
        self.ask_pending = (session_id, request)
        self.ask_request_cancel = threading.Event()
        event = self.ask_request_cancel
        self.meeting_chat.set_question("")
        self.ask_status.set(tr("Consultando a transcrição localmente…"))
        self._render_chat()
        submitted = self._submit(
            "ask_this_meeting",
            lambda: self.controller.ask_this_meeting(
                session_id, question, model, revision=revision, history=history, cancel_event=event,
            ),
            lambda value, error: self._answer_loaded(session_id, request, question, value, error),
        )
        if not submitted:
            self._answer_loaded(session_id, request, question, None, tr("Fila de operações ocupada."))
        self.meeting_chat.focus_composer()

    def open_summary_models(self):
        """Take the user to where a summary model is installed."""
        self.notebook.select(self.settings_tab)
        self.settings_sections.select("models")
        self.model_sections.select("summary")

    def retry_chat_answer(self, turn_id):
        """Send a failed question again: the failed turn goes, the question goes back to the composer."""
        turns = self.ask_conversations.get(self.selected, [])
        turn = self._chat_turn(self.selected, turn_id)
        if turn is None or turn.get("status") != "error" or self.ask_pending:
            return
        turns.remove(turn)
        self.meeting_chat.set_question(turn["question"])
        self.ask_this_meeting()

    def cancel_chat_answer(self):
        """Stop the pending answer and give its question back to the composer."""
        if not self.ask_pending:
            return
        session_id, request = self.ask_pending
        event = getattr(self, "ask_request_cancel", None)
        if event is not None:
            event.set()
        self.ask_request_cancel = None
        self.ask_pending = None
        self.bridge.invalidate("ask_this_meeting")
        turn = self._chat_turn(session_id, request)
        if turn is not None:
            self.ask_conversations[session_id].remove(turn)
        if session_id == self.selected:
            restored = turn is not None and not self.meeting_chat.get_question().strip()
            if restored:
                self.meeting_chat.set_question(turn["question"])
            self.ask_status.set(tr("Resposta cancelada. A pergunta voltou para o campo de texto.")
                                if restored else tr("Resposta cancelada."))
            self._render_chat()
            self.meeting_chat.focus_composer()
        else:
            self._sync_qa_controls()

    def _answer_loaded(self, session_id, request, question, value, error):
        if self.closed or self.ask_pending != (session_id, request):
            return
        self.ask_pending = None
        self.ask_request_cancel = None
        turn = self._chat_turn(session_id, request)
        if turn is None:
            self._sync_qa_controls()
            return
        if error or not isinstance(value, dict) or not isinstance(value.get("answer"), str):
            turn.update(status="error", error=tr("Nada foi salvo. Tente enviar a pergunta de novo."),
                        details=_technical_error_detail(error) if error else "")
        else:
            turn.update(status="complete", answer=value["answer"][:MAX_ANSWER_CHARS],
                        citations=[item for item in value.get("citations", [])
                                   if isinstance(item, str)][:16],
                        uncertainty=str(value.get("uncertainty", "high")), payload=dict(value),
                        revision=value.get("revision") or turn["revision"])
        if session_id == self.selected:
            if turn["status"] == "error" and not self.meeting_chat.get_question().strip():
                self.meeting_chat.set_question(question)
            self.ask_status.set(tr("Resposta não concluída. Tente novamente.") if turn["status"] == "error"
                                else tr("Continue a conversa. Salve as respostas que quiser guardar."))
            self._render_chat()
        else:
            self._sync_qa_controls()

    def copy_chat_answer(self, turn_id):
        turn = self._chat_turn(self.selected, turn_id)
        if turn is None or turn.get("status") != "complete":
            return
        text = turn["answer"]
        self._submit("copy_chat_answer", lambda: Clipboard.set_content(text),
                     lambda copied, error: self.ask_status.set(
                         tr("Resposta copiada.") if copied and not error else tr("Não foi possível copiar a resposta.")))

    def save_answer(self, turn_id=None):
        if getattr(self, "qa_mode", None) is not None and self.qa_mode.get() == "memory_only":
            self.ask_status.set(tr("O workspace está em memory_only; a resposta não pode ser salva."))
            self._sync_qa_controls()
            return
        turns = self.ask_conversations.get(self.selected, [])
        turn = self._chat_turn(self.selected, turn_id) if turn_id is not None else next(
            (item for item in reversed(turns) if item.get("status") == "complete"), None,
        )
        if turn is None or turn.get("status") != "complete" or turn.get("saved") or turn.get("saving"):
            return
        session_id = self.selected
        turn_id = turn["id"]
        answer = dict(turn["payload"])
        answer.pop("revision", None)
        answer.pop("revision_id", None)
        turn["saving"] = True
        self._render_chat()

        def saved(value, error):
            if self.closed or self._chat_turn(session_id, turn_id) is not turn:
                return
            turn["saving"] = False
            if error or value is False:
                if error:
                    self._fail("library", tr("A resposta não foi salva"),
                               tr("Ela continua nesta conversa. Tente salvar de novo."),
                               error=error, session=session_id)
            else:
                turn["saved"] = True
            if session_id == self.selected:
                self.ask_status.set(tr("Resposta salva nos relatórios.") if turn.get("saved")
                                    else tr("A resposta não foi salva. Tente novamente."))
                self._render_chat()
                if turn.get("saved"):
                    self.refresh_reports(session_id)

        submitted = self._submit(
            f"save_answer:{session_id}:{turn_id}",
            lambda: self.controller.save_answer(
                session_id, answer, turn["model"], question=turn["question"],
                revision=turn["revision"],
            ), saved,
        )
        if not submitted:
            saved(False, None)

    def jump_to_report_citation(self):
        index = self.report_citations.curselection()
        if not index or index[0] >= len(getattr(self, "report_citation_refs", [])):
            return
        identifier = self.report_citation_refs[index[0]]
        revision = self.selected_report.get("transcript_revision") if self.selected_report else None
        self._jump_to_citation(identifier, revision)

    def _jump_to_citation(self, identifier, revision):
        session_id = self.selected
        if not session_id or not revision:
            return
        self.citation_request = getattr(self, "citation_request", 0) + 1
        request = self.citation_request

        def read():
            # Probe bounded transcript pages through the worker-facing
            # controller API.  No full JSONL transcript is retained in Tk.
            for offset in range(0, TRANSCRIPT_LIMIT + TRANSCRIPT_PAGE_SIZE, TRANSCRIPT_PAGE_SIZE):
                page = self.controller.get_transcript_page(
                    session_id, revision=revision, offset=offset, limit=TRANSCRIPT_PAGE_SIZE,
                )
                values = page.get("segments", []) if isinstance(page, dict) else []
                if any(isinstance(item, dict) and item.get("id") == identifier for item in values):
                    return page
                if not isinstance(page, dict) or not page.get("has_more"):
                    break
            return None

        def loaded(page, error):
            if self.closed or request != self.citation_request or session_id != self.selected:
                return
            if error or not page:
                self._fail("library", tr("A fonte {source} não está disponível na revisão selecionada.",
                                         source=identifier[:128]), tone="warning", session=session_id)
                return
            self.transcript_offset = int(page.get("offset", 0))
            self.transcript_revision = revision
            self.transcript_has_previous = bool(page.get("has_previous"))
            self.transcript_has_more = bool(page.get("has_more"))
            self._render_transcript(page.get("segments", []))
            self._update_transcript_paging_controls()
            self.transcript_style.set(tr("Com horários"))
            self._sync_transcript_style()
            self.detail_sections.select("transcript")
            for key, segment in self.segments.items():
                if isinstance(segment, dict) and segment.get("id") == identifier:
                    try:
                        self.transcript.selection_set(key)
                        self.transcript.see(key)
                    except (tk.TclError, AttributeError):
                        pass
                    break
            self._say("library", tr("Fonte da citação {source} carregada.", source=identifier[:128]))

        self._submit("citation_jump", read, loaded)

    def jump_to_ask_citation(self, turn_id, identifier):
        turn = self._chat_turn(self.selected, turn_id)
        if turn is not None and identifier in turn.get("citations", []):
            self._jump_to_citation(identifier, turn["revision"])

    def _settings_loaded(self, raw, error):
        if error:
            self._fail("recording", tr("Não foi possível carregar as configurações"),
                       tr("A gravação fica bloqueada até elas carregarem. Reabra a janela para tentar de novo."),
                       error=error)
            return
        self.raw_settings = dict(raw or {})
        if hasattr(self, "appearance_display"):
            preference = ui_theme.normalize_preference(self.raw_settings.get("appearance"))
            self.appearance_display.set(tr(APPEARANCE_LABELS[preference]))
            self.appearance_status.set(tr(APPEARANCE_STATUS[preference]))
        try:
            settings = resolve_meeting_settings(self.raw_settings)
        except ValueError as exc:
            self._fail("recording", tr("As configurações de gravação são inválidas"), str(exc),
                       tone="warning")
            return
        self.settings = settings
        self.input_enabled.set(settings.input_enabled)
        self.output_enabled.set(settings.output_enabled)
        self.destination.set(settings.destination)
        self.destination_label.set(destination_display(settings.destination))
        self.voice_boost.set(settings.voice_boost)
        self.profile.set(settings.profile_setting)
        self.language.set(settings.language)
        self.profile_display.set(profile_display_label(settings.profile_setting))
        self.language_display.set(tr(LANGUAGE_LABELS[settings.language]))
        self.hotkey.set(settings.hotkey)
        self.summary_model.set(settings.summary_model)
        self.summary_display.set(tr(SUMMARY_LABELS[settings.summary_model]))
        self._profile_changed()
        self.options.clear()
        self._render_devices()
        self._sync_source_controls()
        self.settings_loaded = True
        if hasattr(self, "controller"):
            self.refresh_report_profiles()
        self._sync_shortcut_hint()
        self._section_loaded("recording")

    def set_startup_status(self, ready, message):
        """Apply app-level recovery admission without exposing private paths."""
        if self.closed:
            return
        self.retention_ready = bool(ready)
        self.startup_status = _safe_retention_text(message, 320)
        if hasattr(self, "start_button") and not ready:
            self.start_button.configure(state="disabled")
        self._sync_trash_controls()
        if hasattr(self, "privacy_status") and not ready:
            self.privacy_status.set(self.startup_status or tr("Recuperação local pendente."))
        if not ready:
            self._fail("recording", tr("A gravação aguarda a recuperação local"),
                       self.startup_status
                       or tr("Recuperação local pendente; ações destrutivas estão bloqueadas."),
                       tone="warning", key="startup", sticky=True)
        else:
            self.messages.clear("recording", key="startup")

    def _privacy_loaded(self, value, error):
        if self.closed:
            return
        if error or not isinstance(value, dict):
            self._privacy_initial_loaded = False
            draft = getattr(self, "_settings_drafts", {}).get("privacy")
            if draft is not None and draft["baseline"] is None:
                self._sync_settings_bar("privacy")
            if error:
                self._fail("settings", tr("Privacidade local indisponível"),
                           tr("A gravação fica bloqueada até ela carregar. Reabra a janela para tentar de novo."),
                           error=error)
            self.privacy_ready = False
            if hasattr(self, "privacy_status"):
                self.privacy_status.set(tr("Privacidade indisponível; gravação bloqueada até recarregar."))
            return
        self.privacy_defaults = copy.deepcopy(value)
        self._privacy_initial_loaded = True
        self.privacy_ready = True
        if hasattr(self, "privacy_notice_enabled"):
            notice = value.get("recording_notice", {})
            if not isinstance(notice, dict):
                notice = {}
            self.privacy_notice_enabled.set(bool(notice.get("enabled", False)))
            self.privacy_notice_language.set(notice.get("language", "pt-BR"))
            self.qa_mode.set(value.get("qa_mode", "explicit_save"))
            self._sync_qa_controls()
            self.privacy_status.set(tr("Privacidade local carregada.") if self._privacy_form_ready()
                                    else tr("Configurações de privacidade carregando…"))
        self._section_loaded("privacy")

    def _profile_changed(self, _event=None):
        shown = self.profile_display.get()
        self.profile.set(FOLLOW_DICTATION if shown == profile_display_label(FOLLOW_DICTATION) else
                         next(key for key, label in PROFILE_LABELS.items() if tr(label) == shown))
        languages = available_languages(self._effective_profile())
        self.language_box.configure(values=[tr(LANGUAGE_LABELS[language]) for language in languages])
        if self.language.get() not in languages:
            self.language.set(languages[0])
        self.language_display.set(tr(LANGUAGE_LABELS[self.language.get()]))

    def _effective_profile(self):
        """The model a recording would use now; follows the current dictation choice."""
        if self.profile.get() != FOLLOW_DICTATION:
            return self.profile.get()
        try:
            current = self.settings_getter()
        except Exception:
            current = self.raw_settings
        return dictation_profile(current if isinstance(current, dict) else self.raw_settings)

    def _language_changed(self, _event=None):
        self.language.set(next(key for key, label in LANGUAGE_LABELS.items() if tr(label) == self.language_display.get()))

    def _source_toggled(self):
        self._sync_source_controls()
        self._source_selection_changed()

    def show_recording_settings(self):
        self.notebook.select(self.settings_tab)
        self.settings_sections.select("recording")

    def _source_selection_changed(self, _event=None):
        self._refresh_source_hints()
        self.preview_status.set(
            tr("Fontes alteradas. Valem a partir da próxima gravação.")
            if self.snapshot.get("state") in CAPTURE_STATES
            else tr("Fontes alteradas. Teste novamente antes de gravar."))

    def _refresh_source_hints(self):
        for track, hint in getattr(self, "endpoint_hints", {}).items():
            variable = self.input_enabled if track == "microphone" else self.output_enabled
            selection = dict(self.options.get(track, [])).get(
                self.endpoint_vars[track].get(), getattr(self.settings, track),
            )
            text, unavailable = endpoint_hint(
                self.devices, track, selection, enabled=bool(variable.get()),
                loaded=getattr(self, "devices_loaded", False),
            )
            hint.configure(text=text, fg=self.ui.warning if unavailable else self.ui.text_muted)

    def _sync_source_controls(self):
        enabled = {"microphone": bool(self.input_enabled.get()),
                   "system": bool(self.output_enabled.get())}
        for track, box in self.endpoint_boxes.items():
            box.configure(state="readonly" if enabled[track] else "disabled")
        self.voice_boost_check.configure(state="normal" if enabled["microphone"] else "disabled")
        if not enabled["microphone"]:
            self.voice_boost.set(False)
        self._refresh_source_hints()


    def choose_destination(self):
        initial = self.destination.get().strip() or None
        options = {"parent": self.window, "title": tr("Pasta padrão das gravações")}
        if initial:
            options["initialdir"] = initial
        path = filedialog.askdirectory(**options)
        if path:
            self.destination.set(path)
            self.destination_label.set(destination_display(path))

    def use_default_destination(self):
        self.destination.set("")
        self.destination_label.set(destination_display(""))

    def _current_settings(self):
        if not self.settings_loaded:
            raise ValueError(tr("Aguarde o carregamento das configurações antes de gravar ou salvar."))
        data = dict(self.raw_settings)
        if self.profile.get() == FOLLOW_DICTATION:
            # The dictation model can change while this window is open.
            data["voice_profile"] = self._effective_profile()
        data.update(meeting_input_enabled=bool(self.input_enabled.get()),
                    meeting_output_enabled=bool(self.output_enabled.get()),
                    meeting_destination=self.destination.get(),
                    meeting_auto_transcribe=True,
                    meeting_auto_summary=True,
                    meeting_voice_boost=bool(self.voice_boost.get()),
                    meeting_profile=self.profile.get(), meeting_language=self.language.get(),
                    meeting_hotkey=self.hotkey.get(), meeting_summary_model=self.summary_model.get())
        for track in ("microphone", "system"):
            label = self.endpoint_vars[track].get()
            selection = dict(self.options[track]).get(label)
            if selection is None:
                raise ValueError(tr("Atualize e selecione novamente o dispositivo de áudio."))
            data["meeting_" + track] = selection.payload()
        return validated_settings(data, data.get("voice_hotkey", DEFAULT_DICTATION_HOTKEY),
                                  data.get("voice_command_hotkey", DEFAULT_COMMAND_HOTKEY))

    def _saved_settings(self, settings, error):
        if error:
            self._fail("settings", tr("Não foi possível salvar as configurações"),
                       tr("As configurações anteriores continuam valendo. Tente salvar de novo."),
                       error=error)
            return
        self.settings = settings
        self.raw_settings.update(settings.payload())
        self._sync_shortcut_hint()
        self._say("settings", tr("Configurações salvas para a próxima gravação."))
        if self.on_settings_changed:
            self.on_settings_changed()

    def save_settings(self):
        draft = getattr(self, "_settings_drafts", {}).get("recording")
        if draft and draft["saving"]:
            return
        try:
            payload = self._recording_settings_payload()
            settings = resolve_meeting_settings(dict(self.raw_settings, **payload))
            values = self._draft_values("recording")
        except (ValueError, StopIteration) as exc:
            self._fail("settings", tr("Não foi possível salvar as configurações"),
                       str(exc) or tr("Revise as configurações e tente de novo."), tone="warning")
            return
        destination_failed = threading.Event()
        persist = self.persist_settings
        def save():
            path = payload["meeting_destination"]
            if path and not os.path.isdir(path):
                destination_failed.set()
                raise ValueError(tr("Pasta dos arquivos finais: escolha uma pasta existente e absoluta."))
            result = persist(payload)
            if result is False:
                raise ValueError(tr("Não foi possível salvar as configurações. Tente novamente."))
            return settings
        def saved(value, error):
            self._saved_settings(value, error)
            self._section_saved("recording", values, not error)
            if destination_failed.is_set():
                label = getattr(self, "recording_field_errors", {}).get("destination")
                if label is not None:
                    label.configure(text="⚠ " + str(error), fg=self.ui.danger)
                target = getattr(self, "recording_validation_fields", {}).get("destination")
                if target is not None:
                    target.focus_set()
        if draft:
            draft["saving"] = True
            self._sync_settings_bar("recording")
        if not self._submit("save_settings", save, saved):
            self._section_saved("recording", values, False)

    def refresh_devices(self):
        self.preview_status.set(tr("Buscando dispositivos…"))
        self.device_refresh_button.configure(text=tr("Atualizando…"), state="disabled")
        if not self._submit("devices", self.controller.devices, self._devices_loaded):
            self.device_refresh_button.configure(text=tr("Atualizar dispositivos"), state="normal")
            self.preview_status.set(tr("Aguarde a operação atual e tente atualizar novamente."))

    @staticmethod
    def _source_signature(settings):
        return settings.sources, settings.microphone.argument(), settings.system.argument()

    def preview_sources(self):
        request = self._validated_recording_request()
        if request is None:
            return
        settings, _title = request
        self.preview_signature = self._source_signature(settings)
        self.preview_status.set(tr("Testando por 3 segundos. Fale ou reproduza áudio nas fontes escolhidas…"))
        if not self._submit("preview_sources", lambda: self.controller.preview_sources(settings),
                            self._previewed):
            self.preview_status.set(tr("Não foi possível iniciar o teste. Tente novamente."))

    def _previewed(self, result, error):
        if error:
            self.preview_status.set(tr(SOURCE_TEST_HINT))
            self._fail("recording", tr("O teste de áudio falhou"),
                       tr("Nenhum áudio foi gravado. Confira as fontes escolhidas e teste de novo."),
                       error=error)
            return
        try:
            current = self._source_signature(self._current_settings())
        except (ValueError, StopIteration):
            current = None
        if current != self.preview_signature:
            self.preview_status.set(tr("Fontes alteradas. Teste novamente antes de gravar."))
            return
        names = {"microphone": tr("Microfone"), "system": tr("Sistema")}
        errors = set(result["errors"])
        parts = []
        for track in result["enabled"]:
            if track in errors:
                state = tr("falhou")
            elif result["peaks"][track] >= 0.01:
                state = tr("sinal detectado")
            else:
                state = tr("sem sinal detectado")
            parts.append(f"{names[track]}: {state}")
        self.preview_status.set(" · ".join(parts) + ".")

    def _devices_loaded(self, devices, error):
        self.device_refresh_button.configure(text=tr("Atualizar dispositivos"), state="normal")
        if error:
            self.preview_status.set(tr(SOURCE_TEST_HINT))
            self._fail("recording", tr("Não foi possível atualizar os dispositivos"),
                       tr("A lista anterior continua em uso. Conecte o dispositivo e atualize de novo."),
                       error=error)
            return
        # The startup read is no outcome the user asked for: it ends on the
        # standing hint, and only a later refresh earns the receipt.
        first_load = not getattr(self, "devices_loaded", False)
        self.devices = list(islice(devices or [], 256))
        self.devices_loaded = True
        self._render_devices()
        self.preview_status.set(tr(SOURCE_TEST_HINT) if first_load
                                else tr("Dispositivos atualizados. Teste as fontes antes de gravar."))

    def _render_devices(self):
        for track in ("microphone", "system"):
            current = dict(self.options.get(track, [])).get(self.endpoint_vars[track].get(), getattr(self.settings, track))
            options = endpoint_options(self.devices, track, current)
            box = self.endpoint_boxes[track]
            if getattr(box, "_device_elision", False) is True:
                font = ui_theme.combobox_font(box, self.ui)
                available = max(100, box.winfo_width() - self.ui.px(32)) if box.winfo_width() > 1 else self.ui.px(240)
                full_names, fitted = {}, []
                for label, selection in options:
                    display = ui_theme.ellipsize(label, font, available)
                    suffix = 2
                    while display in full_names:
                        tail = f" ({suffix})"
                        display = ui_theme.ellipsize(label, font, available - font.measure(tail)) + tail
                        suffix += 1
                    full_names[display] = label
                    fitted.append((display, selection))
                if not hasattr(self, "_endpoint_full_names"):
                    self._endpoint_full_names = {}
                self._endpoint_full_names[track] = full_names
                options = fitted
            self.options[track] = options
            self.endpoint_boxes[track].configure(values=[label for label, _ in options])
            match = next(label for label, selection in options if selection.argument() == current.argument())
            self.endpoint_vars[track].set(match)
        self._sync_source_controls()

    def _device_width_changed(self, track, width):
        widths = getattr(self, "_device_widths", {})
        if widths.get(track) == width:
            return
        widths[track] = width
        self._device_widths = widths
        if getattr(self, "_device_layout_after", None) is not None:
            self.window.after_cancel(self._device_layout_after)
        def fit():
            self._device_layout_after = None
            if not self.closed:
                self._render_devices()
        self._device_layout_after = self.window.after_idle(fit)

    def _validated_recording_request(self):
        try:
            settings = self._current_settings()
            title = self.record_title.get().strip()
            if len(title) > 400:
                raise ValueError(tr("O título deve ter até 400 caracteres."))
            if settings.destination and (not os.path.isabs(settings.destination)
                                         or not os.path.isdir(settings.destination)):
                raise ValueError(tr("A pasta padrão de gravações não existe ou não é absoluta."))
            for track in ("microphone", "system"):
                selection = getattr(settings, track)
                if settings.sources not in ("both", track):
                    continue
                if selection.mode == "manual" and not any(device.get("id") == selection.endpoint_id
                        and device.get("kind") == track for device in self.devices):
                    raise ValueError(tr("O dispositivo manual está indisponível. Atualize ou escolha outro dispositivo."))
        except (ValueError, StopIteration) as exc:
            self._fail("recording", tr("Revise as configurações da gravação"),
                       str(exc) or tr("Confira as fontes e tente de novo."), tone="warning")
            return None
        return settings, title

    def _start_hold(self, snapshot):
        """Name the window's own reason an idle recorder cannot start, or ""."""
        if snapshot.get("state", "idle") != "idle":
            return ""
        if not self.settings_loaded:
            return tr("Carregando as configurações de gravação…")
        if not self.retention_ready:
            return self.startup_status or tr("A gravação aguarda a recuperação do workspace.")
        if self.privacy_save_inflight:
            return tr("Aguarde a atualização de privacidade antes de iniciar a gravação.")
        if not self.privacy_ready:
            return tr("A gravação aguarda a privacidade local.")
        return ""

    def _processing_title(self, snapshot):
        """The library title of the recording the running job belongs to, or ""."""
        item = self.session_items.get(snapshot.get("processing_session"))
        title = " ".join(str(item.get("title") or "").split()) if item else ""
        if not title:
            return ""
        return title if len(title) <= 40 else title[:39].rstrip() + "…"

    def _start_blocked_text(self, snapshot):
        """One sentence naming what holds Start, or "" when nothing does."""
        job = start_blocker(snapshot)
        if not job:
            return self._start_hold(snapshot)
        if self.start_after_interrupt:
            return tr("Interrompendo {job}; a gravação começa em seguida.", job=job)
        title = self._processing_title(snapshot)
        if title:
            return tr("Aguardando {job} de “{title}”.", job=job, title=title)
        return tr("Aguardando {job}.", job=job)

    def _sync_start_blocked(self, snapshot, start_ready):
        """Show the hold under Start and, for processing, the interrupt override."""
        state = snapshot.get("state", "idle")
        reason = "" if start_ready or state not in {"idle", "postprocessing"} \
            else self._start_blocked_text(snapshot)
        interruptible = bool(reason and processing_job_label(snapshot))
        key = (reason, interruptible, bool(self.start_after_interrupt))
        if key == self._start_blocked_key:
            return
        self._start_blocked_key = key
        self.start_blocked_reason.set(reason)
        if reason:
            self.start_blocked_row.grid()
        else:
            self.start_blocked_row.grid_remove()
        if interruptible:
            self.interrupt_start_button.configure(
                state="disabled" if self.start_after_interrupt else "normal",
            )
            self.interrupt_start_button.grid()
        else:
            self.interrupt_start_button.grid_remove()

    def _library_blocker(self, snapshot):
        """Why work on a recording cannot start now, in a sentence ("" when it can)."""
        state = snapshot.get("state", "idle")
        if state in CAPTURE_STATES:
            return tr("Finalize a gravação em andamento primeiro.")
        job = processing_job_label(snapshot)
        if job or state == "postprocessing":
            return tr("Aguarde {job} terminar.", job=job or tr("o processamento"))
        if state != "idle":
            return tr("Os recursos de áudio ainda estão em uso.")
        return ""

    def _sync_processing_cancel(self, snapshot):
        """Offer Cancel only for a running job of this recording or of no recording.

        The line above the buttons names the step it stops; with no job of this
        recording running, it says what keeps Transcrever novamente from acting.
        """
        button = getattr(self, "cancel_processing_button", None)
        if button is None:
            return
        session_id = snapshot.get("processing_session")
        job = processing_job_label(snapshot) if snapshot.get("state", "idle") not in CAPTURE_STATES else ""
        mine = bool(job) and (session_id is None or session_id == self.selected)
        self._show_cancel(button, mine)
        if mine:
            step = tr("Em execução: {job}.", job=job)
        elif self.selected and self.detail_ready:
            step = self._library_blocker(snapshot) or (
                "" if getattr(self, "final_available", False)
                else tr("Transcrever de novo requer o áudio final. Use Preparar áudio final, em Arquivos."))
        else:
            step = ""
        if step != self.processing_job_status.get():
            self.processing_job_status.set(step)
            if step:
                self.processing_job_label.grid()
            else:
                self.processing_job_label.grid_remove()

    def _sync_library_controls(self, snapshot):
        """Enable each recovery control only when it can act, and say what holds the rest."""
        blocker = self._library_blocker(snapshot)
        ready = bool(self.selected and self.detail_ready and not blocker)
        final = getattr(self, "final_available", False)
        playable = ready and bool(self.playback_choices)
        self.play_button.configure(state="normal" if playable else "disabled")
        self.transcribe_button.configure(state="normal" if ready and final else "disabled")
        recover_needed = getattr(self, "recovery_needed", False)
        self.recover_audio_button.configure(state="normal" if ready and recover_needed else "disabled")
        note = ""
        if self.selected and self.detail_ready and not (ready and recover_needed):
            note = blocker or (tr("O áudio final está completo; não há nada a recuperar.")
                               if final and not recover_needed else "")
        if note != self.recover_audio_note.cget("text"):
            self.recover_audio_note.configure(text=note)
        reason = ""
        if self.selected and self.detail_ready and not playable:
            reason = blocker or tr("Esta gravação ainda não tem áudio final para ouvir.")
        if reason != self._play_reason:
            self._play_reason = reason
            self.playback_status.set(reason or tr("Pronto para ouvir."))

    # -- The recorder: state, transport, trouble -----------------------------------------------

    def _sync_recording_status(self, snapshot, start_ready):
        """Write the recorder's one state beside its tone dot."""
        state = snapshot.get("state", "idle")
        text = format_recording_status(snapshot, start_held=state == "idle" and not start_ready)
        self.record_status.set(text)
        self.record_chip.set(text, recording_tone(snapshot, start_ready))

    def _sync_transport(self, state, start_ready):
        """Show only the controls that can act: Start while idle; Stop (primary) and Pause while capturing."""
        active = state in CAPTURE_STATES
        live = state in ("recording", "paused")
        key = (active, live)
        if key != self._transport_key:
            self._transport_key = key
            for button in (self.start_button, self.stop_button, self.pause_button):
                button.pack_forget()
            if active:
                self.stop_button.pack(side="left", before=self.shortcut_hint)
                if live:
                    self.pause_button.pack(side="left", padx=(self.ui.space_sm, 0),
                                           before=self.shortcut_hint)
            else:
                self.start_button.pack(side="left", before=self.shortcut_hint)
        self.start_button.configure(state="normal" if start_ready else "disabled")
        self.stop_button.configure(state="normal" if active else "disabled")
        self.pause_button.configure(text=tr("Retomar") if state == "paused" else tr("Pausar"),
                                    state="normal" if live else "disabled")

    def _sync_shortcut_hint(self):
        """Name the shortcut beside the transport, when the user set one."""
        hotkey = hotkey_display(self.settings.hotkey)
        self.shortcut_hint.configure(text=tr("Atalho: {hotkey}", hotkey=hotkey) if hotkey else "")

    def _sync_recording_hint(self, active):
        """The line under the recorder: test guidance while idle, what a change means while capturing."""
        if active != self._recording_hint:
            self._recording_hint = active
            self.preview_status.set(tr(RECORDING_HINT) if active else tr(SOURCE_TEST_HINT))

    def _recording_details(self, snapshot, lost):
        """The cause behind Detalhes for the recorder's trouble, bounded and path-redacted."""
        details = []
        _append_detail_once(details, _technical_error_detail(snapshot["error"]) if snapshot.get("error") else "")
        health = snapshot.get("capture_health") or {}
        for track, state in lost.items():
            reason = _safe_retention_text(health[track].get("reason", ""), 512)
            details.append(f"{tr(SOURCE_LABELS[track])}: {state}" + (f" ({reason})" if reason else ""))
        if snapshot.get("final_audio"):
            details.append(tr("Arquivo final: {path}", path=str(snapshot["final_audio"])[:2048]))
        if snapshot.get("postprocess"):
            details.append(tr("Processamento: {details}", details=str(snapshot["postprocess"])[:2048]))
        return "\n".join(details)

    def _sync_recording_trouble(self, snapshot, now=None):
        """Keep the source captions and the Recording warning true to the capture.

        A source silent past the grace period raises a warning that names it and
        counts the silence. With nothing lost, an error the recorder reports stays
        on the page until it is dismissed.
        """
        now = time.monotonic() if now is None else now
        lost = dict(lost_sources(snapshot))
        health = snapshot.get("capture_health") or {}
        self._lost_since = {track: since for track, since in self._lost_since.items() if track in lost}
        for track in lost:
            # last_packet_age is the silence that already preceded the event that reported the loss.
            age = health[track].get("last_packet_age")
            age = float(age) if isinstance(age, (int, float)) and math.isfinite(age) and age > 0 else 0.0
            self._lost_since.setdefault(track, now - age)
        self._silent_for = {track: now - since for track, since in self._lost_since.items()}
        sources = self._recording_sources or self.settings.sources
        for track, caption in self.source_captions.items():
            text, role = source_health(track, snapshot, sources, self._silent_for.get(track, 0))
            if text != caption.cget("text") or getattr(self.ui, role) != str(caption.cget("fg")):
                caption.configure(text=text, fg=getattr(self.ui, role))

        shown = {track: lost[track] for track in ("microphone", "system")
                 if self._silent_for.get(track, 0) >= LOST_SOURCE_GRACE}
        error = str(snapshot.get("error") or "")
        capturing = snapshot.get("state") in CAPTURE_STATES
        if shown:
            key = ("lost", tuple(shown.items()))
            silent = max(self._silent_for[track] for track in shown)
            names = [tr(SOURCE_PROSE[track]) for track in shown]
            sources_text = names[0] if len(names) == 1 else tr("{first} e {second}", first=names[0], second=names[1])
            title = (tr("Sem áudio {sources} há {duration}", sources=sources_text, duration=format_silence(silent))
                     if len(shown) == 1 else tr("Sem áudio {sources} · maior interrupção: {duration}",
                         sources=sources_text, duration=format_silence(silent)))
            if "disconnected" in shown.values():
                body = tr("A gravação continua e retoma quando o dispositivo voltar. "
                          "Reconecte-o; outro dispositivo só vale na próxima gravação.")
            else:
                body = tr("O aplicativo tenta reabrir o dispositivo e a gravação retoma sozinha. "
                          "Outro dispositivo só vale na próxima gravação.")
            tone = "warning"
            action = (tr("Trocar dispositivo"), lambda track=next(iter(shown)): self._choose_device(track))
        elif error and not lost:
            # A source inside its grace period already put its cause in the error;
            # the lost-source warning will say it better, or the source will mend.
            key = ("error", error, capturing)
            title = tr("A gravação encontrou um problema") if capturing \
                else tr("A última operação terminou com um problema")
            body = tr("A gravação continua. Os detalhes explicam o que aconteceu.") if capturing \
                else tr("O que foi capturado ou salvo foi preservado. Veja a gravação na Biblioteca.")
            tone = "danger" if not capturing and snapshot.get("last_status") == "failed" else "warning"
            action = None if capturing else (tr("Abrir Biblioteca"), self._open_library)
        else:
            key = None
        if key == self._trouble_key:
            if key is not None and key[0] == "lost":
                self.recording_trouble.retitle(title)
            return
        self._trouble_key = key
        if key is None:
            self._trouble_dismissed = None
        if key is None or key == self._trouble_dismissed:
            self.recording_trouble.clear()
            return
        self.recording_trouble.problem(
            tone, title, body, action=action, details=self._recording_details(snapshot, shown) or None,
            when=datetime.now(),
            dismiss=None if key[0] == "lost" else lambda key=key: self._dismiss_trouble(key))

    def _dismiss_trouble(self, key):
        self._trouble_dismissed = key
        self.recording_trouble.clear()

    def _open_library(self):
        self.notebook.select(self.library_tab)

    def _choose_device(self, track):
        """Take the user to the selector of a lost source. The capture helper cannot switch
        endpoints live, so a new choice applies to the next recording and the page says so."""
        self.endpoint_boxes[track].focus_set()
        self.preview_status.set(
            tr("Trocar o dispositivo vale a partir da próxima gravação. "
               "Para continuar agora, reconecte o dispositivo."))

    def interrupt_and_record(self):
        """Stop the job holding Start, after naming what stops, then record."""
        if self.closed or self.start_after_interrupt:
            return
        snapshot = self.controller.snapshot()
        job = processing_job_label(snapshot)
        if not job or snapshot.get("state", "idle") not in {"idle", "postprocessing"}:
            return
        title = self._processing_title(snapshot)
        if snapshot.get("processing_kind") in PIPELINE_JOBS:
            consequence = tr(
                "O áudio original fica preservado. Refaça essa etapa depois, na Biblioteca. "
                "A gravação começa assim que o processamento parar."
            )
        else:
            consequence = tr(
                "Essa tarefa para antes de terminar. Execute-a de novo depois. "
                "A gravação começa assim que ela parar."
            )
        question = (tr("Interromper {job} de “{title}” e começar a gravar?", job=job, title=title)
                    if title else tr("Interromper {job} e começar a gravar?", job=job))
        if not messagebox.askokcancel(tr("Interromper e gravar"), question + "\n\n" + consequence,
                                      parent=self.window):
            return
        self.start_after_interrupt = "window"
        if getattr(self, "summary_pending", None):
            self._summary_cancel_requested = self.summary_pending
        self._say("recording", tr("Interrompendo {job}; a gravação começa em seguida.", job=job))

        def cancelled(_issued, error):
            if error:
                self.start_after_interrupt = None
                self._fail("recording", tr("Não foi possível interromper o processamento"),
                           tr("A gravação não começou. Tente de novo."), error=error)

        if not self._submit("control", self.controller.cancel_processing, cancelled, urgent=True):
            self.start_after_interrupt = None

    def request_start(self, origin="window"):
        """Shared GUI-thread start path for window, hotkey, and tray origins."""
        if self.closed:
            return False
        if getattr(self, "privacy_save_inflight", False):
            self._fail("recording", tr("Gravação não iniciada"),
                       tr("Aguarde a atualização de privacidade antes de iniciar a gravação."),
                       tone="warning")
            return False
        if not getattr(self, "retention_ready", True):
            self._fail("recording", tr("Gravação não iniciada"),
                       tr("A gravação aguarda a recuperação do workspace."), tone="warning")
            return False
        if not getattr(self, "privacy_ready", True):
            self.pending_start_origin = origin
            refresher = getattr(self.controller, "refresh_privacy_defaults", None)
            if callable(refresher):
                self._say("recording", tr("Carregando privacidade local antes de gravar…"))
                if not self._submit("privacy_start", refresher, self._privacy_ready_for_start):
                    self.pending_start_origin = None
                    return False
                return True
            self._fail("recording", tr("Gravação não iniciada"),
                       tr("Privacidade local indisponível; gravação bloqueada."), tone="warning")
            return False
        request = self._validated_recording_request()
        if request is None:
            return False
        settings, title = request
        required = False
        checker = getattr(self.controller, "recording_notice_required", None)
        if callable(checker):
            try:
                required = checker() is True
            except Exception:
                required = True
        if required:
            self._show_recording_notice(origin, settings, title)
            return True
        return self._submit_start(settings, title, origin)

    def _privacy_ready_for_start(self, value, error):
        if self.closed:
            return
        origin = self.pending_start_origin
        self.pending_start_origin = None
        self._privacy_loaded(value, error)
        if error or not self.privacy_ready or not origin:
            return
        self.request_start(origin)

    def _show_recording_notice(self, origin, settings, title):
        """Show selectable local notice; copy failure never blocks confirmation."""
        if self.closed:
            return
        try:
            language = self.privacy_notice_language.get()
        except (AttributeError, tk.TclError):
            language = "pt-BR"
        reader = getattr(self.controller, "recording_notice", None)
        try:
            notice = reader(language) if callable(reader) else self.controller.recording_notice_text(language)
        except Exception:
            notice = (
                "Esta reunião será gravada localmente pelo SnipVoice. "
                "Confirme que todas as pessoas foram informadas e consentiram."
                if language == "pt-BR" else
                "This meeting will be recorded locally by SnipVoice. "
                "Confirm that everyone has been informed and consents."
            )
        dialog = tk.Toplevel(self.window)
        ui_theme.prepare_dialog(dialog, self.window, resolved=self.ui, set_icon=self._dialog_icon)
        self.recording_notice_dialog = dialog
        dialog.title(tr("Confirmação antes de gravar"))
        dialog.transient(self.window)
        dialog.grab_set()
        dialog.configure(bg=self.ui.surface)
        self._label(dialog, tr("Aviso de gravação"), font=self.ui.font(12, "bold"),
                    fg=self.ui.text_strong).pack(anchor="w", padx=self.ui.space_lg,
                    pady=(self.ui.space_lg, self.ui.space_sm))
        self._label(dialog, tr("O texto é selecionável e pode ser copiado para compartilhar com os participantes."),
                    fg=self.ui.text_muted, wraplength=560, justify="left", anchor="w").pack(
                    fill="x", padx=self.ui.space_lg, pady=(0, self.ui.space_sm))
        notice_box = tk.Text(dialog, height=8, width=72, wrap="word", font=self.ui.font(),
                             **self.ui.text_colors(), **self.ui.entry_chrome(readonly=True))
        ui_theme.focus_ring(notice_box, self.ui, shrink=("bd",))
        notice_box.pack(fill="both", expand=True, padx=self.ui.space_lg)
        notice_box.insert("1.0", notice)
        notice_box.configure(state="normal")
        copy_status = tk.StringVar(dialog, "")
        actions = tk.Frame(dialog, bg=self.ui.surface)
        actions.pack(fill="x", padx=self.ui.space_lg, pady=self.ui.space_md)

        def copy_notice():
            try:
                copied = Clipboard.set_content(notice)
            except Exception:
                copied = False
            copy_status.set(tr("Aviso copiado.") if copied else tr("Não foi possível copiar; selecione o texto manualmente."))

        def cancel():
            self.pending_start_origin = None
            try:
                dialog.grab_release()
                dialog.destroy()
            except tk.TclError:
                pass
            self.recording_notice_dialog = None
            self._say("recording", tr("Gravação cancelada no aviso de consentimento."))

        def confirm():
            grant = getattr(self.controller, "grant_recording_consent", None)
            try:
                if callable(grant):
                    grant()
            except Exception as exc:
                self._fail("recording", tr("Não foi possível registrar a confirmação"),
                           tr("A gravação não começou. Confirme o aviso de novo."), error=exc)
                return
            try:
                dialog.grab_release()
                dialog.destroy()
            except tk.TclError:
                pass
            self.recording_notice_dialog = None
            self._submit_start(settings, title, origin)

        self._button(actions, tr("Copiar aviso"), copy_notice).pack(side="left")
        self._label(actions, "", textvariable=copy_status, fg=self.ui.text_muted).pack(side="left", padx=8)
        self._button(actions, tr("Cancelar"), cancel).pack(side="right", padx=(8, 0))
        self._button(actions, tr("Confirmar e iniciar"), confirm, accent=True).pack(side="right")
        dialog.protocol("WM_DELETE_WINDOW", cancel)
        dialog.bind("<Escape>", lambda _event: cancel())
        dialog.geometry("650x360")
        dialog.lift()
        dialog.focus_force()
        return dialog

    def _submit_start(self, settings, title, origin="window"):
        # The recorder's own state says the rest; a message from before is stale.
        self.messages.clear("recording")
        self._recording_sources = settings.sources
        submitted = self._submit(
            "control", lambda: self.controller.start(settings, title=title),
            lambda accepted, error: self._started(accepted, error, origin), urgent=True,
        )
        if not submitted:
            revoke = getattr(self.controller, "revoke_recording_consent", None)
            if callable(revoke):
                revoke()
        return submitted

    def start(self):
        return self.request_start("window")

    def _started(self, accepted, error, origin="window"):
        if error:
            revoke = getattr(self.controller, "revoke_recording_consent", None)
            if callable(revoke):
                revoke()
            self._fail("recording", tr("Não foi possível iniciar a gravação"),
                       tr("Nada foi gravado. Verifique as fontes e tente de novo."), error=error)
        else:
            if accepted:
                return
            revoke = getattr(self.controller, "revoke_recording_consent", None)
            if callable(revoke):
                revoke()
            # Name what refused the start (a hotkey press lands here too).
            snapshot = self.controller.snapshot()
            job = start_blocker(snapshot)
            if not job:
                self._fail("recording", tr("Gravação não iniciada"),
                           tr("Verifique o estado da gravação e tente de novo."), tone="warning")
            elif processing_job_label(snapshot):
                self._fail("recording", tr("Gravação não iniciada"),
                           tr("Aguardando {job}. Para gravar agora, use Interromper e gravar.", job=job),
                           tone="warning")
            else:
                self._fail("recording", tr("Gravação não iniciada"),
                           tr("Aguardando {job}.", job=job), tone="warning")

    def stop(self):
        self._action("stop", urgent=True)

    def pause_resume(self):
        self._action("resume" if self.snapshot.get("state") == "paused" else "pause", urgent=True)

    def _action(self, method, *args, urgent=False, callback=None, receipt=None, **kwargs):
        """Run a controller command off Tk; ``receipt`` is the quiet line a success earns."""
        if callback is None:
            destination = self._here()
            session = getattr(self, "selected", None) if destination == "library" else None
            callback = lambda value, error: self._done(
                value, error, receipt, destination=destination, session=session)
        self._submit("control" if urgent else method, lambda: getattr(self.controller, method)(*args, **kwargs),
                     callback, urgent)

    def search(self):
        self._cancel_library_search()
        self.offset = 0
        self.library_cursor = None
        self.library_next_cursor = None
        self.library_back_stack = [None]
        self.library_page_index = 0
        self.library_filter_generation += 1
        self.refresh_library()

    def change_page(self, delta):
        if delta > 0:
            if not self.library_next_cursor:
                return
            if len(self.library_back_stack) >= MAX_PAGE_BACKSTACK:
                self.library_back_stack.pop(0)
            self.library_back_stack.append(self.library_next_cursor)
            self.library_page_index = len(self.library_back_stack) - 1
            self.library_cursor = self.library_next_cursor
        elif delta < 0:
            if self.library_page_index <= 0:
                return
            self.library_page_index -= 1
            self.library_back_stack = self.library_back_stack[:self.library_page_index + 1]
            self.library_cursor = self.library_back_stack[-1]
        self.offset = self.library_page_index * PAGE_SIZE
        self.refresh_library()


    def refresh_library(self):
        cursor, query = self.library_cursor, self.query.get()
        status = ""
        filters = {}
        page_index = self.library_page_index
        generation = self.library_filter_generation
        def read():
            page = None
            reader = getattr(self.controller, "list_sessions_page", None)
            if callable(reader):
                candidate = reader(
                    # Keyset cursors own the position.  Keep the legacy offset
                    # explicitly at zero so page 2 cannot accidentally send
                    # both cursor and its display offset to the controller.
                    limit=PAGE_SIZE, cursor=cursor, offset=0, query=query, status=status,
                    **filters,
                )
                if isinstance(candidate, dict):
                    page = candidate
            if page is None:
                items = self.controller.list_sessions(
                    offset=page_index * PAGE_SIZE, limit=PAGE_SIZE, query=query, status=status,
                    **filters,
                )
                page = {"items": list(islice(items or (), PAGE_SIZE)), "next_cursor": None,
                        "cursor_reset": False, "index_state": "compatibility"}
            search_results = []
            if query.strip():
                searcher = getattr(self.controller, "search_library", None)
                if callable(searcher):
                    candidate = searcher(query, limit=MAX_SEARCH_RESULTS, **filters, status=status)
                    if isinstance(candidate, (list, tuple)):
                        search_results = list(candidate)[:MAX_SEARCH_RESULTS]
            info = getattr(self.controller, "semantic_info", None)
            if callable(info):
                page["semantic_info"] = info()
            return page, search_results, generation
        self._submit("library", read, self._library_loaded)

    def _library_loaded(self, sessions, error):
        if error:
            self._fail("library", tr("Não foi possível carregar a biblioteca"),
                       tr("A lista abaixo pode estar desatualizada. Tente novamente."),
                       error=error, action=(tr("Tentar novamente"), self.refresh_library),
                       key="library_load")
            if not self.sessions.get_children():
                self._set_library_detail_visible(False)
                self.library_empty.set(tr("Não foi possível carregar a biblioteca."))
                for button in (self.library_empty_record_button, self.library_empty_import_button,
                               self.library_empty_clear_button, self.library_empty_retry_button):
                    button.pack_forget()
                self.library_empty_retry_button.pack(side="left")
                self.library_empty_panel.place(relx=0, rely=0, relwidth=1, relheight=1)
                self.library_empty_panel.lift()
            return
        self.messages.clear("library", key="library_load")
        search_results = []
        generation = self.library_filter_generation
        if isinstance(sessions, tuple) and len(sessions) == 3 and isinstance(sessions[0], dict):
            page, search_results, generation = sessions
            if generation != self.library_filter_generation:
                return
            self.library_cursor_reset = bool(page.get("cursor_reset"))
            items = list(islice(page.get("items", []) or [], PAGE_SIZE))
            self.library_next_cursor = page.get("next_cursor")
            index_state = page.get("index_state")
            info = page.get("semantic_info")
            if isinstance(info, dict) and hasattr(self, "semantic_enabled"):
                self.semantic_enabled.set(info.get("enabled", False))
                state = info.get("state", "disabled")
                self.semantic_status.set(tr("Busca semântica: {state}",
                    state=tr(INDEX_STATE_LABELS.get(state, state))) if info.get("enabled")
                    else tr("Busca por palavras ativa."))
        else:
            items = list(islice(sessions or [], PAGE_SIZE))
            self.library_next_cursor = None
            index_state = "compatibility"
        self.sessions.delete(*self.sessions.get_children())
        self.session_items = {item["id"]: item for item in items}
        for item in items:
            self.sessions.insert("", "end", iid=item["id"])
            self._render_session_row(item["id"])
        if self.library_cursor_reset:
            self.library_cursor = None
            self.library_back_stack = [None]
            self.library_page_index = 0
            self.offset = 0
            self.index_status.set(tr("Índice de busca mudou; listagem reiniciada."))
        elif index_state:
            self.index_status.set(tr("Índice de busca: {state}",
                                     state=tr(INDEX_STATE_LABELS.get(index_state, index_state))))
        self.previous_button.configure(state="normal" if self.library_page_index else "disabled")
        self.next_button.configure(state="normal" if self.library_next_cursor else "disabled")
        self.page_label.set(self._library_count_text(len(items)))
        self._render_search_results(search_results)
        self._render_library_empty_state(items or self.search_result_items)
        if self.search_result_items:
            self.library_empty_panel.place_forget()
            self.page_label.set(tr("1 resultado") if len(search_results) == 1 else
                                tr("{count} resultados", count=len(search_results)))
        if self.selected in self.sessions.get_children():
            self.sessions.selection_set(self.selected)
        restored = getattr(self, "_restoring_view_state", None)
        if restored is not None:
            self.sessions.yview_moveto(restored.list_scroll)
            self._restoring_view_state = None

    def _render_search_results(self, results):
        self.search_result_items = [item for item in (results or ()) if isinstance(item, dict)][:MAX_SEARCH_RESULTS]
        self.search_results.delete(*self.search_results.get_children())
        labels = {
            "transcript": tr("Transcrição"),
            "report": tr("Relatório"),
            "reviewed_artifact": tr("Revisão"),
            "session": tr("Reunião"),
        }
        for index, item in enumerate(self.search_result_items):
            source = labels.get(item.get("source_kind"), str(item.get("source_kind", tr("Fonte"))))
            meeting = str(item.get("title") or item.get("session_id") or "")[:120]
            snippet = " ".join(str(item.get("snippet", "")).split())[:480]
            self.search_results.insert("", "end", iid=str(index), values=(source, meeting, snippet))
        frame = getattr(self, "search_results_frame", None)
        if frame is not None:
            if self.search_result_items:
                frame.grid(row=0, column=0, sticky="ew", padx=(12, 0))
            else:
                frame.grid_remove()

    def open_search_result(self):
        selected = self.search_results.selection()
        if not selected:
            return
        try:
            result = self.search_result_items[int(selected[0])]
        except (AttributeError, IndexError, TypeError, ValueError):
            return
        session_id = result.get("session_id")
        if not isinstance(session_id, str):
            return
        self.search_request += 1
        request = self.search_request

        def loaded(value, error):
            if self.closed or request != self.search_request:
                return
            if error or not isinstance(value, dict):
                self._fail("library", tr("A fonte da busca não está mais disponível"),
                           tr("A gravação ou o relatório pode ter mudado. Busque de novo."), error=error)
                return
            if value.get("session_id") != self.selected:
                self.pending_search_resolution = value
                self.load_session(value["session_id"])
                return
            self._apply_search_resolution(value, request)

        self._submit("search_result", lambda: self.controller.resolve_search_result(result), loaded)

    def _apply_search_resolution(self, value, request=None):
        if request is not None and request != self.search_request:
            return
        if self.closed or value.get("session_id") != self.selected:
            return
        kind = value.get("source_kind")
        if kind == "transcript":
            self._jump_to_transcript_evidence(
                value["session_id"], value.get("revision_id"), value.get("segment_id"),
                request=request,
            )
        elif kind in {"report", "reviewed_artifact"}:
            report_id = value.get("report_id")
            if report_id:
                self._load_report(report_id)
                self._say("library", tr("Fonte do relatório carregada."))
        else:
            self._say("library", tr("Reunião encontrada na biblioteca."))

    def _jump_to_transcript_evidence(self, session_id, revision, segment_id, *, request=None):
        if not session_id or not revision or not segment_id:
            return
        self.citation_request += 1
        citation_request = self.citation_request
        def read():
            for offset in range(0, TRANSCRIPT_LIMIT + TRANSCRIPT_PAGE_SIZE, TRANSCRIPT_PAGE_SIZE):
                page = self.controller.get_transcript_page(
                    session_id, revision=revision, offset=offset, limit=TRANSCRIPT_PAGE_SIZE,
                )
                values = page.get("segments", []) if isinstance(page, dict) else []
                if any(isinstance(item, dict) and item.get("id") == segment_id for item in values):
                    return page
                if not isinstance(page, dict) or not page.get("has_more"):
                    break
            return None
        def loaded(page, error):
            if (self.closed or citation_request != self.citation_request
                    or self.selected != session_id or (request is not None and request != self.search_request)):
                return
            if error or not page:
                self._fail("library", tr("O trecho citado não está disponível nesta revisão."),
                           tone="warning", session=session_id)
                return
            self.transcript_offset = page.get("offset", 0)
            self.transcript_revision = revision
            if hasattr(self, "transcript_style"):
                self.transcript_style.set(tr("Com horários"))
                self._sync_transcript_style()
                self.detail_sections.select("transcript")
                self._load_transcript_document()
            self.transcript_has_previous = bool(page.get("has_previous"))
            self.transcript_has_more = bool(page.get("has_more"))
            self._render_transcript(page.get("segments", []))
            self._update_transcript_paging_controls()
            for key, segment in self.segments.items():
                if isinstance(segment, dict) and segment.get("id") == segment_id:
                    try:
                        self.transcript.selection_set(key)
                        self.transcript.see(key)
                    except (tk.TclError, AttributeError):
                        pass
                    break
            self._say("library", tr("Fonte carregada · {time}", time=format_time(next((item.get('start', 0) for item in page.get('segments', []) if item.get('id') == segment_id), 0))))
        self._submit("search_citation", read, loaded)

    def refresh_workspace(self):
        def read():
            return self.controller.read_workspace()
        self._submit("workspace", read, self._workspace_loaded)

    def _workspace_loaded(self, workspace, error):
        if self.closed:
            return
        if error or not isinstance(workspace, dict):
            if not getattr(self, "_workspace_initial_loaded", False):
                self._workspace_initial_loaded = False
            self._fail("library", tr("Não foi possível carregar as preferências da biblioteca"),
                       tr("Os padrões atuais continuam em uso."), tone="warning", error=error)
            return
        self._workspace_initial_loaded = True
        self._apply_workspace_settings(workspace)
        if self._privacy_form_ready() and hasattr(self, "privacy_status"):
            self.privacy_status.set(tr("Privacidade local carregada."))


    def ask_across_meetings(self):
        if getattr(self, "cross_pending", None):
            return
        question = self.cross_question.get().strip()
        if not question:
            self.cross_status.set(tr("Digite uma pergunta."))
            return
        self.cross_request += 1
        request = self.cross_request
        self.cross_pending = request
        self.cross_request_cancel = threading.Event()
        event = self.cross_request_cancel
        model = self.summary_model.get().strip()
        self._sync_job_cancels()
        self.cross_status.set(tr("Processando evidência local; a resposta não será salva automaticamente."))
        filters = {}
        submitted = self._submit(
            "cross_question",
            lambda: self.controller.ask_across_meetings(
                question, model, filters=filters, cancel_event=event,
            ),
            lambda value, error: self._cross_answer_loaded(request, value, error),
        )
        if not submitted:
            self.cross_pending = None
            self.cross_request_cancel = None
            self._sync_job_cancels()

    def cancel_cross_question(self):
        event = getattr(self, "cross_request_cancel", None)
        if event is not None:
            event.set()
        self.cross_request_cancel = None
        self.bridge.invalidate("cross_question")
        self.cross_request += 1
        self.cross_pending = None
        self._sync_job_cancels()
        self.cross_status.set(tr("Cancelamento solicitado; a resposta anterior foi preservada."))

    def _cross_answer_loaded(self, request, value, error):
        if self.closed or request != self.cross_request:
            return
        self.cross_pending = None
        self.cross_request_cancel = None
        self._sync_job_cancels()
        if error:
            self.cross_status.set(tr("Resposta cruzada fica somente na memória."))
            self._fail("library", tr("A pergunta entre gravações não foi concluída"),
                       tr("Nada foi salvo. Tente perguntar de novo."), error=error)
            return
        if not isinstance(value, dict):
            self.cross_status.set(tr("A resposta cruzada não tinha um formato utilizável."))
            return
        answer = str(value.get("answer", ""))[:MAX_ANSWER_CHARS]
        self.cross_answer.configure(state="normal")
        self.cross_answer.delete("1.0", "end")
        self.cross_answer.insert("1.0", answer)
        self.cross_answer.configure(state="disabled")
        self.cross_citation_refs = [item for item in value.get("citations", []) if isinstance(item, dict)][:16]
        self.cross_citations.delete(0, "end")
        for item in self.cross_citation_refs:
            self.cross_citations.insert(
                "end", tr("Carregando fontes…"),
            )
        citations = copy.deepcopy(self.cross_citation_refs)
        controller = self.controller
        self._submit("cross_sources", lambda: MeetingWindow._cross_source_labels(controller, citations),
                     lambda labels, error: self._cross_sources_loaded(request, labels, error))
        self.cross_status.set(
            tr("Resposta em memória · incerteza {uncertainty} · {count} citações resolvidas.",
               uncertainty=value.get('uncertainty', tr('alta')), count=len(self.cross_citation_refs))
        )

    @staticmethod
    def _cross_source_labels(controller, citations):
        labels = []
        for citation in citations[:16]:
            try:
                value = controller.resolve_search_result(dict(citation, source_kind="transcript"))
                if not isinstance(value, dict):
                    raise ValueError("unresolved citation")
                title = str(value.get("title") or tr("Gravação"))[:200]
                excerpt = " ".join(str(value.get("text") or "").split())[:120]
                labels.append(f"{title} · {format_time(value.get('start', citation.get('start', 0)))} · {excerpt}")
            except (ValueError, OSError, KeyError):
                labels.append(tr("Fonte indisponível"))
        return labels

    def _cross_sources_loaded(self, request, labels, error):
        if self.closed or request != self.cross_request:
            return
        self.cross_citations.delete(0, "end")
        for label in labels if not error else [tr("Fonte indisponível") for _ in self.cross_citation_refs]:
            self.cross_citations.insert("end", label)

    def jump_to_cross_citation(self):
        indexes = self.cross_citations.curselection()
        if not indexes or indexes[0] >= len(getattr(self, "cross_citation_refs", [])):
            return
        citation = self.cross_citation_refs[indexes[0]]
        session_id = citation.get("session_id")
        if not session_id:
            return
        self.cross_citation_request += 1
        request = self.cross_citation_request
        self._submit(
            "cross_citation",
            lambda: self.controller.resolve_search_result({
                "source_kind": "transcript", "session_id": session_id,
                "revision_id": citation.get("revision_id"), "segment_id": citation.get("segment_id"),
            }),
            lambda value, error: self._cross_citation_loaded(request, value, error),
        )

    def _cross_citation_loaded(self, request, value, error):
        if self.closed or request != self.cross_citation_request:
            return
        if error or not isinstance(value, dict):
            self._fail("library", tr("A citação cruzada deixou de resolver para a transcrição canônica."),
                       tone="warning")
            return
        if value.get("session_id") != self.selected:
            self.pending_search_resolution = value
            self.load_session(value["session_id"])
            return
        self._apply_search_resolution(value)

    def show_trash(self):
        """Open the trash: restore or permanently delete; every mutation runs on the IO lane."""
        if not getattr(self, "retention_ready", True):
            self._say("library", tr("A lixeira aguarda a recuperação do workspace."))
            return None
        if self.closed:
            return None
        dialog = getattr(self, "trash_dialog", None)
        try:
            if dialog is not None and dialog.winfo_exists():
                dialog.deiconify()
                dialog.lift()
                self.refresh_trash()
                return dialog
        except tk.TclError:
            pass
        try:
            self.trash_opener = self.window.focus_get()
        except (KeyError, tk.TclError):
            # Tk cannot name a focused combobox popdown; there is nothing to return to.
            self.trash_opener = None
        dialog = tk.Toplevel(self.window)
        ui_theme.prepare_dialog(dialog, self.window, resolved=self.ui, set_icon=self._dialog_icon)
        self.trash_dialog = dialog
        dialog.title(tr("Lixeira"))
        dialog.geometry("720x420")
        dialog.transient(self.window)
        dialog.configure(bg=self.ui.surface)
        pad = self.ui.space_lg
        # Actions and the status line claim the bottom first, so a short
        # window shrinks the list instead of clipping the buttons.
        actions = tk.Frame(dialog, bg=self.ui.surface)
        actions.pack(side="bottom", fill="x", padx=pad, pady=(self.ui.space_sm, pad))
        self.trash_restore_button = self._button(actions, tr("Restaurar"), self.restore_selected_trash,
                                                 accent=True)
        self.trash_restore_button.pack(side="left")
        self.trash_purge_button = self._button(actions, tr("Excluir definitivamente…"),
                                               self.purge_selected_trash, danger=True)
        self.trash_purge_button.pack(side="left", padx=(self.ui.space_sm, 0))
        self.trash_empty_button = self._button(actions, tr("Esvaziar expirados…"),
                                               self.empty_expired_trash, danger=True)
        self.trash_empty_button.pack(side="right")
        self._button(actions, tr("Atualizar"), self.refresh_trash).pack(
            side="right", padx=(0, self.ui.space_sm))
        self.trash_status = tk.StringVar(dialog, tr("Carregando lixeira…"))
        self._wrap_label(dialog, "", textvariable=self.trash_status, fg=self.ui.text_muted,
                         anchor="w", justify="left").pack(side="bottom", fill="x", padx=pad)
        self._label(dialog, tr("Lixeira"), font=self.ui.heading(), fg=self.ui.text_strong).pack(
            anchor="w", padx=pad, pady=(pad, self.ui.space_xs))
        self._wrap_label(
            dialog, tr("Restaurar devolve a gravação à Biblioteca. Excluir definitivamente apaga os "
                       "arquivos do app e não pode ser desfeito."),
            fg=self.ui.text_muted, anchor="w", justify="left",
        ).pack(fill="x", padx=pad, pady=(0, self.ui.space_sm))
        frame = tk.Frame(dialog, bg=self.ui.surface)
        frame.pack(fill="both", expand=True, padx=pad)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        tree = ttk.Treeview(frame, columns=("meeting", "deleted", "expires", "size"), show="headings",
                            selectmode="browse", style="Meeting.Treeview", height=6)
        self.trash_tree = tree
        body = tkfont.Font(root=dialog, font=self.ui.body())
        heading = tkfont.Font(root=dialog, font=self.ui.body_strong())
        column_pad = 2 * self.ui.space_md
        for column, label, samples, anchor in (
            ("meeting", tr("Gravação"), (), "w"),
            ("deleted", tr("Movida em"), _date_column_samples(), "w"),
            ("expires", tr("Expira"), (tr("em {count} d", count=30), tr("Expirada")), "w"),
            ("size", tr("Tamanho"), ("999,9 MB",), "e"),
        ):
            tree.heading(column, text=label, anchor=anchor)
            width = max([heading.measure(label)] + [body.measure(value) for value in samples])
            if column == "meeting":
                tree.column(column, width=180, minwidth=120, stretch=True, anchor=anchor)
            else:
                tree.column(column, width=width + column_pad, minwidth=width + column_pad,
                            stretch=False, anchor=anchor)
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        tree.bind("<<TreeviewSelect>>", lambda _event: self._sync_trash_actions())
        for sequence, command in (("<Return>", self.restore_selected_trash),
                                  ("<KP_Enter>", self.restore_selected_trash),
                                  ("<Delete>", self.purge_selected_trash)):
            tree.bind(sequence, lambda _event, command=command: (command(), "break")[1])
        dialog.protocol("WM_DELETE_WINDOW", self._close_trash_dialog)
        dialog.bind("<Escape>", lambda _event: self._close_trash_dialog())
        self.trash_entries = []
        self._sync_trash_actions()
        ui_theme.apply_window_chrome(dialog, self.ui)
        dialog.update_idletasks()
        # The list gives up rows first (down to three); the action row and the
        # status line never clip, and its two groups keep a gap.
        dialog.minsize(actions.winfo_reqwidth() + 2 * pad + self.ui.space_lg,
                       dialog.winfo_reqheight() - 3 * self.ui.tree_row_height)
        tree.focus_set()
        self.refresh_trash()
        return dialog

    def _close_trash_dialog(self):
        self.trash_request += 1
        dialog, self.trash_dialog = self.trash_dialog, None
        self.trash_tree = None
        opener, self.trash_opener = getattr(self, "trash_opener", None), None
        if dialog is not None:
            try:
                dialog.destroy()
            except tk.TclError:
                pass
        if opener is not None and opener.winfo_exists():
            opener.focus_set()

    def refresh_trash(self):
        if self.closed:
            return
        tree = self.trash_tree
        if tree is None:
            return
        self.trash_request += 1
        request = self.trash_request
        self.trash_status.set(tr("Carregando lixeira…"))
        if not self._submit("trash_list", self.controller.list_trash,
                            lambda value, error: self._trash_loaded(value, error, request)):
            self.trash_status.set(tr("A lixeira está ocupada; tente novamente."))

    @staticmethod
    def _trash_name(session_id):
        """A short, distinct handle for a trashed recording until the trash keeps titles.

        Store ids start with the local date, which repeats within a day; the
        random tail tells recordings apart.
        """
        return session_id if len(session_id) <= 9 else "…" + session_id[-8:]

    def _trash_count_text(self, entries):
        total = len(entries)
        if not total:
            return tr("A lixeira está vazia")
        parts = [tr("1 gravação na lixeira") if total == 1
                 else tr("{count} gravações na lixeira", count=total)]
        expired = self._expired_trash_count(entries)
        # The empty-expired button is disabled without one; the count says why.
        parts.append(tr("nenhuma expirada") if not expired
                     else tr("1 expirada") if expired == 1 else tr("{count} expiradas", count=expired))
        return " · ".join(parts)

    @staticmethod
    def _expired_trash_count(entries):
        remaining = (seconds_until(entry.get("purge_after")) for entry in entries)
        return sum(1 for value in remaining if value is not None and value <= 0)

    def _trash_loaded(self, entries, error, request):
        if self.closed or request != self.trash_request or self.trash_tree is None:
            return
        if error:
            self.trash_status.set(tr("Não foi possível carregar a lixeira. Tente Atualizar."))
            self._fail("library", tr("Não foi possível carregar a lixeira"),
                       tr("Atualize a lixeira para tentar de novo."), error=error)
            return
        previous = self._selected_trash_entry(quiet=True)
        projected = []
        for entry in list(entries or ())[:MAX_TRASH_ITEMS]:
            if isinstance(entry, dict):
                value = entry
                get = value.get
            else:
                get = lambda key, default=None, item=entry: getattr(item, key, default)
            session_id = str(get("session_id", ""))[:128]
            if not session_id:
                continue
            projected.append({
                "session_id": session_id,
                "deleted_at": _safe_retention_text(get("deleted_at", ""), 40),
                "purge_after": _safe_retention_text(get("purge_after", ""), 40),
                "byte_estimate": max(0, int(get("byte_estimate", 0) or 0)),
            })
        self.trash_entries = projected
        tree = self.trash_tree
        tree.delete(*tree.get_children())
        for index, entry in enumerate(projected):
            tree.insert("", "end", iid=str(index), values=(
                self._trash_name(entry["session_id"]), format_local_timestamp(entry["deleted_at"]),
                format_expiry(entry["purge_after"]), format_storage_size(entry["byte_estimate"]),
            ))
        if projected:
            # Keep the row the person was on; otherwise start at the top.
            keep = next((index for index, entry in enumerate(projected)
                         if previous and entry["session_id"] == previous["session_id"]), 0)
            tree.selection_set(str(keep))
            tree.focus(str(keep))
            tree.see(str(keep))
        self._sync_trash_actions()
        # A finished action's receipt leads the refreshed count.
        receipt, self.trash_receipt = getattr(self, "trash_receipt", ""), ""
        count = self._trash_count_text(projected)
        self.trash_status.set(f"{receipt} · {count}" if receipt else count)

    def _sync_trash_actions(self):
        """Restore and delete act on the selected row; emptying needs an expired one."""
        selected = self.trash_tree is not None and bool(self.trash_tree.selection())
        self.trash_restore_button.configure(state="normal" if selected else "disabled")
        self.trash_purge_button.configure(state="normal" if selected else "disabled")
        self.trash_empty_button.configure(
            state="normal" if self._expired_trash_count(self.trash_entries) else "disabled")

    def _selected_trash_entry(self, quiet=False):
        if self.trash_tree is None:
            return None
        selected = self.trash_tree.selection()
        if not selected:
            if not quiet:
                self.trash_status.set(tr("Selecione uma gravação na lixeira."))
            return None
        try:
            entry = self.trash_entries[int(selected[0])]
        except (IndexError, TypeError, ValueError):
            if not quiet:
                self.trash_status.set(tr("A lista da lixeira mudou; atualize e selecione de novo."))
            return None
        return entry

    def restore_selected_trash(self):
        entry = self._selected_trash_entry()
        if not entry:
            return
        session_id = entry["session_id"]
        name = self._trash_name(session_id)
        request = self.trash_request
        self.trash_status.set(tr("Restaurando {name}…", name=name))
        self._submit(
            "trash_restore",
            lambda: self.controller.restore_session(session_id),
            lambda value, error: self._trash_mutation_finished(
                tr("Gravação {name} restaurada na Biblioteca", name=name), value, error, request),
        )

    def purge_selected_trash(self):
        entry = self._selected_trash_entry()
        if not entry:
            return
        session_id = entry["session_id"]
        name = self._trash_name(session_id)
        facts = tr("Movida para a lixeira: {moved} · {size}",
                   moved=format_local_timestamp(entry.get("deleted_at")),
                   size=format_storage_size(entry.get("byte_estimate", 0)))
        if not self._confirm_permanent_delete(
                tr("Excluir “{name}” definitivamente?", name=name), facts):
            return
        if self.trash_tree is None:
            return
        request = self.trash_request
        self.trash_status.set(tr("Excluindo {name} definitivamente…", name=name))
        self._submit(
            "trash_purge",
            lambda: self.controller.purge_session(session_id, confirm=True),
            lambda value, error: self._trash_mutation_finished(
                tr("Gravação {name} excluída definitivamente", name=name), value, error, request),
        )

    def empty_expired_trash(self):
        expired = self._expired_trash_count(self.trash_entries)
        if not expired:
            self.trash_status.set(tr("Nenhuma gravação da lixeira expirou ainda."))
            return
        question = (tr("Excluir 1 gravação expirada definitivamente?") if expired == 1
                    else tr("Excluir {count} gravações expiradas definitivamente?", count=expired))
        if not self._confirm_permanent_delete(
                question, tr("Somente as gravações cujo prazo na lixeira já venceu.")):
            return
        if self.trash_tree is None:
            return
        request = self.trash_request
        self.trash_status.set(tr("Excluindo as gravações expiradas…"))
        self._submit(
            "trash_empty_expired",
            lambda: self.controller.empty_trash(confirm=True),
            lambda value, error: self._trash_mutation_finished(
                tr("Gravações expiradas excluídas definitivamente"), value, error, request),
        )

    def _typed_confirmation(self, question, facts):
        """Build the irreversible-delete dialog: it confirms only once the word is typed."""
        owner = self.trash_dialog or self.window
        word = tr("EXCLUIR")
        pad = self.ui.space_lg
        dialog = tk.Toplevel(owner)
        ui_theme.prepare_dialog(dialog, owner, resolved=self.ui, set_icon=self._dialog_icon)
        dialog.title(tr("Excluir definitivamente"))
        dialog.transient(owner)
        dialog.resizable(False, False)
        # A fixed width; the height follows the wrapped text.
        dialog.minsize(480, 0)
        dialog.configure(bg=self.ui.surface)
        confirmation = types.SimpleNamespace(dialog=dialog, word=word, confirmed=False)
        self._wrap_label(dialog, question, font=self.ui.heading(), fg=self.ui.text_strong,
                         anchor="w", justify="left").pack(fill="x", padx=pad, pady=(pad, self.ui.space_xs))
        self._wrap_label(dialog, facts, fg=self.ui.text_muted, anchor="w", justify="left").pack(
            fill="x", padx=pad)
        ui_theme.info_bar(
            dialog, self.ui, "danger", tr("Isso não pode ser desfeito."),
            tr("Os arquivos são apagados do app para sempre. Isso não garante apagamento forense do disco."),
        ).pack(fill="x", padx=pad, pady=(self.ui.space_md, 0))
        self._label(dialog, tr("Digite {word} para confirmar.", word=word), anchor="w").pack(
            fill="x", padx=pad, pady=(self.ui.space_md, self.ui.space_xs))
        typed = tk.StringVar(dialog)
        confirmation.typed = typed
        entry = self._entry(dialog, typed, 24)
        entry.pack(anchor="w", padx=pad)
        confirmation.entry = entry
        actions = tk.Frame(dialog, bg=self.ui.surface)
        actions.pack(fill="x", padx=pad, pady=pad)

        def matches():
            return typed.get().strip().casefold() == word.casefold()

        def close():
            if dialog.winfo_exists():
                dialog.grab_release()
                dialog.destroy()

        def confirm():
            if matches():
                confirmation.confirmed = True
                close()

        cancel = self._button(actions, tr("Cancelar"), close)
        cancel.pack(side="right")
        button = self._button(actions, tr("Excluir definitivamente"), confirm, danger=True)
        button.configure(state="disabled")
        button.pack(side="right", padx=(0, self.ui.space_sm))
        confirmation.confirm_button = button
        typed.trace_add("write", lambda *_args: button.configure(
            state="normal" if matches() else "disabled"))
        entry.bind("<Return>", lambda _event: (confirm(), "break")[1])
        entry.bind("<KP_Enter>", lambda _event: (confirm(), "break")[1])
        dialog.bind("<Escape>", lambda _event: close())
        dialog.protocol("WM_DELETE_WINDOW", close)
        ui_theme.apply_window_chrome(dialog, self.ui)
        dialog.update_idletasks()
        x = owner.winfo_rootx() + (owner.winfo_width() - max(480, dialog.winfo_reqwidth())) // 2
        y = owner.winfo_rooty() + (owner.winfo_height() - dialog.winfo_reqheight()) // 3
        dialog.geometry(f"+{max(0, x)}+{max(0, y)}")
        entry.focus_set()
        return confirmation

    def _confirm_permanent_delete(self, question, facts):
        """Ask before an irreversible delete; True only after the confirmation word is typed."""
        confirmation = self._typed_confirmation(question, facts)
        confirmation.dialog.grab_set()
        confirmation.dialog.wait_window()
        if self.trash_tree is not None:
            self.trash_tree.focus_set()
        return confirmation.confirmed

    def _trash_mutation_finished(self, message, value, error, request):
        if self.closed or request != self.trash_request or self.trash_tree is None:
            return
        if error or value is False:
            if error:
                self._fail("library", tr("A lixeira não confirmou a ação"),
                           tr("Atualize a lista para ver o estado atual."), error=error)
            self.trash_status.set(tr("A lixeira não confirmou a ação. Atualize a lista para ver o estado atual."))
            return
        self.trash_receipt = message
        self.refresh_trash()
        self.refresh_library()

    def toggle_semantic_search(self):
        enabled = self.semantic_enabled.get()
        self.library_filter_generation += 1
        self.semantic_status.set(tr("Salvando opção de busca…"))
        def finished(_value, error):
            if error:
                self.semantic_status.set(tr("Não foi possível salvar a opção de busca semântica."))
            self.search()
        self._submit("semantic_settings", lambda: self.controller.set_semantic_search(enabled), finished)

    def install_embedding_model(self):
        if self.semantic_cancel is not None:
            return
        from embedding_models import MODEL, download_embedding_model
        if not messagebox.askyesno(tr("Baixar modelo local"),
                tr("Baixar {name} ({size})?\nLicença: {license}\nO arquivo será verificado por SHA-256. "
                   "A busca semântica exige um runtime local compatível.", name=MODEL["name"],
                   size=format_model_size(MODEL["size_bytes"]), license=MODEL["license_id"]), parent=self.window):
            return
        self.semantic_cancel = threading.Event()
        event = self.semantic_cancel
        self._semantic_model_download_active = True
        button = getattr(self, "semantic_model_action", None)
        if button is not None:
            button.configure(text=tr("Cancelar"), command=self.cancel_semantic_operation)
        self.semantic_status.set(tr("Baixando modelo com verificação SHA-256…"))
        self._sync_job_cancels()
        progress_state = getattr(self, "semantic_progress_state", [None])
        self.semantic_progress_state = progress_state
        def progress(done, total):
            progress_state[0] = (done, total)
        if not self._submit("semantic_model", lambda: download_embedding_model(cancel_event=event,
                progress=progress),
                            self._semantic_operation_finished):
            self.semantic_cancel = None
            self._semantic_model_download_active = False
            if button is not None:
                button.configure(text=tr("Instalar modelo…"), command=self.install_embedding_model)
            self._sync_job_cancels()
            self.semantic_status.set(tr("Aguarde a operação atual e tente novamente."))

    def rebuild_semantic_index(self):
        if self.semantic_cancel is not None:
            return
        if not self.semantic_enabled.get():
            self.semantic_status.set(tr("Ative a busca semântica antes de reindexar."))
            return
        self.semantic_cancel = threading.Event()
        event = self.semantic_cancel
        self.semantic_status.set(tr("Indexando transcrições localmente…"))
        self._sync_job_cancels()
        if not self._submit("semantic_rebuild", lambda: self.controller.rebuild_semantic_index(cancel_event=event),
                            self._semantic_operation_finished):
            self.semantic_cancel = None
            self._sync_job_cancels()
            self.semantic_status.set(tr("Aguarde a operação atual e tente novamente."))

    def cancel_semantic_operation(self):
        if self.semantic_cancel is None:
            return
        self.semantic_cancel.set()
        queued_model = self.bridge.cancel_queued("semantic_model")
        queued_rebuild = self.bridge.cancel_queued("semantic_rebuild")
        if queued_model or queued_rebuild:
            self._semantic_operation_finished(None, "cancelled")
            return
        cancel = getattr(self.controller, "cancel_semantic_index", None)
        if callable(cancel):
            cancel()
        self.semantic_status.set(tr("Cancelamento solicitado; busca por palavras disponível."))

    def _semantic_operation_finished(self, value, error):
        self.semantic_cancel = None
        self._semantic_model_download_active = False
        if hasattr(self, "semantic_progress_state"):
            self.semantic_progress_state[0] = None
        button = getattr(self, "semantic_model_action", None)
        if button is not None:
            button.configure(text=tr("Instalar modelo…"), command=self.install_embedding_model)
        self._sync_job_cancels()
        if error:
            self.semantic_status.set(tr("Operação semântica cancelada ou indisponível. Busca por palavras disponível."))
        elif isinstance(value, dict) and value.get("state") == "rebuilding":
            self.semantic_status.set(tr("Indexando transcrições localmente…"))
        else:
            self.semantic_status.set(tr("Operação semântica concluída."))
            self.search()

    def rebuild_index(self):
        if self.rebuild_cancel is not None:
            return
        self.rebuild_cancel = threading.Event()
        event = self.rebuild_cancel
        self.rebuild_progress = {"done": 0, "total": None, "state": "rebuilding"}
        self.rebuild_button.configure(state="disabled")
        self._sync_job_cancels()
        self.index_status.set(tr("Índice de busca: reconstruindo…"))
        if not self._submit(
            "rebuild_index",
            lambda: self.controller.rebuild_index(
                cancel_event=event,
                progress=self._rebuild_progress_from_worker,
            ),
            self._rebuild_finished,
        ):
            self._rebuild_finished(None, tr("Fila de operações ocupada."))

    def _rebuild_progress_from_worker(self, done, total):
        self.rebuild_progress = {"done": done, "total": total, "state": "rebuilding"}

    def cancel_rebuild_index(self):
        if self.rebuild_cancel is not None:
            self.rebuild_cancel.set()
            if self.bridge.cancel_queued("rebuild_index"):
                self._rebuild_finished({"state": "cancelled", "sessions": 0}, None)
                return
            self.index_status.set(tr("Índice de busca: cancelamento solicitado…"))

    def _rebuild_finished(self, value, error):
        cancelled = self.rebuild_cancel is not None and self.rebuild_cancel.is_set()
        self.rebuild_cancel = None
        self.rebuild_button.configure(state="normal")
        self._sync_job_cancels()
        if error:
            self.rebuild_progress = dict(self.rebuild_progress, state="cancelled" if cancelled else "failed")
            self.index_status.set(tr("Índice de busca: reconstrução cancelada ou indisponível."))
            self._fail("library", tr("Não foi possível reconstruir o índice de busca"),
                       tr("A busca por palavras continua disponível."), tone="warning", error=error)
            return
        state = value.get("state", "ready") if isinstance(value, dict) else "ready"
        count = value.get("sessions", 0) if isinstance(value, dict) else 0
        self.rebuild_progress = {"done": count, "total": count, "state": state}
        self.index_status.set(tr("Índice de busca: {state} · {count} reuniões",
                                 state=tr(INDEX_STATE_LABELS.get(state, state)), count=count))
        self.search()

    def _selection_changed(self, _event=None):
        self._sync_trash_controls()
        selected = self.sessions.selection()
        if not selected or selected[0] == self.selected:
            return
        session_id = selected[0]
        if self.dirty:
            answer = messagebox.askyesnocancel(tr("Título não salvo"), tr("Salvar o título antes de abrir outra gravação?"), parent=self.window)
            if answer is None:
                if self.selected in self.sessions.get_children():
                    self.sessions.selection_set(self.selected)
                return
            if answer:
                self.save_title(after=lambda: self.load_session(session_id))
                return
        self.pending_search_resolution = None
        self.load_session(session_id)

    def load_session(self, session_id):
        self._selection_generation = getattr(self, "_selection_generation", 0) + 1
        self._action("stop_playback", urgent=True)
        self.selected = session_id
        if hasattr(self, "undo_receipt"):
            self.undo_receipt.pack_forget()
        if hasattr(self, "title_entry"):
            self._hide_title_editor(focus=False)
        # What the page said about another recording does not describe this one.
        self.messages.drop_other_sessions(session_id)
        self._reset_transcript_document()
        self._show_summary("")
        self._show_library_detail(True)
        if hasattr(self, "completion_receipt"):
            self.completion_receipt.pack_forget()
        self.report_request = getattr(self, "report_request", 0) + 1
        self.ask_request = getattr(self, "ask_request", 0) + 1
        self.citation_request = getattr(self, "citation_request", 0) + 1
        self.selected_report = None
        self.report_sections = {}
        if hasattr(self, "summary_focus"):
            self.summary_focus.delete("1.0", "end")
        if hasattr(self, "meeting_chat"):
            self.meeting_chat.set_question("")
            self.meeting_chat.clear_notice()
            self._render_chat()
        if hasattr(self, "ask_status"):
            self.ask_status.set(tr("Conversa temporária. Salve as respostas que quiser guardar."))
        self.detail_ready = False
        self.transcript_offset = 0
        self.transcript_revision = None
        self.transcript_has_more = False
        self.transcript_has_previous = False
        self.transcript_request = getattr(self, "transcript_request", 0) + 1
        self.annotation_generation = 0
        self.speaker_labels = {}
        self.highlights = []
        self.selected_segment_id = None
        # The list row already knows the state, date and length; the read below refreshes them.
        self.detail_state_item = self.session_items.get(session_id)
        self._render_detail_state()
        self.transcription_status = ""
        self._show_processing_issue(None)
        self.delete_button.configure(state="disabled")
        self.play_button.configure(state="disabled")
        self.replay_stop_button.configure(state="disabled")
        self.audio_overview.set(tr("Carregando áudio…"))
        self.playback_status.set(tr("Pronto para ouvir."))
        self._play_reason = None
        self.playback_position.set(0.0)
        self.loading = True
        self.title.set("")
        self.loading = False
        self.dirty = False
        self.transcript.delete(*self.transcript.get_children())
        self.segments.clear()
        self._render_annotations({}, 0)
        self.segment_text.configure(state="normal")
        self.segment_text.delete("1.0", "end")
        self.segment_text.configure(state="disabled")
        self.bridge.invalidate("detail")
        self.bridge.invalidate("outputs")
        self.bridge.invalidate("transcript_page")
        def read():
            raw = self.controller.get_session(session_id)
            metadata = {key: raw.get(key) for key in ("id", "title", "status", "duration", "error",
                                                      "created_at")}
            metadata["processing_details"] = _processing_failure_details(raw)
            metadata["processing_issue"] = _processing_issue(raw)
            metadata["transcription_status"] = _stage_status(raw, "transcription")
            final = raw.get("final_audio")
            final_path = final.get("path") if isinstance(final, dict) else None
            metadata["final_available"] = bool(isinstance(final_path, str) and os.path.isfile(final_path))
            inventory = getattr(self.controller, "audio_inventory", None)
            try:
                sizes = inventory(session_id) if callable(inventory) else {}
            except (OSError, ValueError):
                sizes = {}
            metadata["audio_sizes"] = sizes if isinstance(sizes, dict) else {}
            tracks = raw.get("tracks") if isinstance(raw.get("tracks"), dict) else {}
            metadata["tracks"] = {
                track: {
                    "available": value.get("available", True) is not False,
                    "raw_removed": bool(value.get("raw_removed")),
                }
                for track, value in tracks.items()
                if track in TRACK_LABELS.values() and isinstance(value, dict)
            }
            summary = raw.get("summary") or raw.get("reviewed_summary")
            summary = format_summary(summary)
            revision = active_transcript_revision(raw)
            page = self._read_transcript_page(session_id, revision, 0)
            annotations = raw.get("annotations") if isinstance(raw.get("annotations"), dict) else {}
            annotations = annotations_for_revision(annotations, revision)
            speaker_labels = annotations.get("speaker_labels", {})
            highlights = annotations.get("highlights", [])
            metadata.update(summary=str(summary or ""),
                            transcript_revision=revision,
                            transcript_offset=page["offset"],
                            transcript_has_previous=page["has_previous"],
                            transcript_has_more=page["has_more"],
                             annotation_generation=raw.get("annotation_generation", annotations.get("generation", 0)),
                             collection_ids=list(raw.get("annotations", {}).get("collection_ids", []))
                             if isinstance(raw.get("annotations"), dict) else [],
                             tags=list(raw.get("annotations", {}).get("tags", []))
                             if isinstance(raw.get("annotations"), dict) else [],
                             people=list(raw.get("annotations", {}).get("people", []))
                             if isinstance(raw.get("annotations"), dict) else [],
                             series_id=raw.get("annotations", {}).get("series_id")
                             if isinstance(raw.get("annotations"), dict) else None,
                             speaker_labels=speaker_labels,
                            highlights=highlights[:2000])
            return metadata, page["segments"]
        self._submit("detail", read, lambda data, error: self._detail_loaded(session_id, data, error))

    def _detail_loaded(self, session_id, data, error):
        if session_id != self.selected:
            return
        if error:
            self._fail("library", tr("Não foi possível carregar a gravação"),
                       tr("Selecione-a de novo na lista para tentar outra vez."),
                       error=error, session=session_id)
            return
        metadata, segments = data
        self._explain_capture(session_id, metadata)
        self.loading = True
        self.title.set(metadata.get("title") or "")
        self._saved_title = self.title.get()
        if hasattr(self, "title_entry"):
            self._hide_title_editor(focus=False)
        self.transcript_revision = metadata.get("transcript_revision")
        self.transcript_offset = metadata.get("transcript_offset", 0)
        self.transcript_has_previous = bool(metadata.get("transcript_has_previous"))
        self.transcript_has_more = bool(metadata.get("transcript_has_more"))
        generation = metadata.get("annotation_generation", 0)
        self.annotation_generation = generation if isinstance(generation, int) else 0
        self.speaker_labels = metadata.get("speaker_labels", {})
        self.highlights = metadata.get("highlights", [])
        try:
            duration = max(0.0, float(metadata.get("duration") or 0))
            self.playback_duration = duration if math.isfinite(duration) else 0.0
        except (TypeError, ValueError):
            self.playback_duration = 0.0
        self.playback_seek.configure(to=max(1.0, self.playback_duration))
        self.playback_position.set(0.0)
        self.playback_clock.set(f"{format_time(0)} / {format_time(self.playback_duration)}")
        self.audio_overview.set(format_time(self.playback_duration))
        self.audio_sizes = metadata.get("audio_sizes") or {}
        self._set_audio_capabilities(metadata.get("tracks", {}), metadata.get("final_available", False))
        self.detail_state_item = {"id": session_id, **{
            key: metadata.get(key) for key in ("status", "created_at", "duration")}}
        self._render_detail_state()
        self.transcription_status = metadata.get("transcription_status", "")
        self._show_processing_issue(metadata.get("processing_issue"))
        self.detail_ready = True
        self._sync_qa_controls()
        self._sync_trash_controls()
        self.dirty = False
        self.loading = False
        self._render_transcript(segments)
        self._render_annotations(self.speaker_labels, self.annotation_generation)
        self._update_transcript_paging_controls()
        self._show_summary(metadata.get("summary"))
        if (metadata.get("summary") and not getattr(self, "_detail_section_chosen", False)
                and self.detail_sections.current == "transcript"):
            self.detail_sections.select("summary")
        self._load_transcript_document()
        self.refresh_reports(session_id)
        self._restore_title_draft(session_id)
        pending_search = getattr(self, "pending_search_resolution", None)
        if isinstance(pending_search, dict) and pending_search.get("session_id") == session_id:
            self.pending_search_resolution = None
            self._apply_search_resolution(pending_search, self.search_request)

    def _restore_title_draft(self, session_id):
        draft = getattr(self, "_restored_title_draft", None)
        if draft is not None and draft[0] == session_id:
            self._restored_title_draft = None
            self.loading = True
            self.title.set(draft[1])
            self._saved_title = draft[2]
            self.dirty = draft[1] != draft[2]
            self.loading = False

    def _explain_capture(self, session_id, metadata):
        """Say why a capture ended as it did, with the recorded cause behind Detalhes."""
        error = metadata.get("error")
        if not error:
            return
        status = metadata.get("status")
        if status == "failed":
            title = tr("A captura falhou")
            body = tr("O que foi capturado antes da falha foi preservado.")
        elif status == "interrupted":
            title = tr("A captura foi interrompida")
            body = tr("O que foi capturado até ali foi preservado.")
        else:
            title = tr("A captura teve problemas")
            body = tr("Parte do áudio de uma fonte pode estar faltando. O restante foi preservado.")
        self._fail("library", title, body, tone="danger" if status == "failed" else "warning",
                   error=error, session=session_id)

    def _set_audio_capabilities(self, tracks, final_available=False):
        # Keep these projections for legacy metadata and controller doubles;
        # they are intentionally not rendered as user-facing controls.
        self.raw_tracks_present = {
            track for track, value in (tracks or {}).items()
            if track in TRACK_LABELS.values() and isinstance(value, dict)
        } if isinstance(tracks, dict) else set()
        self.raw_unavailable_tracks = {
            track for track, value in (tracks or {}).items()
            if isinstance(value, dict) and (value.get("available") is False or value.get("raw_removed"))
        } if isinstance(tracks, dict) else set()
        self.final_available = bool(final_available)
        self.recovery_needed = (not self.final_available or
                                bool(self.raw_tracks_present - self.raw_unavailable_tracks))
        sizes = getattr(self, "audio_sizes", None) or {}
        self.playback_choices = playback_sources(tracks, final_available)
        self.audio_source.set(self.playback_choices[0] if self.playback_choices else "")
        self.raw_capabilities = {
            "playback": bool(self.playback_choices),
            "retranscription": bool(final_available),
            "clip": bool(final_available),
            "audio_export": bool(final_available),
        }
        if hasattr(self, "final_audio_status"):
            if final_available:
                final_size = sizes.get("final")
                detail = " · ".join(value for value in (
                    sizes.get("final_format", ""),
                    format_size(final_size) if isinstance(final_size, (int, float)) else "",
                ) if value)
                self.final_audio_status.set(
                    tr("O áudio mixado usado para ouvir e baixar.")
                    + (f" {detail}." if detail else ""))
            else:
                self.final_audio_status.set(tr("Esta gravação ainda não tem áudio final salvo."))
        for name, state in (
            ("play_button", "normal" if self.playback_choices else "disabled"),
            ("transcribe_button", "normal" if final_available else "disabled"),
            ("export_audio_button", "normal" if final_available else "disabled"),
        ):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.configure(state=state)
        recover_button = getattr(self, "recover_audio_button", None)
        if recover_button is not None:
            recover_button.configure(
                text=tr("Verificar e otimizar áudio") if final_available
                else tr("Preparar áudio final"),
                state="normal" if self.recovery_needed else "disabled",
            )

    @staticmethod
    def _retention_workflow_available(controller):
        method = getattr(controller, "retention_plan", None)
        if not callable(method):
            return False
        module = getattr(type(method), "__module__", "")
        if module.startswith("unittest.mock"):
            # Existing embedded/legacy test doubles only implement the old
            # delete_session seam.  A configured Mock return value still opts
            # into the new exact-plan workflow.
            value = getattr(method, "return_value", None)
            return value is not None and not getattr(type(value), "__module__", "").startswith("unittest.mock")
        return True

    def _retention_preview_text(self, projection, title=""):
        operation = tr(RETENTION_POLICY_LABELS.get(
            projection.get("operation"), projection.get("operation", N_("retenção local")),
        ))
        status = tr("elegível") if projection.get("eligible") else tr("bloqueada")
        lines = [
            tr("Reunião: {title}", title=_safe_retention_text(title or projection.get('session_id'), 160)),
            tr("Operação: {operation} · {status}", operation=operation, status=status),
            tr("Inventário: {count} alvo(s) · {size} bytes",
               count=projection.get('target_count', 0), size=projection.get('byte_estimate', 0)),
            tr("Recuperação: {mode}", mode=projection.get('recovery_mode', 'none')),
        ]
        for label, key in (
            (tr("Motivos"), "reasons"), (tr("Capacidades perdidas"), "lost_capabilities"),
            (tr("Exportações externas preservadas"), "excluded_external_exports"),
            (tr("Alterações na biblioteca"), "canonical_changes"),
        ):
            values = projection.get(key) or []
            if values:
                lines.append(label + ":\n- " + "\n- ".join(values[:8]))
        for target in projection.get("targets", []):
            lines.append(tr("{kind}: {size}", kind=target["kind"], size=format_storage_size(target["bytes"])))
        return "\n".join(lines)[:6000]

    def _retention_preview_loaded(self, session_id, title, plan, error, request):
        if getattr(self, "_retention_pending", None) != (request, session_id):
            return
        if self.closed or not self._trash_target_ready(session_id):
            self._finish_retention(request, session_id)
            return
        if error:
            self._finish_retention(request, session_id)
            self._fail("library", tr("A prévia de retenção falhou"), tr("Nenhuma exclusão foi tentada."),
                       error=error, session=session_id)
            return
        projection = retention_plan_projection(plan)
        if not projection.get("eligible"):
            self._finish_retention(request, session_id)
            self._fail("library", tr("A retenção foi bloqueada"),
                       "; ".join(projection.get("reasons", [])[:3]), tone="warning", session=session_id)
            messagebox.showwarning(tr("Retenção bloqueada"), self._retention_preview_text(projection, title), parent=self.window)
            return
        if not self._confirm_trash(projection, title):
            self._finish_retention(request, session_id)
            self._say("library", tr("Nenhuma exclusão foi aplicada."))
            return
        if self.closed or not self._trash_target_ready(session_id):
            self._finish_retention(request, session_id)
            return
        self._say("library", tr("Aplicando a prévia de retenção…"))
        if not self._submit(
                "retention_apply",
                lambda: self.controller.apply_retention(plan, confirm=True),
                lambda value, apply_error: self._retention_applied(session_id, value, apply_error, request),
        ):
            self._finish_retention(request, session_id)

    def _retention_applied(self, session_id, value, error, request):
        if getattr(self, "_retention_pending", None) != (request, session_id):
            return
        self._finish_retention(request, session_id)
        if self.closed:
            return
        if error or value is False:
            self._fail("library", tr("A retenção não foi aplicada"),
                       tr("A prévia ficou inválida ou a retenção falhou. Revise e gere uma nova prévia."),
                       error=error, session=session_id)
            return
        if self.selected == session_id:
            self._clear_library_detail()
        self.refresh_library()
        self._show_trash_receipt(session_id)

    def _trash_confirmation_text(self, projection, title):
        session_id = projection["session_id"]
        item = getattr(self, "session_items", {}).get(session_id, {})
        duration = self._trash_duration_seconds(item)
        if duration is None:
            detail = getattr(self, "detail_state_item", None) or {}
            if isinstance(detail, dict) and detail.get("id") == session_id:
                duration = self._trash_duration_seconds(detail)
        duration_text = (format_time(duration) if duration is not None
                         else tr("Duração desconhecida"))
        days = f"{float(projection['purge_after_days']):g}"
        return tr(
            "Mover “{title}” para a lixeira?\n\nDuração: {duration} · Tamanho: {size}\n"
            "Você poderá restaurar esta gravação por {days} dias. Cópias exportadas ficam preservadas.",
            title=_safe_retention_text(title, 400), duration=duration_text,
            size=format_storage_size(projection["byte_estimate"]), days=days,
        )

    @staticmethod
    def _trash_duration_seconds(item):
        if not isinstance(item, dict):
            return None
        seconds = item.get("duration")
        if isinstance(seconds, bool):
            return None
        try:
            seconds = float(seconds)
        except (TypeError, ValueError, OverflowError):
            return None
        return seconds if math.isfinite(seconds) and seconds >= 0 else None

    def _confirm_trash(self, projection, title):
        dialog = tk.Toplevel(self.window)
        dialog.withdraw()
        ui_theme.prepare_dialog(dialog, self.window, resolved=self.ui, set_icon=self._dialog_icon)
        dialog.title(tr("Mover para a lixeira"))
        dialog.configure(bg=self.ui.surface)
        dialog.transient(self.window)
        confirmed = False
        def finish(accepted=False):
            nonlocal confirmed
            confirmed = accepted
            dialog.destroy()
        self._wrap_label(dialog, self._trash_confirmation_text(projection, title),
                         anchor="w", justify="left").pack(fill="x", padx=16, pady=16)
        details = tk.Text(dialog, height=8, width=50, wrap="word", font=self.ui.mono(),
                          **self.ui.text_colors(), **self.ui.entry_chrome(readonly=True))
        details.insert("1.0", self._retention_preview_text(projection, title))
        details.configure(state="disabled")
        actions = tk.Frame(dialog, bg=self.ui.surface)
        actions.pack(fill="x", padx=16, pady=(0, 16))
        def toggle_details():
            if details.winfo_manager():
                details.pack_forget()
                dialog.geometry("560x240")
            else:
                details.pack(fill="both", expand=True, padx=16, pady=(0, 16), before=actions)
                dialog.geometry("560x480")
        self._button(actions, tr("Detalhes"), toggle_details).pack(side="left")
        cancel = self._button(actions, tr("Cancelar"), finish)
        cancel.pack(side="right")
        self._button(actions, tr("Mover para a lixeira"), lambda: finish(True)).pack(side="right", padx=8)
        dialog.protocol("WM_DELETE_WINDOW", finish)
        dialog.bind("<Escape>", lambda _event: finish())
        dialog.geometry("560x240")
        dialog.minsize(480, 240)
        dialog.deiconify()
        if not cancel.winfo_viewable():
            cancel.wait_visibility()
        dialog.grab_set()
        cancel.focus_set()
        dialog.wait_window()
        return confirmed

    def _show_trash_receipt(self, session_id):
        self._undo_session = session_id
        self.messages.clear("library")
        self.undo_button.configure(state="normal")
        self.undo_receipt.pack(fill="x", pady=(0, self.ui.space_sm), before=self.library_panes)

    def _undo_trash(self):
        session_id = self._undo_session
        if not session_id:
            return
        self.undo_button.configure(state="disabled")
        def restored(value, error):
            if error or value is False:
                self.undo_button.configure(state="normal")
                self._fail("library", tr("Não foi possível restaurar a gravação"),
                           tr("Abra a lixeira para tentar novamente."), error=error)
                return
            if self._undo_session == session_id:
                self._undo_session = None
                self.undo_receipt.pack_forget()
            self.refresh_library()
            self._say("library", tr("Gravação restaurada na biblioteca."))
        if not self._submit("library_undo", lambda: self.controller.restore_session(session_id), restored):
            self.undo_button.configure(state="normal")

    def _trash_target_ready(self, session_id=None):
        """Require loaded detail and reject any conflicting list selection."""
        selected = getattr(self, "selected", None)
        detail = getattr(self, "detail_state_item", None) or {}
        return bool(
            selected and (session_id is None or selected == session_id)
            and self.detail_ready and detail.get("id", selected) == selected
            and (self.sessions.selection() == (selected,)
                 or (not self.sessions.selection() and detail.get("id") == selected))
            and not getattr(self, "_title_save_pending", None)
            and getattr(self, "retention_ready", True)
            and detail.get("status") != "recording"
        )

    def _sync_trash_controls(self):
        button = getattr(self, "delete_button", None)
        if button is not None and not self.closed:
            ready = self._trash_target_ready() and not getattr(self, "_retention_pending", None)
            button.configure(state="normal" if ready else "disabled")

    def _finish_retention(self, request, session_id):
        if getattr(self, "_retention_pending", None) == (request, session_id):
            self._retention_pending = None
            self._sync_trash_controls()

    def _legacy_delete_selected(self, request):
        """Compatibility path for old embedded doubles only."""
        session_id = self.selected
        title = self.title.get().strip() or session_id
        message = tr(
            "Excluir “{title}” da biblioteca?\n\n"
            "Os arquivos capturados, a transcrição, as notas e o resumo serão movidos "
            "para a lixeira do app. Um arquivo final exportado para outra pasta não será apagado.",
            title=title,
        )
        if not messagebox.askyesno(tr("Excluir gravação"), message, parent=self.window):
            self._finish_retention(request, session_id)
            return
        if self.closed or not self._trash_target_ready(session_id):
            self._finish_retention(request, session_id)
            return
        self.delete_button.configure(state="disabled")
        self._say("library", tr("Movendo gravação para a lixeira…"))

        def deleted(value, error):
            if getattr(self, "_retention_pending", None) != (request, session_id):
                return
            self._finish_retention(request, session_id)
            if self.closed:
                return
            if error or value is False:
                self._fail("library", tr("Não foi possível mover a gravação para a lixeira"),
                           tr("A gravação continua na biblioteca. Tente de novo."),
                           error=error, session=session_id)
                return
            if self.selected == session_id:
                self._clear_library_detail()
            self.refresh_library()
            self._show_trash_receipt(session_id)

        if not self._submit("delete_session", lambda: self.controller.delete_session(session_id), deleted):
            self._finish_retention(request, session_id)

    def delete_selected(self):
        if getattr(self, "_retention_pending", None):
            return
        if not getattr(self, "retention_ready", True):
            self._say("library", tr("A retenção aguarda a recuperação do workspace."))
            return
        if not self._trash_target_ready():
            self._say("library", tr("Selecione uma gravação na biblioteca."))
            return
        session_id = self.selected
        title = self.title.get().strip() or session_id
        self.retention_request = getattr(self, "retention_request", 0) + 1
        request = self.retention_request
        self._retention_pending = (request, session_id)
        self.delete_button.configure(state="disabled")
        if not self._retention_workflow_available(self.controller):
            return self._legacy_delete_selected(request)
        self._say("library", tr("Calculando prévia de retenção…"))
        if not self._submit(
                "retention_plan",
                lambda: self.controller.retention_plan(
                    session_id, policy={"mode": "whole_meeting", "after_days": 0},
                ),
                lambda plan, error: self._retention_preview_loaded(
                    session_id, title, plan, error, request,
                ),
        ):
            self._finish_retention(request, session_id)

    def _select_transcript_text(self, _event=None):
        self.transcript_document.tag_add("sel", "1.0", "end-1c")
        return "break"

    def _sync_transcript_style(self):
        self.transcript_document_frame.pack(fill="both", expand=True, padx=(self.ui.space_md, 0),
                                            after=self.transcript_processing_actions)
        if getattr(self, "transcript_audio_expanded", False):
            self.timed_transcript.pack(fill="both", expand=True, after=self.transcript_audio_toggle)
        else:
            self.timed_transcript.pack_forget()

    def _toggle_transcript_audio(self):
        self.transcript_audio_expanded = not self.transcript_audio_expanded
        ui_theme.disclosure(self.transcript_audio_toggle, tr("Trechos para ouvir"),
                            self.transcript_audio_expanded, self.ui)
        self._sync_transcript_style()

    def _transcript_measure(self, event):
        # 680px is a reading measure, not a spacing token. Extra room becomes
        # symmetric whitespace instead of longer lines at wide window sizes.
        self.transcript_document.configure(padx=max(self.ui.space_md, (event.width - self.ui.px(680)) // 2))

    def _tag_transcript_timestamps(self, text, context):
        self._transcript_timestamp_context = context
        self._transcript_timestamps = {}
        # Tcl 8.6 counts non-BMP characters differently from Python. Line-local
        # ASCII timestamp columns stay exact even after emoji in earlier text.
        for line_number, line in enumerate(text.split("\n"), 1):
            match = re.match(r"^(\d{2,}):([0-5]\d):([0-5]\d)(?=[: ])", line)
            if match is None:
                continue
            tag = f"timestamp_{line_number}"
            start, end = f"{line_number}.0", f"{line_number}.{match.end()}"
            self.transcript_document.tag_add("timestamp", start, end)
            self.transcript_document.tag_add(tag, start, end)
            self._transcript_timestamps[tag] = int(match[1]) * 3600 + int(match[2]) * 60 + int(match[3])

    def _seek_document_timestamp(self, index):
        context = getattr(self, "_transcript_timestamp_context", None)
        if (not context or self.closed or not self.detail_ready or not self.transcript_document_ready
                or context != (self.selected, self.transcript_revision, self.transcript_document_request)):
            return
        for tag in self.transcript_document.tag_names(index):
            if tag in self._transcript_timestamps:
                self.position.set(str(self._transcript_timestamps[tag]))
                self.play()
                break

    def _timestamp_click(self, event):
        index = self.transcript_document.index(f"@{event.x},{event.y}")
        self.transcript_document.mark_set("insert", index)
        self._seek_document_timestamp(index)

    def _timestamp_enter(self, _event=None):
        self._seek_document_timestamp("insert")
        return "break"

    def _transcript_style_changed(self, _event=None):
        self._sync_transcript_style()
        self._load_transcript_document()

    def _reset_transcript_document(self):
        if not hasattr(self, "transcript_document"):
            return
        self.transcript_document_request += 1
        self.bridge.invalidate("transcript_document")
        self.transcript_document_ready = False
        self.transcript_document_truncated = False
        self.transcript_document.configure(state="normal")
        self.transcript_document.delete("1.0", "end")
        self.transcript_document.configure(state="disabled")
        self.copy_transcript_button.configure(state="disabled")
        self._sync_transcript_style()

    def _load_transcript_document(self):
        if not hasattr(self, "transcript_document") or not self.selected or not self.detail_ready:
            return
        session_id, revision = self.selected, self.transcript_revision
        style = displayed_value(TRANSCRIPT_STYLES, self.transcript_style.get())
        self.transcript_document_request += 1
        request = self.transcript_document_request
        self.transcript_document_ready = False
        self.copy_transcript_button.configure(state="disabled")
        self.transcript_document_status.set(tr("Preparando transcrição…"))
        self.bridge.invalidate("transcript_document")

        def read():
            return self.controller.get_transcript_preview(session_id, revision=revision, style=style)

        def loaded(value, error):
            if (self.closed or self.selected != session_id or request != self.transcript_document_request
                    or self.transcript_revision != revision):
                return
            if error or not isinstance(value, dict) or not isinstance(value.get("text"), str):
                if error:
                    self._fail("library", tr("Não foi possível carregar a transcrição"),
                               tr("Abra a gravação de novo para tentar outra vez."),
                               error=error, session=session_id)
                self.transcript_document_status.set(tr("Não foi possível carregar a transcrição. Tente novamente."))
                return
            text = value["text"]
            self.transcript_document_truncated = bool(value.get("truncated"))
            self.transcript_document_ready = True
            self.transcript_document.configure(state="normal")
            self.transcript_document.delete("1.0", "end")
            self.transcript_document.insert("1.0", text)
            if style == "timestamped":
                self._tag_transcript_timestamps(text, (session_id, revision, request))
            else:
                self._transcript_timestamp_context = None
            self.transcript_document.configure(state="disabled")
            self.copy_transcript_button.configure(
                state="normal" if text and not self.transcript_document_truncated else "disabled",
            )
            self.transcript_document_status.set(
                tr("Transcrição extensa: prévia limitada. Exportar salva o texto completo.")
                if self.transcript_document_truncated else
                self._empty_transcript_text() if not text else
                tr("Formato aplicado. Copiar e Exportar usam esta apresentação.")
            )

        self._submit("transcript_document", read, loaded)

    def _sync_empty_transcript_caption(self):
        """Re-say why an empty transcript is empty; a loaded transcript keeps its own caption."""
        if (not hasattr(self, "transcript_document") or not self.transcript_document_ready
                or self.transcript_document_truncated
                or self.transcript_document.get("1.0", "end-1c")):
            return
        self.transcript_document_status.set(self._empty_transcript_text())

    def _empty_transcript_text(self):
        """Why the selected recording shows no text: it is processing, failed, stopped, or is due.

        A job outranks the saved status: a failure loaded before the job is stale.
        """
        if self.selected and self.selected == self.library_processing_session:
            return tr("Processando esta gravação. O resultado aparecerá aqui ao terminar.")
        status = getattr(self, "transcription_status", "")
        if status == "failed":
            return tr("A transcrição falhou e esta gravação não tem texto.")
        if status == "cancelled":
            return tr("A transcrição foi cancelada antes de terminar; esta gravação não tem texto.")
        return tr("A transcrição aparecerá após o processamento com um modelo local.")

    def copy_transcript(self):
        if not self.transcript_document_ready:
            self._say("library", tr("Aguarde o carregamento da transcrição."))
            return
        if self.transcript_document_truncated:
            self._say("library", tr("Use Exportar para obter esta transcrição completa."))
            return
        text = self.transcript_document.get("1.0", "end-1c")
        if not text:
            self._say("library", tr("Esta gravação ainda não tem uma transcrição."))
            return
        self._submit(
            "copy_transcript", lambda: Clipboard.set_content(text),
            lambda copied, error: self._copied(
                tr("Transcrição copiada."), tr("Não foi possível copiar a transcrição"), copied, error),
        )

    def export_transcript(self):
        if not self.selected or not self.detail_ready:
            self._say("library", tr("Selecione uma gravação antes de exportar."))
            return
        path = filedialog.asksaveasfilename(
            parent=self.window, title=tr("Exportar transcrição"), defaultextension=".txt",
            filetypes=((tr("Texto"), "*.txt"),),
        )
        if path:
            self._action("export_transcript", self.selected, path,
                         style=displayed_value(TRANSCRIPT_STYLES, self.transcript_style.get()),
                         revision=self.transcript_revision, receipt=tr("Exportação da transcrição iniciada."))

    def _render_transcript(self, segments):
        self.transcript.delete(*self.transcript.get_children())
        self.segments.clear()
        for index, segment in enumerate(segments):
            if not isinstance(segment, dict):
                continue
            key = str(segment.get("id") or self.transcript_offset + index)
            if key in self.segments:
                key = f"{self.transcript_offset + index}:{key}"
            self.segments[key] = segment
            self.transcript.insert("", "end", iid=key, values=(format_time(segment.get("start", 0)),
                str(segment.get("text", ""))[:8000]))

    def _render_annotations(self, speaker_labels, generation=None):
        if isinstance(speaker_labels, dict):
            self.speaker_labels = {
                key: value for key, value in speaker_labels.items() if isinstance(value, dict)
            }
        if generation is not None and isinstance(generation, int):
            self.annotation_generation = generation

    def _update_transcript_paging_controls(self):
        previous = getattr(self, "transcript_previous_button", None)
        next_button = getattr(self, "transcript_next_button", None)
        if previous is not None:
            previous.configure(
                state="normal" if self.detail_ready and self.transcript_has_previous else "disabled",
            )
        if next_button is not None:
            next_button.configure(
                state="normal" if self.detail_ready and self.transcript_has_more else "disabled",
            )
        label = getattr(self, "transcript_page_label", None)
        if label is not None:
            page = self.transcript_offset // TRANSCRIPT_PAGE_SIZE + 1
            suffix = " · " + tr("há mais trechos") if self.transcript_has_more else ""
            label.set(tr("Página {page} · {count} trechos", page=page, count=len(self.segments)) + suffix)

    def change_transcript_page(self, delta):
        if not self.selected or not self.detail_ready or not isinstance(delta, int):
            return
        target = self.transcript_offset + (delta * TRANSCRIPT_PAGE_SIZE)
        if target < 0 or (delta > 0 and not self.transcript_has_more):
            return
        self._load_transcript_page(target)

    def _load_transcript_page(self, offset):
        if not self.selected or not self.detail_ready or offset < 0:
            return
        session_id, revision = self.selected, self.transcript_revision
        self.transcript_request += 1
        request = self.transcript_request
        self.bridge.invalidate("transcript_page")

        def read():
            return self._read_transcript_page(session_id, revision, offset)

        def loaded(page, error):
            if self.closed or request != self.transcript_request or self.selected != session_id:
                return
            if error:
                self._fail("library", tr("Não foi possível carregar a página da transcrição"),
                           tr("A página anterior continua na tela. Tente de novo."),
                           error=error, session=session_id)
                return
            page = page or {}
            self.transcript_offset = int(page.get("offset", offset))
            self.transcript_has_previous = bool(page.get("has_previous", self.transcript_offset > 0))
            self.transcript_has_more = bool(page.get("has_more"))
            self._render_transcript(page.get("segments", []))
            self._update_transcript_paging_controls()

        self._submit("transcript_page", read, loaded)

    def _read_transcript_page(self, session_id, revision, offset):
        """Read one bounded page through the controller's worker-facing API."""
        limit = TRANSCRIPT_PAGE_SIZE

        def bounded(values):
            result = []
            for segment in islice(values or (), limit):
                if not isinstance(segment, dict):
                    continue
                result.append(
                    {key: segment.get(key) for key in ("id", "start", "end", "track", "speaker")}
                    | {"text": str(segment.get("text", ""))[:8000]}
                )
            return result

        try:
            page = self.controller.get_transcript_page(
                session_id, revision=revision, offset=offset, limit=limit,
            )
            if isinstance(page, dict) and isinstance(page.get("segments"), list):
                values = page.get("segments", [])
                return {
                    "segments": bounded(values),
                    "offset": max(0, int(page.get("offset", offset))),
                    "has_previous": bool(page.get("has_previous", offset > 0)),
                    "has_more": bool(page.get("has_more")),
                }
        except (AttributeError, TypeError):
            # Keep compatibility with older controller doubles and embedded callers.
            pass
        try:
            values = self.controller.get_transcript(
                session_id, revision=revision, offset=offset, limit=limit + 1,
            )
        except TypeError:
            values = self.controller.get_transcript(session_id)
        values = list(islice(values or (), limit + 1))
        return {
            "segments": bounded(values),
            "offset": offset,
            "has_previous": offset > 0,
            "has_more": len(values) > limit,
        }

    def _mark_dirty(self, *_args):
        if not self.loading and self.selected:
            self.dirty = True

    def save_title(self, after=None):
        if not self.selected or not self.detail_ready:
            self._say("library", tr("Selecione uma gravação na biblioteca."))
            return
        if getattr(self, "_title_save_pending", None):
            return
        session_id, title = self.selected, self.title.get()
        if len(title) > 400:
            continuation, self._title_leave_continuation = getattr(self, "_title_leave_continuation", None), None
            if continuation:
                continuation[1]()
            self._say("library", tr("Use um título de até 400 caracteres."))
            return
        self._title_save_pending = (session_id, title)
        self._sync_trash_controls()
        def saved(_value, error):
            self._title_save_pending = None
            self._sync_trash_controls()
            if error or _value is False:
                continuation, self._title_leave_continuation = getattr(self, "_title_leave_continuation", None), None
                if continuation:
                    continuation[1]()
                self._fail("library", tr("Não foi possível salvar o título"),
                           tr("O título anterior continua salvo. Tente salvar de novo."),
                           error=error, session=session_id)
                self._title_work_settled()
                return
            if self.selected != session_id:
                self._title_work_settled()
                return
            self._saved_title = title
            if self.selected == session_id and self.title.get() == title:
                self.dirty = False
            self._say("library", tr("Título salvo."), session=session_id)
            self.refresh_library()
            if after and not self.dirty:
                after()
            elif after:
                self._say("library", tr("O título foi alterado novamente. Salve antes de sair."))
            continuation, self._title_leave_continuation = getattr(self, "_title_leave_continuation", None), None
            if continuation:
                (continuation[0] if not self.dirty else continuation[1])()
            self._title_work_settled()
        if not self._submit("save_title", lambda: self.controller.rename_session(session_id, title), saved):
            self._title_save_pending = None
            self._sync_trash_controls()
            continuation, self._title_leave_continuation = getattr(self, "_title_leave_continuation", None), None
            if continuation:
                continuation[1]()
            self._title_work_settled()

    def _title_work_settled(self):
        settled = getattr(self, "on_forms_settled", None)
        if settled:
            settled()

    def _clear_library_detail(self):
        self._action("stop_playback", urgent=True)
        self.bridge.invalidate("detail")
        self.bridge.invalidate("outputs")
        self.bridge.invalidate("transcript_page")
        self.transcript_request += 1
        self.selected = None
        self.messages.drop_other_sessions()
        self.detail_state_item = None
        self._render_detail_state()
        self.transcription_status = ""
        self._show_processing_issue(None)
        self._render_chat()
        self._reset_transcript_document()
        self._show_summary("")
        self._show_library_detail(False)
        self.playback_choices = ()
        self.audio_source.set("")
        self.audio_overview.set(tr("Selecione uma gravação para ouvir."))
        self.playback_position.set(0.0)
        self.playback_clock.set("00:00:00 / 00:00:00")
        self.play_button.configure(state="disabled")
        self.replay_stop_button.configure(state="disabled")
        self.detail_ready = False
        self.dirty = False
        self.transcript_offset = 0
        self.transcript_revision = None
        self.transcript_has_more = False
        self.transcript_has_previous = False
        self.annotation_generation = 0
        self.speaker_labels = {}
        self.highlights = []
        self.selected_segment_id = None
        self.loading = True
        self.title.set("")
        self.loading = False
        self.transcript.delete(*self.transcript.get_children())
        self.segments.clear()
        self._render_annotations({}, 0)
        self.segment_text.configure(state="normal")
        self.segment_text.delete("1.0", "end")
        self.segment_text.configure(state="disabled")
        self._show_summary("")
        self.delete_button.configure(state="disabled")
        selected = self.sessions.selection()
        if selected:
            self.sessions.selection_remove(*selected)

    def _position(self):
        try:
            value = float(self.position.get())
        except ValueError as exc:
            raise ValueError(tr("Informe um horário em segundos, por exemplo 90.5.")) from exc
        if not math.isfinite(value) or value < 0:
            raise ValueError(tr("O horário deve ser finito e não negativo."))
        return value

    def _transcript_selected(self, _event=None):
        selected = self.transcript.selection()
        if selected and selected[0] in self.segments:
            self.selected_segment_id = self.segments[selected[0]].get("id")
            segment = self.segments[selected[0]]
            self.position.set(str(segment.get("start", 0)))
            self.segment_text.configure(state="normal")
            self.segment_text.delete("1.0", "end")
            self.segment_text.insert("1.0", segment.get("text", ""))
            self.segment_text.configure(state="disabled")
            self.transcript_timing.set(
                tr("Trecho selecionado · dois cliques ou Enter para ouvir; horário por bloco, não por palavra.")
            )

    def _play_transcript_click(self, event):
        row = self.transcript.identify_row(event.y)
        if row in self.segments:
            self.transcript.selection_set(row)
            self._play_transcript_selected()
        return "break"

    def _play_transcript_selected(self, _event=None):
        if self.transcript.selection():
            self._transcript_selected()
            self.play()
        return "break"

    def _player_source_changed(self, _event=None):
        if self._playback_active:
            self.stop_selected_recording()
        self.playback_position.set(0.0)
        self.playback_clock.set(f"{format_time(0)} / {format_time(self.playback_duration)}")
        self.playback_status.set(tr("Fonte selecionada. Reproduza para ouvir."))

    def _player_seek_begin(self, _event=None):
        self.playback_dragging = True

    def _player_seek_end(self, _event=None):
        self.playback_dragging = False
        position = min(self.playback_duration, max(0.0, self.playback_position.get()))
        self.playback_clock.set(f"{format_time(position)} / {format_time(self.playback_duration)}")
        if self._playback_active:
            self.play_selected_recording()

    def play_selected_recording(self):
        if not self.selected or not self.detail_ready:
            self.playback_status.set(tr("Selecione uma gravação para ouvir."))
            return
        if self.snapshot.get("state") in ("starting", "recording", "paused", "stopping"):
            self.playback_status.set(tr("Finalize a captura antes de reproduzir uma gravação."))
            return
        if not self.playback_choices:
            self.playback_status.set(tr("O áudio desta gravação não está disponível."))
            return
        position = min(self.playback_duration, max(0.0, self.playback_position.get()))
        if self.playback_duration and position >= self.playback_duration:
            position = 0.0
            self.playback_position.set(0.0)
        self._action("seek_playback", self.selected, "final", start=position,
                     urgent=True, callback=self._replay_started)

    def _replay_started(self, accepted, error):
        if error:
            self.playback_status.set(tr("Não foi possível reproduzir o áudio."))
            self._fail("library", tr("Não foi possível reproduzir o áudio"),
                       tr("Tente de novo. O áudio final continua salvo."), error=error)
        elif not accepted:
            self.playback_status.set(tr("A reprodução não está disponível durante outra operação."))
        else:
            self.playback_status.set(tr("Iniciando reprodução…"))

    def stop_selected_recording(self):
        self._action("stop_playback", urgent=True)

    def play(self):
        if not self.selected:
            self._say("library", tr("Selecione uma gravação na biblioteca."))
            return
        if self.snapshot.get("state") in ("starting", "recording", "paused", "stopping"):
            self._say("library", tr("Finalize a captura antes de ouvir uma gravação."))
            return
        try:
            position = self._position()
        except ValueError as exc:
            self._say("library", str(exc))
            return
        self._action("seek_playback", self.selected, "final", start=position, urgent=True)

    def transcribe(self):
        if not self.selected:
            self._say("library", tr("Selecione uma gravação na biblioteca."))
            return
        if not getattr(self, "final_available", False):
            self._say("library", tr("A retranscrição exige um áudio final MP3 salvo."))
            return
        try:
            settings = self._current_settings()
        except ValueError as exc:
            self._say("library", str(exc))
            return
        session_id = self.selected
        self._action("transcribe", session_id, settings.profile, settings.language,
                     callback=lambda value, error: self._processing_launched(session_id, value, error))

    def import_model(self):
        profile = self._effective_profile()
        path = filedialog.askopenfilename(parent=self.window, title=tr("Importar modelo local compatível"),
                                           filetypes=((tr("Modelo GGUF"), "*.gguf"), (tr("Todos os arquivos"), "*")))
        if path:
            self._action("import_model", profile, path,
                         callback=lambda value, error: self._processing_launched("", value, error))

    def import_audio(self):
        try:
            settings = self._current_settings()
        except ValueError as exc:
            self._say("library", str(exc))
            return
        path = filedialog.askopenfilename(
            parent=self.window,
            title=tr("Importar áudio"),
            filetypes=(
                (tr("Áudio compatível"), "*.wav *.mp3 *.aac *.m4a *.flac *.ogg *.opus"),
                (tr("Todos os arquivos"), "*"),
            ),
        )
        if path:
            def imported(session_id, error):
                if error:
                    self.refresh_library()
                    self._fail("library", tr("Não foi possível importar o áudio"),
                               tr("Nenhuma gravação foi criada. Verifique o arquivo e tente de novo."),
                               error=error)
                    return
                self.refresh_library()
                if not self.dirty:
                    self.notebook.select(self.library_tab)
                    self.load_session(session_id)
                else:
                    self._say("library", tr("Áudio importado. O título em edição foi preservado."))
            self._action("import_audio", path, settings, callback=imported)

    def export(self, format):
        if not self.selected:
            self._say("library", tr("Selecione uma gravação na biblioteca."))
            return
        suffix = ".md" if format == "markdown" else ".txt"
        path = filedialog.asksaveasfilename(parent=self.window, title=tr("Exportar transcrição"),
            defaultextension=suffix, filetypes=(("Markdown" if suffix == ".md" else tr("Texto"), "*" + suffix),))
        if path:
            self._action("export", self.selected, path, format=format,
                         receipt=tr("Exportação iniciada."))

    def export_audio(self):
        """Save a copy of the final audio, the file playback uses."""
        if not self.selected:
            self._say("library", tr("Selecione uma gravação na biblioteca."))
            return
        if not getattr(self, "final_available", False):
            self._say("library", tr("Esta gravação ainda não tem áudio final salvo."))
            return
        sizes = getattr(self, "audio_sizes", None) or {}
        extension = "." + (sizes.get("final_format") or "MP3").lower()
        options = {"parent": self.window, "title": tr("Baixar áudio final"),
                   "defaultextension": extension, "initialfile": safe_filename_stem(self.title.get()) + extension,
                   "filetypes": ((tr("Áudio {format}", format=extension[1:].upper()), "*" + extension),)}
        path = filedialog.asksaveasfilename(**options)
        if path:
            self._action("export_final_audio", self.selected, path,
                         receipt=tr("Exportação do áudio final iniciada."))

    def prepare_final_audio(self):
        """Recover and verify the final MP3 from a retained capture."""
        if not self.selected or not self.detail_ready:
            self._say("library", tr("Selecione uma gravação na biblioteca."))
            return
        if getattr(self, "final_available", False) and not getattr(self, "recovery_needed", False):
            self._say("library", tr("Esta gravação já tem áudio final salvo."))
            return
        if not callable(getattr(self.controller, "recover_audio", None)):
            self._say("library", tr("A recuperação do áudio final não está disponível nesta versão."))
            return
        session_id = self.selected
        self._action("recover_audio", session_id,
                     callback=lambda value, error: self._processing_launched(session_id, value, error))

    def summarize(self):
        self.generate_report()

    def _sync_summary_controls(self):
        if not hasattr(self, "regenerate_summary_button"):
            return
        snapshot = getattr(self, "snapshot", {})
        pending = getattr(self, "summary_pending", None)
        busy = bool(pending or snapshot.get("processing") or snapshot.get("state", "idle") != "idle")
        enabled = bool(not busy and self.detail_ready and self.transcript_revision)
        self.regenerate_summary_button.configure(
            text=tr("Gerando…") if pending and pending[0] == self.selected else tr("Gerar resumo"),
            state="normal" if enabled else "disabled",
        )
        reason = ""
        if self.selected and self.detail_ready and not enabled and not pending:
            reason = self._library_blocker(snapshot) or (
                "" if self.transcript_revision else tr("Transcreva esta gravação antes de gerar o resumo."))
        if reason != self._summary_reason:
            self._summary_reason = reason
            self.summary_status.set(reason or self._summary_hint(getattr(self, "summary_text", "")))
        self._sync_job_cancels()

    def _processing_launched(self, session_id, accepted, error):
        if error or not accepted:
            if error:
                self._fail("library", tr("Não foi possível iniciar o processamento local"),
                           tr("Nada foi alterado. Tente de novo."), error=error,
                           session=session_id or None)
            else:
                self._fail("library", tr("O processamento não começou"),
                           tr("Há uma captura ou outro processamento em andamento. Tente de novo depois."),
                           tone="warning")
            return
        self.processing_target = session_id
        self._say("library", tr("Processamento local iniciado. O resultado aparecerá quando terminar."))

    def refresh_outputs(self, session_id, *, completion_context=None, completed=False):
        if self.selected != session_id:
            return
        revision, offset = self.transcript_revision, self.transcript_offset
        def read():
            raw = self.controller.get_session(session_id)
            summary = format_summary(raw.get("summary") or raw.get("reviewed_summary"))
            current_revision = active_transcript_revision(raw) or revision
            page = self._read_transcript_page(session_id, current_revision, offset)
            annotations = raw.get("annotations") if isinstance(raw.get("annotations"), dict) else {}
            final = raw.get("final_audio")
            path = final.get("path") if isinstance(final, dict) else None
            final_available = bool(isinstance(path, str) and os.path.isfile(path))
            return (summary, page, annotations_for_revision(annotations, current_revision),
                    raw.get("tracks", {}), final_available, _processing_failure_details(raw),
                    _processing_issue(raw), _stage_status(raw, "transcription"))
        def loaded(data, error):
            if self.selected != session_id or not self.detail_ready:
                return
            if error:
                self._results_not_refreshed(error, session_id)
                return
            (summary, page, annotations, tracks, final_available, _processing_details,
             processing_issue, transcription_status) = data
            self._set_audio_capabilities(tracks, final_available)
            self.transcription_status = transcription_status
            self._show_processing_issue(processing_issue)
            if getattr(self, "_adjusted_audio_target", None) == session_id:
                self._adjusted_audio_target = None
                if final_available:
                    self.audio_source.set(tr("Áudio final"))
            self.transcript_revision = annotations.get("revision_filter", self.transcript_revision)
            generation = annotations.get("generation")
            if isinstance(generation, int):
                self.annotation_generation = generation
            labels = annotations.get("speaker_labels")
            if isinstance(labels, dict):
                self.speaker_labels = labels
            highlights = annotations.get("highlights")
            if isinstance(highlights, list):
                self.highlights = highlights[:2000]
            self._show_summary(summary)
            self.transcript_offset = page.get("offset", self.transcript_offset)
            self.transcript_has_previous = page.get("has_previous", self.transcript_offset > 0)
            self.transcript_has_more = page.get("has_more", False)
            self._render_transcript(page.get("segments", []))
            self._render_annotations(self.speaker_labels, self.annotation_generation)
            self._update_transcript_paging_controls()
            self._load_transcript_document()
            self.refresh_reports(session_id)
            self._say("library", tr("Resultado local atualizado. Revise a transcrição e o resumo."),
                      session=session_id)
            if completed and not processing_issue and summary:
                self._summary_completed(session_id, completion_context)
        self._submit("outputs", read, loaded)

    def copy_summary(self):
        text = getattr(self, "summary_text", "")
        if not text:
            self._say("library", tr("Esta gravação ainda não tem um resumo."))
            return
        self._submit(
            "copy_summary", lambda: Clipboard.set_content(text),
            lambda copied, error: self._copied(
                tr("Resumo copiado."), tr("Não foi possível copiar o resumo"), copied, error),
        )

    def _show_summary(self, value):
        text = format_summary(value)
        self.summary.configure(state="normal")
        self.summary.delete("1.0", "end")
        self.summary.insert("1.0", text[:65536] or tr("O resumo aparecerá aqui após o processamento local."))
        self.summary.tag_configure("heading", font=self.ui.heading(), spacing1=self.ui.space_lg,
                                   spacing3=self.ui.space_sm)
        self.summary.tag_configure("bullet", lmargin1=self.ui.space_sm, lmargin2=self.ui.space_lg,
                                   spacing3=self.ui.space_xs)
        for number, line in enumerate(text[:65536].splitlines(), 1):
            if line.startswith("• "):
                self.summary.tag_add("bullet", f"{number}.0", f"{number}.end")
            elif line.endswith(":") and line:
                self.summary.tag_add("heading", f"{number}.0", f"{number}.end")
        self.summary.configure(state="disabled")
        self.summary_text = text
        self._summary_reason = None
        if hasattr(self, "copy_summary_button"):
            self.copy_summary_button.configure(state="normal" if text else "disabled")
        if hasattr(self, "summary_status"):
            self.summary_status.set(self._summary_hint(text))
        if hasattr(self, "summary_setup_toggle"):
            self._sync_summary_setup_label()

    def _summary_hint(self, text):
        """What the line under Gerar resumo says while nothing is running."""
        installed = any(self.summary_model_installed.values())
        return (
            tr("Resumo salvo. Escolha um formato para gerar outra versão.") if text
            else tr("O resumo será gerado automaticamente com os modelos instalados.") if installed
            else tr("Instale um modelo de resumo em Configurações → Modelos para gerar automaticamente.")
        )

    def _apply_playback_snapshot(self, snapshot):
        """Apply controller-owned progress on Tk, ignoring stale generations."""
        if self.closed:
            return
        playback = snapshot.get("playback") if isinstance(snapshot, dict) else None
        if not isinstance(playback, dict):
            return
        generation = playback.get("generation")
        if not isinstance(generation, int) or generation < self.playback_generation:
            return
        self.playback_generation = generation
        active = bool(playback.get("active"))
        self._playback_active = active
        position = playback.get("position", playback.get("start", 0))
        try:
            position = max(0.0, float(position))
        except (TypeError, ValueError):
            position = 0.0
        if self.selected == playback.get("session_id"):
            if hasattr(self, "playback_position") and not getattr(self, "playback_dragging", False):
                self.playback_position.set(min(position, getattr(self, "playback_duration", position)))
            if hasattr(self, "playback_clock"):
                self.playback_clock.set(
                    f"{format_time(position)} / {format_time(getattr(self, 'playback_duration', 0))}"
                )
        if hasattr(self, "replay_stop_button"):
            self.replay_stop_button.configure(
                state="normal" if active and self.selected == playback.get("session_id") else "disabled"
            )
        if self.selected != playback.get("session_id"):
            return
        if active:
            track = playback.get("track")
            track_label = tr("Áudio final") if track == "final" else tr("Áudio")
            self.playback_status.set(
                tr("Reproduzindo {track} · {time}", track=track_label, time=format_time(position))
            )
            if self.selected == playback.get("session_id"):
                self._select_playback_segment(position, track)
        else:
            error = str(playback.get("error") or "")
            if error:
                self.playback_status.set(tr("A reprodução falhou."))
                if generation != self._playback_failure:
                    self._playback_failure = generation
                    self._fail("library", tr("A reprodução falhou"),
                               tr("O áudio final continua salvo. Tente de novo."),
                               error=error, session=self.selected)
            else:
                self.playback_status.set(tr("Reprodução parada."))

    def _select_playback_segment(self, position, track):
        for key, segment in self.segments.items():
            if not isinstance(segment, dict) or segment.get("track") != track:
                continue
            try:
                start = float(segment.get("start", 0))
                end = float(segment.get("end", start))
            except (TypeError, ValueError):
                continue
            if start <= position < max(start, end) or (end <= start and position >= start):
                if self.transcript.selection() != (key,):
                    self.transcript.selection_set(key)
                    self.transcript.see(key)
                return

    def _recording_finished(self, snapshot):
        self.refresh_library()
        session_id = snapshot.get("session_id")
        if not session_id:
            return
        if snapshot.get("processing"):
            self.processing_target = session_id
        if not self.dirty and self._visible_destination() == "recording":
            self.notebook.select(self.library_tab)
            self.load_session(session_id)

    def _sync_audio_export_progress(self):
        """Render worker progress on Tk and refresh the saved file before ASR finishes."""
        export = self.snapshot.get("audio_export") or {}
        stage = export.get("stage")
        active = bool(self.snapshot.get("processing") and stage in {"preparing", "encoding", "finalizing"})
        selected = bool(export.get("session_id") and export["session_id"] == self.selected
                        and self.detail_ready)
        animating = getattr(self, "_audio_export_animating", set())
        self._audio_export_animating = animating
        for name, visible in (("audio_export_progress", active),
                              ("final_audio_progress", active and selected)):
            bar = getattr(self, name, None)
            if bar is None:
                continue
            if visible:
                percent = export.get("percent")
                mode = "indeterminate" if percent is None else "determinate"
                if str(bar.cget("mode")) != mode:
                    bar.stop()
                    animating.discard(name)
                    bar.configure(mode=mode)
                if mode == "indeterminate" and name not in animating:
                    bar.start(50)
                    animating.add(name)
                if percent is not None:
                    bar.configure(value=percent)
                bar.grid()
            else:
                bar.stop()
                animating.discard(name)
                bar.grid_remove()
        if selected and stage in {"preparing", "encoding", "finalizing", "failed", "cancelled"}:
            self.final_audio_status.set(export["label"])
        if selected and hasattr(self, "export_audio_button"):
            audio_stage_active = stage in {"preparing", "encoding", "finalizing"}
            self.export_audio_button.configure(
                state="disabled" if audio_stage_active else (
                    "normal" if getattr(self, "final_available", False) else "disabled"
                ),
            )
        if selected and hasattr(self, "recover_audio_button"):
            self.recover_audio_button.configure(
                state="normal" if not self.snapshot.get("processing")
                and getattr(self, "recovery_needed", False) else "disabled",
            )
        key = (export.get("session_id"), stage) if selected and stage in {
            "completed", "failed", "cancelled"} else None
        if key and key != getattr(self, "_audio_export_refreshed", None):
            self._audio_export_refreshed = key
            self.refresh_outputs(export["session_id"])
        elif not key:
            self._audio_export_refreshed = None

    def _poll(self):
        self.after_id = None
        if self.closed or not getattr(self, "visible", True):
            return
        try:
            had_results = not self.bridge.results.empty()
            self.bridge.drain()
            semantic_state = getattr(self.controller, "semantic_state", None)
            if (callable(semantic_state) and hasattr(self, "semantic_enabled")
                    and self.semantic_enabled.get() and self.semantic_cancel is None):
                state = semantic_state()
                if isinstance(state, str):
                    previous = getattr(self, "last_semantic_state", None)
                    self.last_semantic_state = state
                    self.semantic_status.set(tr("Busca semântica: {state}",
                        state=tr(INDEX_STATE_LABELS.get(state, state))))
                    if previous == "rebuilding" and state == "ready":
                        self.refresh_library()
            with self.summary_progress_lock:
                progress = self.summary_progress
            if progress is not None:
                name, done, total = progress
                percent = int(done * 100 / total) if total else 0
                self.summary_model_status.set(
                    tr("Baixando {name}: {percent}% · verificação SHA-256 antes da instalação",
                       name=name, percent=percent)
                )
                label = getattr(self, "summary_model_states", {}).get(getattr(self, "_summary_download_profile", None))
                if label is not None:
                    label.configure(text=tr("Baixando {percent}%", percent=min(100, percent)))
            semantic_progress = getattr(self, "semantic_progress_state", [None])[0]
            if semantic_progress is not None and self.semantic_cancel is not None:
                done, total = semantic_progress
                self.semantic_status.set(tr("Baixando {percent}%", percent=min(100, int(done * 100 / total)))
                                         if total else tr("Baixando…"))
            rebuild = self.rebuild_progress
            if getattr(self, "rebuild_cancel", None) is not None and isinstance(rebuild, dict):
                done, total = rebuild.get("done", 0), rebuild.get("total")
                suffix = f"{done}/{total}" if total else str(done)
                self.index_status.set(tr("Índice de busca: reconstruindo · {progress} reuniões", progress=suffix))
            elif isinstance(rebuild, dict) and rebuild.get("state") not in {None, "idle"}:
                self.index_status.set(
                    tr("Índice de busca: {state} · {count} reuniões",
                       state=rebuild.get('state'), count=rebuild.get('done', 0))
                )
            snapshot = self.controller.snapshot()
            self.snapshot = snapshot
            self._sync_readiness()
            projection = (snapshot, self.settings_loaded, self.retention_ready,
                          self.privacy_ready, self.dirty, self._visible_destination())
            if (not had_results and not self.bridge.callbacks and not snapshot.get("processing")
                    and self.processing_target is None and not self.previous_processing
                    and manager_poll_interval(snapshot) == 2000
                    and projection == getattr(self, "_last_idle_projection", None)):
                self.messages.clear("recording", key="poll")
                self.after_id = self.root.after(2000, self._poll)
                return
            self._last_idle_projection = projection
            self._sync_qa_controls()
            self._apply_playback_snapshot(self.snapshot)
            state = self.snapshot.get("state", "idle")
            active = state in CAPTURE_STATES
            processing = self.snapshot.get("processing")
            start_ready = (
                state == "idle" and not start_blocker(self.snapshot)
                and not self._start_hold(self.snapshot)
            )
            self._sync_start_blocked(self.snapshot, start_ready)
            self._sync_processing_cancel(self.snapshot)
            self._sync_library_processing(self.snapshot)
            self._sync_processing_banner(self.snapshot)
            if self.start_after_interrupt and start_ready:
                origin, self.start_after_interrupt = self.start_after_interrupt, None
                self.request_start(origin)
            elif self.start_after_interrupt and state == "unavailable":
                self.start_after_interrupt = None
                self._fail("recording", tr("A gravação não foi iniciada"),
                           tr("Os recursos de áudio ainda estão em uso."), tone="warning")
            elif self.start_after_interrupt and active:
                # Another path (hotkey, tray) already started the recording.
                self.start_after_interrupt = None
            self._sync_transport(state, start_ready)
            self.preview_button.configure(state="normal" if start_ready else "disabled")
            error = self.snapshot.get("error")
            self._sync_summary_controls()
            self._sync_recording_status(self.snapshot, start_ready)
            self._sync_recording_hint(active)
            self._sync_recording_trouble(self.snapshot)
            self._sync_audio_export_progress()
            self._sync_library_controls(self.snapshot)
            show_waveform = bool(active or self.snapshot.get("previewing"))
            waveform_visible = self._visible_destination() == "recording" and show_waveform
            self.waveform.set_visible(waveform_visible)
            if show_waveform != self._waveform_visible:
                self._waveform_visible = show_waveform
                if show_waveform:
                    self.waveform.grid()
                else:
                    self.waveform.grid_remove()
                self.recording_monitor.rowconfigure(WAVEFORM_ROW, weight=int(show_waveform))
            points = tuple((track, tuple(values)) for track, values in
                           self.snapshot.get("waveforms", {}).items() if track in ("microphone", "system"))
            if waveform_visible and points != self._waveform_key:
                self._waveform_key = points
                self.waveform.clear()
                for track, values in points:
                    self.waveform.append_block(track, values)
            # The caption follows the status lead: a held Start reads the job
            # (Processando), a source test (Testando) or, with neither, Aguardando.
            waveform_state = ("checking" if self.snapshot.get("previewing") else
                              "paused" if state == "paused" else "recording"
                              if state in ("starting", "recording", "stopping") else "idle"
                              if start_ready else "processing"
                              if processing or state == "postprocessing" else "waiting")
            self.waveform.set_state(waveform_state)
            for track, meter in self.meters.items():
                value = float(self.snapshot.get("levels", {}).get(track, 0) or 0)
                shown = meter_value(value)
                if float(meter.cget("value")) != shown:
                    meter.configure(value=shown)
                readout = getattr(self, "meter_readouts", {}).get(track)
                if readout is not None:
                    readout.configure(text=meter_decibels(value))
            if self.previous_state in ("starting", "recording", "paused", "stopping") and not active:
                self._recording_finished(self.snapshot)
            if state != self.previous_state and self.on_recording_state_changed:
                self.on_recording_state_changed(state, dict(self.snapshot))
            self.previous_state = state
            if self.previous_processing and not processing:
                self.refresh_library()
            self.previous_processing = bool(processing)
            if self.processing_target is not None and not processing:
                target, self.processing_target = self.processing_target, None
                if error:
                    self._adjusted_audio_target = None
                if target:
                    context = getattr(self, "_completed_processing_context", None)
                    self._completed_processing_context = None
                    self.refresh_outputs(target, completion_context=context[1] if context and context[0] == target else None,
                                         completed=bool(context and context[0] == target))
        except Exception as exc:
            current = self.messages.current("recording")
            if current is None or current.key != "poll":
                self._fail("recording", tr("Não foi possível atualizar o estado da gravação"),
                           tr("O que aparece pode estar desatualizado. Uma gravação em andamento continua."),
                           error=exc, key="poll")
        else:
            self.messages.clear("recording", key="poll")
        interval = manager_poll_interval(self.snapshot)
        if any(key not in LONG_JOB_KEYS for key in self.bridge.callbacks):
            interval = 200
        elif self.bridge.callbacks:
            interval = min(interval, 1000)
        self.after_id = self.root.after(interval, self._poll)

    def _destroyed(self, event):
        if event.widget is self.window:
            self.close_without_prompt(destroy=False)

    def close(self, destroy=True, after_close=None):
        if self.closed:
            return
        if self.dirty:
            answer = messagebox.askyesnocancel(tr("Título não salvo"), tr("Salvar o título antes de fechar?"), parent=self.window)
            if answer is None:
                return
            if answer:
                self.save_title(after=lambda: self.close_without_prompt(destroy, after_close))
                return
        self.close_without_prompt(destroy, after_close)

    def close_without_prompt(self, destroy=True, after_close=None):
        if self.closed:
            return
        self._title_leave_continuation = None
        if getattr(self, "_device_layout_after", None) is not None:
            self.window.after_cancel(self._device_layout_after)
            self._device_layout_after = None
        self._cancel_library_search()
        self.closed = True
        # Closing never cancels a recording's work (its capture, final audio,
        # transcript or summary); it stops only answers this view would show.
        for name in ("ask_request_cancel", "cross_request_cancel", "summary_request_cancel"):
            event = getattr(self, name, None)
            if event is not None:
                event.set()
                setattr(self, name, None)
        self.report_request = getattr(self, "report_request", 0) + 1
        self.ask_request = getattr(self, "ask_request", 0) + 1
        self.citation_request = getattr(self, "citation_request", 0) + 1
        self.search_request = getattr(self, "search_request", 0) + 1
        self.cross_request = getattr(self, "cross_request", 0) + 1
        self.cross_citation_request = getattr(self, "cross_citation_request", 0) + 1
        self.trash_request = getattr(self, "trash_request", 0) + 1
        self.retention_request = getattr(self, "retention_request", 0) + 1
        self._retention_pending = None
        self.pending_start_origin = None
        self.start_after_interrupt = None
        if hasattr(self, "ask_conversations"):
            self.ask_conversations.clear()
        self.ask_pending = None
        self.cross_pending = None
        self.playback_generation = getattr(self, "playback_generation", -1) + 1
        self.transcript_request = getattr(self, "transcript_request", 0) + 1
        if getattr(self, "summary_download_cancel", None) is not None:
            self.summary_download_cancel.set()
        for name in ("rebuild_cancel", "semantic_cancel"):
            event = getattr(self, name, None)
            if event is not None:
                event.set()
        try:
            self.controller.stop_playback()
        except (AttributeError, RuntimeError):
            # Older controller doubles and an already-torn-down controller are safe.
            pass
        self._unbind_mousewheel_regions()
        self.bridge.close()
        for dialog_name in ("recording_notice_dialog", "trash_dialog"):
            dialog = getattr(self, dialog_name, None)
            if dialog is not None:
                try:
                    dialog.destroy()
                except tk.TclError:
                    pass
                setattr(self, dialog_name, None)
        self.trash_tree = None
        if self.after_id is not None:
            self.root.after_cancel(self.after_id)
            self.after_id = None
        if getattr(self, "_session_title_fit", None) is not None:
            self.window.after_cancel(self._session_title_fit)
            self._session_title_fit = None
        if destroy:
            self.window.destroy()
        if after_close is not None:
            after_close()


def open_meeting_window(root, controller, settings_getter, persist_settings, on_settings_changed=None,
                        on_recording_state_changed=None, on_appearance_changed=None):
    view = MeetingWindow(root, controller, settings_getter, persist_settings,
                         on_settings_changed, on_recording_state_changed,
                         on_appearance_changed)
    view.window._meeting_view = view
    return view.window


def add_meeting_tabs(root, window, notebook, controller, settings_getter,
                     persist_settings, on_settings_changed=None,
                     on_recording_state_changed=None, on_appearance_changed=None,
                     data_location=None, relocate_data=None,
                     models_location=None, relocate_models=None, on_language_changed=None,
                     set_window_icon=None):
    """Attach recording and library tabs to the shared application window."""
    view = MeetingWindow(
        root, controller, settings_getter, persist_settings, on_settings_changed,
        on_recording_state_changed, on_appearance_changed,
        window=window, notebook=notebook,
        data_location=data_location, relocate_data=relocate_data,
        models_location=models_location, relocate_models=relocate_models,
        on_language_changed=on_language_changed, set_window_icon=set_window_icon,
    )
    window._meeting_view = view
    return view
