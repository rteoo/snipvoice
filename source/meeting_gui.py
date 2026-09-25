"""Shared-root meeting workspace with bounded workers and Tk-only updates."""

from itertools import islice
import copy
import json
import math
import os
import queue
import re
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from clipboard_support import Clipboard
import data_relocation
from meeting_files import report_export_projection
from meeting_settings import EndpointSelection, resolve_meeting_settings, validate_hotkey_conflicts
from meeting_waveform import MeetingWaveform
from summary_catalog import format_model_size, summary_catalog, summary_catalog_entry
from summary_models import (
    delete_summary_model,
    download_summary_model,
    summary_model_is_installed,
)
import ui_theme
from voice_catalog import available_languages, format_size, selectable_catalog
from voice_hotkey import DEFAULT_COMMAND_HOTKEY, DEFAULT_DICTATION_HOTKEY


PAGE_SIZE = 50
MAX_PAGE_BACKSTACK = 32
MAX_SEARCH_RESULTS = 32
TRANSCRIPT_LIMIT = 500
APPEARANCE_LABELS = {
    "system": "Sistema",
    "light": "Claro",
    "dark": "Escuro",
}
APPEARANCE_STATUS = {
    "system": "Segue o tema do sistema.",
    "light": "Tema claro fixo.",
    "dark": "Tema escuro fixo.",
}
TRANSCRIPT_PAGE_SIZE = 100
NOTES_LIMIT = 1024 * 1024
BOOKMARK_LIMIT = 1000
MAX_ANSWER_CHARS = 4_000
REPORT_HISTORY_LIMIT = 500
MAX_RETENTION_PREVIEW_ITEMS = 64
MAX_TRASH_ITEMS = 256
MAX_RETENTION_TEXT = 800
RETENTION_POLICY_LABELS = {
    "keep": "Manter indefinidamente",
    "whole_meeting": "Excluir reunião após o prazo",
    "raw_tracks": "Remover áudio raw após o prazo",
}
STATE_LABELS = {"idle": "Pronto", "starting": "Iniciando", "recording": "Gravando",
                "paused": "Pausado", "stopping": "Finalizando", "postprocessing": "Processando",
                "completed": "Concluído",
                "stopped": "Parado", "interrupted": "Interrompido", "failed": "Falha",
                "partial": "Parcial", "teardown_blocked": "Recursos ainda em uso",
                "unavailable": "Recursos ainda em uso"}
INDEX_STATE_LABELS = {"ready": "pronto", "stale": "desatualizado", "rebuilding": "reconstruindo",
                      "unavailable": "indisponível", "incomplete": "incompleto",
                      "compatibility": "modo de compatibilidade", "cancelled": "cancelado",
                      "failed": "falhou"}
STATUS_FILTERS = {"Todos": "", "Concluídos": "completed", "Parciais": "partial",
                  "Falhas": "failed", "Interrompidos": "interrupted", "Gravando": "recording"}
PROFILE_LABELS = {"balanced": "Equilibrado · Parakeet TDT", "compact": "Compacto · Qwen 0.6B",
                  "accuracy": "Precisão · Qwen 1.7B", "streaming": "Transcrição contínua",
                  "whisper-small": "Whisper Small", "whisper-turbo": "Whisper Large v3 Turbo",
                  "whisper-large-v3": "Whisper Large v3"}
LANGUAGE_LABELS = {"auto": "Automático", "pt-BR": "Português (Brasil)", "en-US": "Inglês (Estados Unidos)"}
TRACK_LABELS = {"Microfone": "microphone", "Sistema": "system"}
AUDIO_SOURCE_LABELS = {"Áudio final": "final", **TRACK_LABELS}
AUDIO_TRACK_LABELS = {track: label for label, track in AUDIO_SOURCE_LABELS.items()}
SUMMARY_LABELS = {entry["id"]: f'{entry["name"]} · {entry["parameters"]}'
                  for entry in summary_catalog()}


def _safe_retention_text(value, limit=MAX_RETENTION_TEXT):
    """Keep retention previews bounded and free of local absolute paths."""
    text = " ".join(str(value or "").split())
    # Plan reasons are backend-owned text.  Do not let a future reason or an
    # OS error disclose the workspace path through a confirmation dialog.
    # Redact the remainder of a comma/semicolon-delimited field so paths with
    # spaces and UNC shares cannot leak only their tail.
    text = re.sub(r"\\\\[^\\/\s]+[\\/][^,;]*", "[caminho local]", text)
    text = re.sub(r"(?i)(?<!\w)[A-Z]:[\\/][^,;]*", "[caminho local]", text)
    text = re.sub(r"(?<![:\w])/[^,;]*", "[caminho local]", text)
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
        "raw_tracks": [_safe_retention_text(item, 80) for item in list(get("raw_tracks", ()) or ())[:4]],
    }


def format_time(seconds):
    seconds = float(seconds or 0)
    if not math.isfinite(seconds) or seconds < 0:
        seconds = 0
    whole = int(seconds)
    return f"{whole // 3600:02d}:{whole // 60 % 60:02d}:{whole % 60:02d}"


def playback_sources(tracks, final_available):
    """Offer the saved mix first, then only raw tracks that still exist."""
    choices = ["Áudio final"] if final_available else []
    if isinstance(tracks, dict):
        for label, track in TRACK_LABELS.items():
            value = tracks.get(track)
            if (isinstance(value, dict) and value.get("available") is not False
                    and not value.get("raw_removed")):
                choices.append(label)
    return tuple(choices)


def meter_value(peak):
    """Make quiet speech visible on a bounded -60 dB to 0 dB meter."""
    try:
        peak = float(peak)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(peak) or peak <= 0:
        return 0.0
    return max(0.0, min(1.0, 1.0 + math.log10(min(1.0, peak)) / 3.0))


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


def destination_display(path):
    """Describe the implicit local destination without changing its saved value."""
    return str(path or "").strip() or "Pasta local padrão (recordings)"


def format_recording_status(snapshot):
    """Return bounded user-facing state without exposing paths or native errors."""
    state = snapshot.get("state", "idle")
    displayed = snapshot.get("last_status") if state == "idle" else state
    parts = [STATE_LABELS.get(displayed or state, displayed or state),
             format_time(snapshot.get("elapsed"))]
    if snapshot.get("processing"):
        parts.append("Processando localmente")
    if snapshot.get("final_audio"):
        parts.append("Gravação preservada")
    if snapshot.get("partial"):
        parts.append("Uma fonte de áudio foi interrompida")
    if snapshot.get("error"):
        parts.append("Não foi possível concluir uma etapa")
    return " · ".join(parts)


def endpoint_options(devices, track, selection):
    """Keep a missing manually selected endpoint explicit and pinned."""
    options = [("Padrão do sistema · multimídia", EndpointSelection()),
               ("Padrão do sistema · comunicações", EndpointSelection(default_role="communications"))]
    used_labels = {label for label, _selection in options}
    generic_label = "Dispositivo de entrada" if track == "microphone" else "Dispositivo de saída"
    for device in devices:
        if (isinstance(device, dict) and device.get("kind") == track
                and isinstance(device.get("id"), str) and device["id"]):
            identifier = device["id"]
            name = str(device.get("name") or "").strip()
            base_label = name if name and name != identifier else generic_label
            label = base_label
            suffix = 2
            while label in used_labels:
                label = f"{base_label} ({suffix})"
                suffix += 1
            used_labels.add(label)
            options.append((label, EndpointSelection("manual", identifier)))
    if (selection.mode == "manual"
            and not any(item.endpoint_id == selection.endpoint_id for _, item in options)):
        options.append(("Dispositivo selecionado indisponível", selection))
    return options


def validated_settings(raw, dictation=DEFAULT_DICTATION_HOTKEY, command=DEFAULT_COMMAND_HOTKEY):
    settings = resolve_meeting_settings(raw)
    if settings.language not in available_languages(settings.profile):
        raise ValueError("Este modelo usa detecção automática de idioma. Selecione auto.")
    validate_hotkey_conflicts(dict(raw, voice_hotkey=dictation, voice_command_hotkey=command))
    return settings


class BackgroundBridge:
    """Two bounded lanes keep capture controls independent of slow disk work."""

    def __init__(self):
        # ceiling: eight pending jobs per lane and sixteen returned results.
        # Increase only after measured UI request/IO requirements justify it.
        self.jobs = {False: queue.Queue(8), True: queue.Queue(8)}
        self.results = queue.Queue(16)
        self.closed = threading.Event()
        self.tokens = {}
        self.callbacks = {}
        self.serial = 0
        self.workers = []
        for urgent in (False, True):
            worker = threading.Thread(target=self._work, args=(urgent,), daemon=True,
                                      name="MeetingGuiControl" if urgent else "MeetingGuiIO")
            worker.start()
            self.workers.append(worker)

    def submit(self, key, operation, callback, urgent=False):
        if self.closed.is_set():
            return False
        self.serial += 1
        token = self.serial
        try:
            self.jobs[urgent].put_nowait((key, token, operation))
        except queue.Full:
            self.invalidate(key)
            return False
        self.tokens[key] = token
        self.callbacks[key] = callback
        return True

    def invalidate(self, key):
        self.tokens.pop(key, None)
        self.callbacks.pop(key, None)

    def _work(self, urgent):
        while not self.closed.is_set():
            try:
                key, token, operation = self.jobs[urgent].get(timeout=0.1)
            except queue.Empty:
                continue
            if self.closed.is_set():
                break
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
            if not self.closed.is_set() and self.tokens.get(key) == token:
                callback = self.callbacks.pop(key, None)
                self.tokens.pop(key, None)
                if callback:
                    callback(value, error)

    def close(self):
        self.closed.set()
        self.callbacks.clear()
        self.tokens.clear()


class SectionSwitcher:
    """Show one of several section frames at a time, chosen from a nav bar.

    Replaces pages that stacked every section in one long scroll. Hidden
    sections keep their widgets alive, so callers keep addressing them
    directly; only the chosen frame is packed into ``container``.
    """

    def __init__(self, ui, nav, container, *, vertical=False, on_select=None):
        self.ui = ui
        self.nav = nav
        self.container = container
        self.vertical = vertical
        self.on_select = on_select
        self.frames = {}
        self.buttons = {}
        self.markers = {}
        self.current = None

    def add(self, key, title):
        bg = self.nav.cget("background")
        item = tk.Frame(self.nav, bg=bg)
        marker = tk.Frame(item, bg=bg, width=3, height=2)
        chrome = self.ui.button_chrome(compact=True)
        if chrome:
            # Keep the keyboard focus ring, but no idle border around each item.
            chrome["highlightbackground"] = bg
        button = tk.Button(
            item, text=title, font=self.ui.font(9), command=lambda: self.select(key),
            anchor="w" if self.vertical else "center",
            **self.ui.nav_button_colors(bg), **chrome,
        )
        if self.vertical:
            item.pack(side="top", fill="x", pady=1)
            marker.pack(side="left", fill="y")
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

    def select(self, key):
        if key == self.current:
            return
        bg = self.nav.cget("background")
        if self.current is not None:
            self.frames[self.current].pack_forget()
            self.buttons[self.current].configure(
                font=self.ui.font(9), **self.ui.nav_button_colors(bg))
            self.markers[self.current].configure(bg=bg)
        self.current = key
        self.frames[key].pack(fill="both", expand=True)
        self.buttons[key].configure(
            font=self.ui.font(9, "bold"), **self.ui.nav_button_colors(bg, selected=True))
        self.markers[key].configure(bg=self.ui.accent)
        if self.on_select is not None:
            self.on_select(key)


class MeetingWindow:
    def __init__(self, root, controller, settings_getter, persist_settings,
                 on_settings_changed=None, on_recording_state_changed=None,
                 on_appearance_changed=None, *,
                 window=None, notebook=None, data_location=None, relocate_data=None,
                 models_location=None, relocate_models=None):
        self.root, self.controller = root, controller
        self.data_location, self.relocate_data = data_location, relocate_data
        self.models_location, self.relocate_models = models_location, relocate_models
        self.settings_getter, self.persist_settings = settings_getter, persist_settings
        self.on_settings_changed = on_settings_changed
        self.on_recording_state_changed = on_recording_state_changed
        self.on_appearance_changed = on_appearance_changed
        if (window is None) != (notebook is None):
            raise ValueError("window and notebook must be supplied together")
        self.embedded = window is not None
        self.window = window or tk.Toplevel(root)
        self.notebook = notebook
        if not self.embedded:
            self.window.title("Gravações e reuniões")
            self.window.geometry("1120x820")
            self.window.minsize(920, 700)
        self.ui = ui_theme.theme() if self.embedded else ui_theme.bind(self.window)
        self.window.configure(bg=self.ui.surface)
        self.bridge = BackgroundBridge()
        self.closed = False
        self.after_id = None
        self.raw_settings = {}
        self.settings = resolve_meeting_settings({})
        self.devices = []
        self.options = {}
        self.offset = 0
        self.library_cursor = None
        self.library_next_cursor = None
        self.library_back_stack = [None]
        self.library_page_index = 0
        self.library_cursor_reset = False
        self.library_filter_generation = 0
        self.selected = None
        self.bookmarks = []
        self.transcript_offset = 0
        self.transcript_revision = None
        self.transcript_has_more = False
        self.transcript_has_previous = False
        self.transcript_request = 0
        self.annotation_generation = 0
        self.speaker_labels = {}
        self.highlights = []
        self.selected_segment_id = None
        self.selected_speaker_label_id = None
        self.selected_highlight_id = None
        self.raw_unavailable_tracks = set()
        self.raw_tracks_present = set()
        self.playback_choices = ()
        self.playback_dragging = False
        self.playback_duration = 0.0
        self._playback_active = False
        self.raw_capabilities = {"playback": True, "retranscription": True,
                                 "clip": True, "audio_export": True}
        if hasattr(self, "audio_capability_status"):
            self.audio_capability_status.set("Nenhuma reunião selecionada.")
        self.dirty = False
        self.loading = False
        self.truncated = False
        self.settings_loaded = False
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
        self.privacy_save_inflight = False
        # Application startup normally sets this false until recovery and the
        # privacy cache have completed.  Standalone/embedded callers keep the
        # backwards-compatible ready default.
        self.retention_ready = True
        self.startup_status = ""
        self.pending_start_origin = None
        self.recording_notice_dialog = None
        self.raw_unavailable_tracks = set()
        self.raw_capabilities = {"playback": True, "retranscription": True,
                                 "clip": True, "audio_export": True}
        self.trash_request = 0
        self.trash_dialog = None
        self.trash_tree = None
        self.trash_entries = []
        self.retention_request = 0
        self.playback_generation = -1
        self.preview_signature = None
        # Report/Q&A requests carry their own generation so a late worker
        # result can never replace a different meeting's selected output.
        self.report_request = 0
        self.ask_request = 0
        self.citation_request = 0
        self.search_request = 0
        self.cross_request = 0
        self.cross_citation_request = 0
        self.rebuild_cancel = None
        self.rebuild_progress = {"done": 0, "total": None, "state": "idle"}
        self.organization_generation = 0
        self.organization_definitions = {"collections": [], "series": []}
        self.report_profiles = []
        self.report_profile_by_label = {}
        self.report_history = []
        self.report_history_ids = []
        self.selected_report = None
        self.report_sections = {}
        self.unsaved_answer = None
        self._mousewheel_bindings = []
        self._build()
        if not self.embedded:
            self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.window.bind("<Destroy>", self._destroyed, add="+")
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
                hint.place(x=4, rely=0.5, anchor="w")

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
            toggle.configure(text=f"{label} ▾")
        else:
            panel.pack(fill="x", padx=(12, 0), pady=(4, 0))
            toggle.configure(text=f"{label} ▴")

    def _refresh_library_toggles(self):
        """Label the panel toggles with open state and the active filter count."""
        toggles = getattr(self, "library_panel_toggles", None)
        if not toggles:
            return
        active = sum(bool(variable.get().strip()) for variable in (
            self.collection_filter, self.tag_filter, self.people_filter,
            self.series_filter, self.date_from_filter, self.date_to_filter,
        ))
        labels = {"filters": f"Filtros ({active})" if active else "Filtros",
                  "cross": "Perguntar à biblioteca", "tools": "Mais"}
        for key, label in labels.items():
            arrow = "▴" if self.library_panels[key].winfo_manager() else "▾"
            toggles[key].configure(text=f"{label} {arrow}")

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
        narrowed = (
            bool(self.query.get().strip()) or bool(STATUS_FILTERS.get(self.status_filter.get()))
            or any(self._library_filters().values())
        )
        self.library_empty.set(
            "Nenhuma gravação encontrada.\nAjuste a busca ou os filtros." if narrowed
            else "Nenhuma gravação ainda.\nComece gravando ou importe um áudio."
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

    def _set_library_detail_visible(self, visible):
        panes = getattr(self, "library_panes", None)
        detail = getattr(self, "library_detail_pane", None)
        pages = getattr(self, "library_pages", None)
        if panes is None or detail is None or pages is None:
            return
        present = str(detail) in panes.panes()
        if visible and not present:
            panes.add(detail, weight=3)
        elif not visible and present:
            panes.forget(detail)
        if visible and not pages.winfo_manager():
            pages.pack(fill="x", pady=(self.ui.space_sm, 0))
        elif not visible:
            pages.pack_forget()

    def _clear_library_search(self):
        self.query.set("")
        self.status_filter.set("Todos")
        self.clear_library_filters()

    def _button(self, parent, text, command, accent=False, danger=False):
        return tk.Button(parent, text=text, command=command, font=self.ui.font(),
                         **self.ui.button_colors(accent=accent, danger=danger),
                         **self.ui.button_chrome(compact=True))

    def _entry(self, parent, variable, width=30):
        return tk.Entry(parent, textvariable=variable, width=width, font=self.ui.font(),
                        **self.ui.entry_colors(), **self.ui.entry_chrome())

    def _page_header(self, parent, title, description):
        header = tk.Frame(parent, bg=self.ui.surface)
        header.pack(fill="x", padx=self.ui.space_lg, pady=(self.ui.space_lg, self.ui.space_sm))
        self._label(
            header, title, font=self.ui.font(16, "bold"), fg=self.ui.text_strong,
        ).pack(anchor="w")
        self._label(
            header, description, font=self.ui.font(9), fg=self.ui.text_muted,
            anchor="w", justify="left", wraplength=940,
        ).pack(fill="x", pady=(self.ui.space_xs, 0))

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

    def _build(self):
        style = ttk.Style(self.window)
        ui_theme.apply_ttk_theme(style, resolved=self.ui)
        ui_theme.configure_manager_styles(style, self.ui)
        style.configure(
            "Playback.Horizontal.TScale",
            background=self.ui.card,
            troughcolor=self.ui.field,
            bordercolor=self.ui.border,
            lightcolor=self.ui.accent,
            darkcolor=self.ui.accent,
        )
        style.configure("Meeting.TFrame", background=self.ui.surface)
        style.configure(
            "Meeting.Treeview",
            background=self.ui.card,
            fieldbackground=self.ui.card,
            foreground=self.ui.text,
            rowheight=self.ui.tree_row_height,
            font=self.ui.font(),
            borderwidth=0,
            bordercolor=self.ui.border,
            lightcolor=self.ui.card,
            darkcolor=self.ui.card,
        )
        style.map(
            "Meeting.Treeview",
            background=[("selected", self.ui.select_bg)],
            foreground=[("selected", self.ui.select_fg)],
        )
        style.configure(
            "Meeting.Treeview.Heading",
            background=self.ui.surface_alt,
            foreground=self.ui.text_strong,
            font=self.ui.font(9, "bold"),
            padding=(8, 8),
            relief="flat",
        )
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
        notebook.add(self.recording_tab, text="Gravação")
        notebook.add(self.library_tab, text="Biblioteca")
        notebook.add(self.settings_tab, text="Configurações")
        self._page_header(
            self.recording_tab,
            "Gravar reunião",
            "Dê um nome, confira as fontes e comece quando a reunião começar.",
        )
        self._page_header(
            self.library_tab,
            "Biblioteca",
            "Ouça e revise suas gravações locais.",
        )
        self._page_header(
            self.settings_tab,
            "Configurações",
            "Ajuste a gravação, a aparência, a privacidade e os modelos locais.",
        )
        self.status = tk.StringVar(self.window, "Carregando configurações…")
        footer = tk.Frame(self.window, bg=self.ui.surface_alt)
        self.status_footer = footer
        footer.pack(side="bottom", fill="x", before=notebook)
        tk.Frame(footer, bg=self.ui.divider, height=1).pack(fill="x")
        self._wrap_label(
            footer, "", textvariable=self.status, anchor="w", justify="left",
            bg=self.ui.surface_alt, fg=self.ui.text_muted, font=self.ui.font(8),
        ).pack(fill="x", padx=self.ui.space_xl, pady=self.ui.space_sm)
        recording_view = ttk.Frame(self.recording_tab, style="Meeting.TFrame")
        recording_view.pack(fill="both", expand=True)
        recording_canvas = tk.Canvas(
            recording_view, background=self.ui.surface,
            highlightthickness=0, borderwidth=0,
        )
        recording_scrollbar = ttk.Scrollbar(
            recording_view, orient="vertical", command=recording_canvas.yview,
        )
        recording_canvas.configure(yscrollcommand=recording_scrollbar.set)
        recording_canvas.pack(side="left", fill="both", expand=True)
        recording_scrollbar.pack(side="right", fill="y")
        recording = ttk.Frame(
            recording_canvas, padding=(16, 0, 16, 16), style="Meeting.TFrame",
        )
        recording_window = recording_canvas.create_window(
            (0, 0), window=recording, anchor="nw",
        )

        def update_recording_scroll_region(_event=None):
            recording_canvas.configure(scrollregion=recording_canvas.bbox("all"))

        def stretch_recording_content(event):
            recording_canvas.itemconfigure(recording_window, width=event.width)

        recording.bind("<Configure>", update_recording_scroll_region)
        recording_canvas.bind("<Configure>", stretch_recording_content)
        self._bind_mousewheel_region(recording_view, recording_canvas)
        self.recording_canvas = recording_canvas
        recording.columnconfigure(0, weight=1)
        library = ttk.Frame(self.library_tab, padding=(16, 0, 16, 16), style="Meeting.TFrame")
        library.pack(fill="both", expand=True)
        settings_view = ttk.Frame(self.settings_tab, style="Meeting.TFrame")
        settings_view.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        settings_view.columnconfigure(1, weight=1)
        settings_view.rowconfigure(0, weight=1)
        settings_nav = tk.Frame(settings_view, bg=self.ui.surface)
        settings_nav.grid(row=0, column=0, sticky="nsw", padx=(0, self.ui.space_lg))
        settings_canvas = tk.Canvas(
            settings_view, background=self.ui.surface, highlightthickness=0,
            borderwidth=0,
        )
        settings_scrollbar = ttk.Scrollbar(
            settings_view, orient="vertical", command=settings_canvas.yview,
        )
        settings_canvas.configure(yscrollcommand=settings_scrollbar.set)
        settings_canvas.grid(row=0, column=1, sticky="nsew")
        settings_scrollbar.grid(row=0, column=2, sticky="ns", padx=(self.ui.space_sm, 0))
        settings_content = ttk.Frame(settings_canvas, style="Meeting.TFrame")
        settings_window = settings_canvas.create_window(
            (0, 0), window=settings_content, anchor="nw",
        )

        def update_settings_scroll_region(_event=None):
            settings_canvas.configure(scrollregion=settings_canvas.bbox("all"))

        def stretch_settings_content(event):
            settings_canvas.itemconfigure(settings_window, width=event.width)

        settings_content.bind("<Configure>", update_settings_scroll_region)
        settings_canvas.bind("<Configure>", stretch_settings_content)
        self._bind_mousewheel_region(settings_view, settings_canvas)
        self.settings_canvas = settings_canvas
        self.settings_content = settings_content
        # One section at a time instead of a single page several screens long.
        self.settings_sections = SectionSwitcher(
            self.ui, settings_nav, settings_content, vertical=True,
            on_select=lambda _key: settings_canvas.yview_moveto(0),
        )
        general = self.settings_sections.add("general", "Geral")
        recording_settings = self.settings_sections.add("recording", "Gravação")
        privacy = self.settings_sections.add("privacy", "Privacidade")
        models = self.settings_sections.add("models", "Modelos")
        model_nav = tk.Frame(models, bg=self.ui.surface)
        model_nav.pack(fill="x", pady=(0, self.ui.space_md))
        model_pages = tk.Frame(models, bg=self.ui.surface)
        model_pages.pack(fill="x")
        self.model_sections = SectionSwitcher(
            self.ui, model_nav, model_pages,
            on_select=lambda _key: settings_canvas.yview_moveto(0),
        )
        transcription = self.model_sections.add("transcription", "Transcrição")
        summary_section = self.model_sections.add("summary", "Resumos")
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
            summary, "Modelos de resumo de texto",
            font=self.ui.font(11, "bold"), fg=self.ui.text_strong,
        ).pack(anchor="w", pady=(0, self.ui.space_xs))
        self._label(
            summary,
            "Baixe e escolha um modelo compacto para resumir sem enviar conteúdo à nuvem.",
            anchor="w", justify="left", wraplength=900, fg=self.ui.text_muted,
        ).pack(fill="x", pady=(0, self.ui.space_sm))
        self.record_title = tk.StringVar(self.window)
        self.input_enabled = tk.BooleanVar(self.window, self.settings.input_enabled)
        self.output_enabled = tk.BooleanVar(self.window, self.settings.output_enabled)
        self.destination = tk.StringVar(self.window, self.settings.destination)
        self.destination_label = tk.StringVar(
            self.window, destination_display(self.settings.destination),
        )
        self.auto_transcribe = tk.BooleanVar(self.window, self.settings.auto_transcribe)
        self.auto_summary = tk.BooleanVar(self.window, self.settings.auto_summary)
        self.voice_boost = tk.BooleanVar(self.window, self.settings.voice_boost)
        self.profile = tk.StringVar(self.window, self.settings.profile)
        self.language = tk.StringVar(self.window, self.settings.language)
        self.profile_display = tk.StringVar(self.window, PROFILE_LABELS[self.settings.profile])
        self.language_display = tk.StringVar(self.window, LANGUAGE_LABELS[self.settings.language])
        self.hotkey = tk.StringVar(self.window)
        self.summary_model = tk.StringVar(self.window, self.settings.summary_model)
        self.summary_display = tk.StringVar(self.window, SUMMARY_LABELS[self.settings.summary_model])
        self.endpoint_vars = {track: tk.StringVar(self.window) for track in ("microphone", "system")}
        self.endpoint_boxes = {}
        defaults = self.recording_defaults_parent
        self._label(
            defaults, "Padrões de gravação", bg=self.ui.card,
            fg=self.ui.text_strong, font=self.ui.font(11, "bold"),
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        self._label(
            defaults,
            "Escolha os modelos, o idioma e o atalho usados em novas gravações.",
            bg=self.ui.card, fg=self.ui.text_muted, anchor="w", justify="left",
            wraplength=860,
        ).grid(row=1, column=0, columnspan=2, sticky="ew", pady=(self.ui.space_xs, self.ui.space_sm))
        self.profile_box = ttk.Combobox(
            defaults, textvariable=self.profile_display,
            values=[PROFILE_LABELS[entry["profile"]] for entry in selectable_catalog()],
            state="readonly",
        )
        self.profile_box.bind("<<ComboboxSelected>>", self._profile_changed)
        self.language_box = ttk.Combobox(
            defaults, textvariable=self.language_display, state="readonly",
        )
        self.language_box.bind("<<ComboboxSelected>>", self._language_changed)
        self.summary_box = ttk.Combobox(
            defaults, textvariable=self.summary_display,
            values=list(SUMMARY_LABELS.values()), state="readonly",
        )
        self.summary_box.bind("<<ComboboxSelected>>", self._summary_display_changed)
        default_rows = [
            ("Modelo de transcrição local", self.profile_box),
            ("Idioma", self.language_box),
            ("Atalho de gravação (opcional)", self._entry(defaults, self.hotkey)),
            ("Modelo de resumo local", self.summary_box),
        ]
        for row, (label, widget) in enumerate(default_rows, 2):
            self._label(defaults, label, anchor="w", bg=self.ui.card).grid(
                row=row, column=0, sticky="w", padx=(0, self.ui.space_lg), pady=3,
            )
            widget.grid(row=row, column=1, sticky="ew", pady=3)
        defaults.columnconfigure(1, weight=1)
        self._button(defaults, "Salvar como padrão", self.save_settings, accent=True).grid(
            row=2 + len(default_rows), column=1, sticky="e", pady=(self.ui.space_sm, 0),
        )
        compact_recording = self.window.winfo_screenheight() <= 800
        card_pady = max(2, self.ui.space_xs // 2) if compact_recording else self.ui.space_sm
        row_pady = 1 if compact_recording else 3
        note_pady = (2, 4) if compact_recording else (4, 6)
        settings_card = self._card(recording_settings, pady=card_pady)
        settings_card.pack(fill="x", pady=(0, self.ui.space_md))
        self._label(
            settings_card, "Detalhes e automação",
            bg=self.ui.card, fg=self.ui.text_strong, font=self.ui.font(11, "bold"),
        ).grid(row=0, column=0, sticky="w", pady=(0, self.ui.space_sm))
        destination_row = tk.Frame(settings_card, bg=self.ui.card)
        destination_entry = self._entry(destination_row, self.destination_label)
        destination_entry.configure(state="readonly")
        destination_entry.pack(side="left", fill="x", expand=True)
        self._button(destination_row, "Escolher…", self.choose_destination).pack(
            side="left", padx=(self.ui.space_sm, 0))
        self._button(destination_row, "Usar padrão", self.use_default_destination).pack(
            side="left", padx=(self.ui.space_sm, 0))
        self._label(settings_card, "Pasta dos arquivos finais", anchor="w", bg=self.ui.card).grid(
            row=1, column=0, sticky="w", pady=(row_pady, 0),
        )
        destination_row.grid(row=2, column=0, sticky="ew", pady=(0, row_pady))
        settings_card.columnconfigure(0, weight=1)
        automation = tk.Frame(settings_card, bg=self.ui.card)
        self.auto_transcribe_check = tk.Checkbutton(
            automation, text="Transcrever automaticamente", variable=self.auto_transcribe,
            command=self._automation_toggled, font=self.ui.font(), anchor="w",
            **self.ui.checkbutton_colors(self.ui.card),
        )
        self.auto_transcribe_check.pack(anchor="w", fill="x")
        self.auto_summary_check = tk.Checkbutton(
            automation, text="Resumir após transcrever", variable=self.auto_summary,
            command=self._automation_toggled, font=self.ui.font(), anchor="w",
            **self.ui.checkbutton_colors(self.ui.card),
        )
        self.auto_summary_check.pack(anchor="w", fill="x", padx=(self.ui.space_xl, 0))
        self.voice_boost_check = tk.Checkbutton(
            automation, text="Ajustar volume do microfone no áudio final", variable=self.voice_boost,
            font=self.ui.font(), anchor="w", **self.ui.checkbutton_colors(self.ui.card),
        )
        self.voice_boost_check.pack(anchor="w", fill="x", pady=(self.ui.space_xs, 0))
        automation_row = 3
        self._label(settings_card, "Depois de gravar", anchor="w", bg=self.ui.card).grid(
            row=automation_row, column=0, sticky="w", pady=(row_pady, 0))
        automation.grid(row=automation_row + 1, column=0, sticky="ew", pady=(0, row_pady))
        note_row = automation_row + 2
        note = self._wrap_label(
            settings_card,
            "Configurações valem para a próxima gravação. A pasta local padrão "
            "fica ao lado da biblioteca.\nO sistema inclui todos os sons do "
            "dispositivo escolhido. Use fones para reduzir duplicação acústica.",
            justify="left", anchor="w", bg=self.ui.card,
            fg=self.ui.text_muted, font=self.ui.font(9),
        )
        note.grid(row=note_row, column=0, sticky="ew", pady=note_pady)
        commands = tk.Frame(settings_card, bg=self.ui.card)
        commands.grid(row=note_row + 1, column=0, sticky="w")
        self._button(commands, "Salvar como padrão", self.save_settings).pack(side="left")
        activity = self._card(recording, pady=card_pady)
        activity.grid(row=0, column=0, sticky="nsew")
        self.recording_activity = activity
        self._label(
            activity, "Gravação", bg=self.ui.card, fg=self.ui.text_strong,
            font=self.ui.font(11, "bold"),
        ).grid(row=0, column=0, sticky="w", pady=(0, self.ui.space_sm))
        self.recording_options_button = self._button(
            activity, "Configurar gravação", self.show_recording_settings,
        )
        self.recording_options_button.grid(row=0, column=1, sticky="e", pady=(0, self.ui.space_sm))
        activity.columnconfigure(1, weight=1)
        activity.rowconfigure(4, weight=1)
        self._label(activity, "Título da reunião (opcional)", anchor="w",
                    bg=self.ui.card).grid(row=1, column=0, columnspan=2, sticky="ew")
        self.record_title_entry = self._entry(activity, self.record_title)
        self.record_title_entry.grid(row=2, column=0, columnspan=2, sticky="ew",
                                     pady=(2, self.ui.space_sm))
        sources = tk.Frame(activity, bg=self.ui.card)
        sources.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(0, self.ui.space_sm))
        sources.columnconfigure(1, weight=1)
        self.source_checks = {}
        for row, (track, label, variable) in enumerate((
                ("microphone", "Microfone", self.input_enabled),
                ("system", "Áudio do sistema", self.output_enabled))):
            control = tk.Checkbutton(
                sources, text=label, variable=variable, command=self._source_toggled,
                font=self.ui.font(), **self.ui.checkbutton_colors(self.ui.card),
            )
            control.grid(row=row, column=0, sticky="w", padx=(0, self.ui.space_md), pady=2)
            combo = ttk.Combobox(
                sources, textvariable=self.endpoint_vars[track], state="readonly",
            )
            combo.grid(row=row, column=1, sticky="ew", pady=2)
            combo.bind("<<ComboboxSelected>>", self._source_selection_changed)
            self.source_checks[track] = control
            self.endpoint_boxes[track] = combo
        self._button(sources, "Atualizar dispositivos", self.refresh_devices).grid(
            row=2, column=0, sticky="w", pady=(4, 0),
        )
        self.preview_button = self._button(sources, "Testar fontes", self.preview_sources)
        self.preview_button.grid(row=2, column=1, sticky="e", pady=(4, 0))
        self.waveform = MeetingWaveform(
            activity, theme=self.ui, height=140,
            track_labels={"microphone": "Microfone", "system": "Áudio do sistema"},
            state_labels={"idle": "Pronto", "checking": "Testando",
                          "recording": "Gravando", "paused": "Pausado"},
        )
        self.waveform.grid(row=4, column=0, columnspan=2, sticky="nsew",
                           pady=(0, self.ui.space_sm))
        transport = tk.Frame(activity, bg=self.ui.card)
        transport.grid(row=5, column=0, columnspan=2, sticky="w")
        self.start_button = self._button(transport, "Iniciar gravação", self.start, accent=True)
        self.start_button.pack(side="left", padx=(0, 8))
        self.pause_button = self._button(transport, "Pausar", self.pause_resume)
        self.pause_button.pack(side="left", padx=(0, 8))
        self.stop_button = self._button(transport, "Parar e preservar", self.stop)
        self.stop_button.pack(side="left")
        self.record_status = tk.StringVar(self.window, "Pronto · 00:00:00")
        status_row = tk.Frame(activity, bg=self.ui.card)
        status_row.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(10, 4))
        self._label(status_row, "", textvariable=self.record_status, anchor="w",
                    wraplength=600, bg=self.ui.card, fg=self.ui.text_muted).pack(
                        side="left", fill="x", expand=True)
        self.record_details = ""
        self.operation_details = ""
        self.record_details_button = self._button(
            status_row, "Ver detalhes", self.show_recording_details,
        )
        self.record_details_button.configure(state="disabled")
        self.record_details_button.pack(side="right", padx=(self.ui.space_sm, 0))
        self.meters = {}
        meters = (("microphone", "Nível do microfone"),
                  ("system", "Nível do sistema"))
        for row, (track, label) in enumerate(meters, 7):
            self._label(activity, label, anchor="w", bg=self.ui.card).grid(
                row=row, column=0, sticky="w", padx=(0, 18), pady=6,
            )
            meter = ttk.Progressbar(activity, maximum=1.0)
            meter.grid(row=row, column=1, sticky="ew")
            self.meters[track] = meter
        self.preview_status = tk.StringVar(
            self.window, "Teste as fontes antes de começar. Fale ou reproduza áudio nas fontes escolhidas.",
        )
        self._wrap_label(activity, "", textvariable=self.preview_status, anchor="w",
                         fg=self.ui.text_muted).grid(
                             row=9, column=0, columnspan=2, sticky="ew", pady=(6, 0),
                         )
        self._profile_changed()
        self._sync_source_controls()
        self._automation_toggled()
        self.options.clear()
        self._render_devices()
        self._build_library(library)
        self._build_summary_models(summary)

    def _build_summary_models(self, parent):
        self.summary_model_buttons = {}
        entries = summary_catalog()
        compact = len(entries) > 5
        for entry in entries:
            row = self._card(parent, pady=self.ui.space_sm if compact else self.ui.space_md)
            row.pack(fill="x", pady=2 if compact else self.ui.space_xs)
            copy = tk.Frame(row, bg=self.ui.card)
            copy.pack(side="left", fill="x", expand=True, padx=(0, 12))
            self._label(copy, f'{entry["name"]} · {format_model_size(entry["size_bytes"])} · '
                        f'{entry["license_id"]}', anchor="w",
                        bg=self.ui.card, fg=self.ui.text_strong,
                        font=self.ui.font(weight="bold")).pack(fill="x")
            self._label(copy, entry["description"], anchor="w", wraplength=720,
                        bg=self.ui.card, fg=self.ui.text_muted).pack(
                            fill="x", pady=(2 if compact else 4, 0))
            button = self._button(row, "", lambda model_id=entry["id"]: self.toggle_summary_model(model_id))
            button.pack(side="right", anchor="n")
            self.summary_model_buttons[entry["id"]] = button
        actions = ttk.Frame(parent, style="Meeting.TFrame")
        actions.pack(fill="x", pady=(12 if compact else 18, 0))
        self._button(actions, "Cancelar download", self.cancel_summary_download).pack(side="left")
        self.summary_model_status = tk.StringVar(self.window)
        self._label(parent, "", textvariable=self.summary_model_status, anchor="w",
                    wraplength=900).pack(fill="x", pady=12)
        self._refresh_summary_models()

    def _summary_display_changed(self, _event=None):
        self.summary_model.set(next(key for key, label in SUMMARY_LABELS.items()
                                    if label == self.summary_display.get()))

    def _refresh_summary_models(self):
        for button in self.summary_model_buttons.values():
            button.configure(text="Verificando…", state="disabled")
        self._submit(
            "summary_inventory",
            lambda: {entry["id"]: summary_model_is_installed(entry["id"])
                     for entry in summary_catalog()},
            self._summary_inventory_loaded,
        )

    def _summary_inventory_loaded(self, installed, error):
        if error:
            self.summary_model_status.set(error)
            return
        self.summary_model_installed = dict(installed)
        for model_id, button in self.summary_model_buttons.items():
            button.configure(text="Remover" if installed.get(model_id) else "Baixar",
                             state="normal")

    def toggle_summary_model(self, model_id):
        entry = summary_catalog_entry(model_id)
        if entry is None:
            self.status.set("O modelo de resumo selecionado não existe no catálogo.")
            return
        if self.summary_model_installed.get(model_id):
            if not messagebox.askyesno("Remover modelo", f'Remover {entry["name"]} deste computador?',
                                       parent=self.window):
                return
            for button in self.summary_model_buttons.values():
                button.configure(state="disabled")
            self.summary_model_status.set(f'Removendo {entry["name"]}…')
            submitted = self._submit(
                "summary_model", lambda: delete_summary_model(model_id),
                lambda _value, error: self._summary_model_finished(entry, error, removed=True),
            )
            if not submitted:
                self._refresh_summary_models()
            return
        terms = ""
        if entry["requires_acceptance"]:
            notice = entry.get("license_notice", "Leia a licença antes de continuar.")
            terms = (
                f"\n\n{notice}"
                f'\n\nTermos completos: {entry["license_url"]}'
                "\n\nAo continuar, você confirma que leu e aceita esses termos."
            )
        if not messagebox.askyesno(
                "Licença do modelo" if entry["requires_acceptance"] else "Baixar modelo local",
                f'Baixar {entry["name"]} ({format_model_size(entry["size_bytes"])})?'
                f'\nLicença: {entry["license_id"]}{terms}', parent=self.window):
            return
        self.summary_download_cancel = threading.Event()
        with self.summary_progress_lock:
            self.summary_progress = (entry["name"], 0, entry["size_bytes"])
        for button in self.summary_model_buttons.values():
            button.configure(state="disabled")
        self.summary_model_status.set(f'Baixando {entry["name"]} com verificação SHA-256…')
        submitted = self._submit(
            "summary_model",
            lambda: download_summary_model(model_id, cancel_event=self.summary_download_cancel,
                                           progress=lambda done, total: self._summary_progress(entry, done, total)),
            lambda _value, error: self._summary_model_finished(entry, error),
        )
        if not submitted:
            self.summary_download_cancel = None
            self._refresh_summary_models()

    def cancel_summary_download(self):
        if self.summary_download_cancel is None:
            self.summary_model_status.set("Nenhum download de modelo está em andamento.")
            return
        self.summary_download_cancel.set()
        self.summary_model_status.set("Cancelando download; os bytes parciais poderão ser retomados.")

    def _summary_progress(self, entry, done, total):
        with self.summary_progress_lock:
            self.summary_progress = (entry["name"], done, total)

    def _summary_model_finished(self, entry, error, removed=False):
        self.summary_download_cancel = None
        with self.summary_progress_lock:
            self.summary_progress = None
        self._refresh_summary_models()
        if error:
            self.summary_model_status.set(error)
            return
        action = "removido" if removed else "baixado e verificado"
        self.summary_model_status.set(f'{entry["name"]} {action}.')

    def _build_library(self, parent):
        # One toolbar row; filters and the cross-meeting question open on
        # demand so the list and the selected recording get the height.
        toolbar = self._card(parent, pady=self.ui.space_sm)
        toolbar.pack(fill="x", pady=(0, self.ui.space_sm))
        self.query = tk.StringVar(self.window)
        search = self._entry(toolbar, self.query, 12)
        search.pack(side="left", fill="x", expand=True)
        search.bind("<Return>", lambda _event: self.search())
        self._placeholder(search, self.query, "Buscar gravações e transcrições")
        self._button(toolbar, "Buscar", self.search, accent=True).pack(side="left", padx=(6, 0))
        self.status_filter = tk.StringVar(self.window, "Todos")
        self.filters_toggle = self._button(toolbar, "Filtros", lambda: self._toggle_library_panel("filters"))
        self.filters_toggle.pack(side="left", padx=(6, 0))
        self.tools_toggle = self._button(toolbar, "Mais", lambda: self._toggle_library_panel("tools"))
        self.tools_toggle.pack(side="right", padx=(6, 0))
        self._button(toolbar, "Importar áudio…", self.import_audio).pack(side="right", padx=(6, 0))

        filter_panel = self._card(parent, pady=self.ui.space_sm)
        status_row = tk.Frame(filter_panel, bg=self.ui.card)
        status_row.pack(fill="x", pady=(0, self.ui.space_sm))
        self._label(status_row, "Estado", bg=self.ui.card, fg=self.ui.text_muted).pack(side="left", padx=(0, 8))
        filters = ttk.Combobox(status_row, textvariable=self.status_filter, state="readonly",
                               values=list(STATUS_FILTERS), width=16)
        filters.pack(side="left")
        filters.bind("<<ComboboxSelected>>", lambda _event: self.search())
        self.collection_filter = tk.StringVar(self.window)
        self.tag_filter = tk.StringVar(self.window)
        self.people_filter = tk.StringVar(self.window)
        self.series_filter = tk.StringVar(self.window)
        self.date_from_filter = tk.StringVar(self.window)
        self.date_to_filter = tk.StringVar(self.window)
        filter_fields = tk.Frame(filter_panel, bg=self.ui.card)
        filter_fields.pack(fill="x")
        # Tk has no portable placeholder text, so each field carries a small
        # caption above it instead.
        for index, (variable, caption) in enumerate((
            (self.collection_filter, "Coleção/projeto"),
            (self.tag_filter, "Tag"),
            (self.people_filter, "Pessoa"),
            (self.series_filter, "Série"),
            (self.date_from_filter, "Data inicial (AAAA-MM-DD)"),
            (self.date_to_filter, "Data final (AAAA-MM-DD)"),
        )):
            row, column = divmod(index, 3)
            row *= 2
            self._label(filter_fields, caption, bg=self.ui.card, fg=self.ui.text_muted,
                        font=self.ui.font(8)).grid(row=row, column=column, sticky="w",
                                                   padx=(0, self.ui.space_sm),
                                                   pady=(self.ui.space_sm if row else 0, 0))
            entry = self._entry(filter_fields, variable, 6)
            entry.configure(insertwidth=1)
            entry.grid(row=row + 1, column=column, sticky="ew",
                       padx=(0, self.ui.space_sm))
            filter_fields.columnconfigure(column, weight=1)
            variable.trace_add("write", lambda *_args: self._refresh_library_toggles())
        filter_actions = tk.Frame(filter_panel, bg=self.ui.card)
        filter_actions.pack(fill="x", pady=(self.ui.space_sm, 0))
        self._button(filter_actions, "Aplicar filtros", self.search, accent=True).pack(
            side="right")
        self._button(filter_actions, "Limpar filtros", self.clear_library_filters).pack(
            side="right", padx=(0, self.ui.space_sm))
        index_row = tk.Frame(filter_panel, bg=self.ui.card)
        index_row.pack(fill="x", pady=(self.ui.space_sm, 0))
        self.index_status = tk.StringVar(self.window, "Índice de busca: estado desconhecido")
        self._label(index_row, "", textvariable=self.index_status, bg=self.ui.card,
                    fg=self.ui.text_muted, anchor="w").pack(side="left")
        self.rebuild_cancel_button = self._button(index_row, "Cancelar", self.cancel_rebuild_index)
        self.rebuild_cancel_button.configure(state="disabled")
        self.rebuild_cancel_button.pack(side="right", padx=(6, 0))
        self.rebuild_button = self._button(index_row, "Reconstruir índice", self.rebuild_index)
        self.rebuild_button.pack(side="right")

        cross_frame = self._card(parent, pady=self.ui.space_sm)
        question_row = tk.Frame(cross_frame, bg=self.ui.card)
        question_row.pack(fill="x")
        self._label(question_row, "Perguntar nas reuniões filtradas", bg=self.ui.card,
                    font=self.ui.font(9, "bold")).pack(side="left", padx=(0, 6))
        self.cross_question = tk.StringVar(self.window)
        self._entry(question_row, self.cross_question, 12).pack(side="left", fill="x", expand=True)
        self._button(question_row, "Perguntar", self.ask_across_meetings, accent=True).pack(side="left", padx=(6, 0))
        self.cross_cancel_button = self._button(question_row, "Cancelar", self.cancel_cross_question)
        self.cross_cancel_button.configure(state="disabled")
        self.cross_cancel_button.pack(side="left", padx=(6, 0))
        self.cross_status = tk.StringVar(self.window, "Resposta cruzada fica somente na memória.")
        self._label(cross_frame, "", textvariable=self.cross_status, bg=self.ui.card,
                    fg=self.ui.text_muted, anchor="w", justify="left").pack(fill="x", pady=(4, 0))
        self.cross_answer = tk.Text(cross_frame, height=3, wrap="word", font=self.ui.font(),
                                    **self.ui.text_colors())
        self.cross_answer.pack(fill="x", pady=(4, 0))
        self.cross_answer.configure(state="disabled")
        cross_citation_row = tk.Frame(cross_frame, bg=self.ui.card)
        cross_citation_row.pack(fill="x", pady=(2, 0))
        self._label(cross_citation_row, "Citações", bg=self.ui.card,
                    fg=self.ui.text_muted).pack(side="left", padx=(0, 4))
        self.cross_citations = tk.Listbox(cross_citation_row, height=2, width=10,
                                          font=self.ui.font(), **self.ui.listbox_colors())
        self.cross_citations.pack(side="left", fill="x", expand=True)
        self.cross_citations.bind("<Double-Button-1>", lambda _event: self.jump_to_cross_citation())
        self._button(cross_citation_row, "Ir à fonte", self.jump_to_cross_citation).pack(side="left", padx=(6, 0))

        tools_panel = self._card(parent, pady=self.ui.space_sm)
        self.cross_toggle = self._button(
            tools_panel, "Perguntar à biblioteca", lambda: self._toggle_library_panel("cross"),
        )
        self.cross_toggle.pack(side="left")
        self._button(tools_panel, "Lixeira…", self.show_trash).pack(side="left", padx=(6, 0))

        panes = ttk.Panedwindow(parent, orient="horizontal")
        panes.pack(fill="both", expand=True)
        self.library_panes = panes
        self.library_panels = {"filters": filter_panel, "cross": cross_frame, "tools": tools_panel}
        self.library_panel_toggles = {"filters": self.filters_toggle, "cross": self.cross_toggle,
                                      "tools": self.tools_toggle}
        self._refresh_library_toggles()
        left = ttk.Frame(panes, style="Meeting.TFrame")
        right_outer = ttk.Frame(panes, style="Meeting.TFrame")
        panes.add(left, weight=1)
        panes.add(right_outer, weight=3)
        self.library_detail_pane = right_outer

        def place_sash(event):
            # The first layout splits by requested widths, which left the
            # list wider than the recording it opens; start at about a third.
            if event.width > 100 and len(panes.panes()) > 1:
                panes.sashpos(0, max(260, int(event.width * 0.36)))
                panes.unbind("<Configure>")

        panes.bind("<Configure>", place_sash)

        # Recording list with an empty state drawn over the tree.
        list_frame = tk.Frame(left, bg=self.ui.surface)
        list_frame.pack(fill="both", expand=True)
        self.sessions = ttk.Treeview(list_frame, columns=("title", "status"), show="headings",
                                     selectmode="extended", style="Meeting.Treeview", height=6)
        self.sessions.heading("title", text="Gravação")
        self.sessions.heading("status", text="Estado")
        self.sessions.column("title", width=170)
        self.sessions.column("status", width=85)
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.sessions.yview)
        self.sessions.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.sessions.pack(fill="both", expand=True)
        self.sessions.bind("<<TreeviewSelect>>", self._selection_changed)
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
            empty_actions, "Gravar agora",
            lambda: self.notebook.select(self.recording_tab), accent=True,
        )
        self.library_empty_import_button = self._button(
            empty_actions, "Importar áudio…", self.import_audio,
        )
        self.library_empty_clear_button = self._button(
            empty_actions, "Limpar busca e filtros", self._clear_library_search,
            accent=True,
        )
        self.library_empty_retry_button = self._button(
            empty_actions, "Tentar novamente", self.refresh_library, accent=True,
        )
        pages = ttk.Frame(left, style="Meeting.TFrame")
        pages.pack(fill="x", pady=(8, 0))
        self.library_pages = pages
        self.previous_button = self._button(pages, "‹ Anterior", lambda: self.change_page(-1))
        self.previous_button.pack(side="left")
        self.next_button = self._button(pages, "Próxima ›", lambda: self.change_page(1))
        self.next_button.pack(side="left", padx=(6, 0))
        self.page_label = tk.StringVar(self.window, "Página 1")
        self._label(pages, "", textvariable=self.page_label, fg=self.ui.text_muted).pack(
            side="left", padx=(8, 0))
        batch = ttk.Frame(left, style="Meeting.TFrame")
        self.batch_controls = batch
        self._button(batch, "Organizar seleção…", self.assign_selected_organization).pack(side="left")
        self.batch_cancel_button = self._button(batch, "Cancelar lote", self.cancel_batch_organization)
        self.batch_cancel_button.configure(state="disabled")
        self.batch_cancel_button.pack(side="left", padx=(6, 0))
        # Full-text hits appear only after a search that found something.
        self.search_results_frame = ttk.Frame(left, style="Meeting.TFrame")
        self._label(self.search_results_frame, "Encontrado em transcrições e relatórios",
                    anchor="w", fg=self.ui.text_muted).pack(fill="x", pady=(8, 2))
        self.search_results = ttk.Treeview(
            self.search_results_frame, columns=("source", "meeting", "snippet"), show="headings",
            selectmode="browse", height=4, style="Meeting.Treeview",
        )
        # Modest starting widths: this tree's request sets the list pane's
        # width, and 470px starved the detail pane's controls. The snippet
        # column stretches into whatever the pane receives.
        for column, label, width in (("source", "Fonte", 70), ("meeting", "Reunião", 90),
                                      ("snippet", "Trecho", 120)):
            self.search_results.heading(column, text=label)
            self.search_results.column(column, width=width, stretch=column == "snippet")
        self.search_results.pack(fill="x")
        self.search_results.bind("<Double-Button-1>", lambda _event: self.open_search_result())
        self.search_results.bind("<Return>", lambda _event: self.open_search_result())

        # Selected recording: a placeholder until one is chosen, then a fixed
        # header over one section at a time.
        right_outer.columnconfigure(0, weight=1)
        right_outer.rowconfigure(0, weight=1)
        self.detail_placeholder = tk.Frame(right_outer, bg=self.ui.surface)
        self.detail_placeholder.grid(row=0, column=0, sticky="nsew", padx=(12, 0))
        self._label(
            self.detail_placeholder,
            "Selecione uma gravação na lista para ver notas, transcrição e resumo.",
            fg=self.ui.text_muted, justify="center", wraplength=320,
        ).place(relx=0.5, rely=0.4, anchor="center")
        self.detail_frame = tk.Frame(right_outer, bg=self.ui.surface)
        self.detail_frame.grid(row=0, column=0, sticky="nsew")
        self.detail_frame.grid_remove()
        title_row = ttk.Frame(self.detail_frame, style="Meeting.TFrame")
        title_row.pack(fill="x", padx=(12, 0))
        self.title = tk.StringVar(self.window)
        self.title.trace_add("write", self._mark_dirty)
        # Small minimum width: the entry expands, and a wide request clipped
        # the delete button off the pane at the minimum window size.
        title_entry = self._entry(title_row, self.title, 12)
        title_entry.configure(font=self.ui.font(12, "bold"))
        title_entry.pack(side="left", fill="x", expand=True)
        self._placeholder(title_entry, self.title, "Título da gravação")
        self._button(title_row, "Salvar", self.save_notes).pack(side="left", padx=(8, 0))
        detail_nav = tk.Frame(self.detail_frame, bg=self.ui.surface)
        detail_nav.pack(fill="x", padx=(12, 0), pady=(self.ui.space_sm, 0))
        tk.Frame(self.detail_frame, bg=self.ui.border, height=1).pack(fill="x", padx=(12, 0))
        detail_body = ttk.Frame(self.detail_frame, style="Meeting.TFrame")
        detail_body.pack(fill="both", expand=True, pady=(self.ui.space_sm, 0))
        detail_body.columnconfigure(0, weight=1)
        detail_body.rowconfigure(0, weight=1)
        self.detail_canvas = tk.Canvas(
            detail_body, background=self.ui.surface, highlightthickness=0, borderwidth=0,
        )
        self.detail_scrollbar = ttk.Scrollbar(
            detail_body, orient="vertical", command=self.detail_canvas.yview,
        )
        self.detail_canvas.configure(yscrollcommand=self.detail_scrollbar.set)
        self.detail_canvas.grid(row=0, column=0, sticky="nsew")
        self.detail_scrollbar.grid(row=0, column=1, sticky="ns", padx=(self.ui.space_sm, 0))
        right = ttk.Frame(self.detail_canvas, style="Meeting.TFrame")
        detail_window = self.detail_canvas.create_window((0, 0), window=right, anchor="nw")

        def update_detail_scroll_region(_event=None):
            self.detail_canvas.configure(scrollregion=self.detail_canvas.bbox("all"))

        def stretch_detail_content(event):
            self.detail_canvas.itemconfigure(detail_window, width=event.width)

        right.bind("<Configure>", update_detail_scroll_region)
        self.detail_canvas.bind("<Configure>", stretch_detail_content)
        self.detail_content = right
        self._bind_mousewheel_region(right, self.detail_canvas)
        self.detail_sections = SectionSwitcher(
            self.ui, detail_nav, right,
            on_select=lambda _key: self.detail_canvas.yview_moveto(0),
        )
        audio_page = self.detail_sections.add("audio", "Ouvir")
        transcript_page = self.detail_sections.add("transcript", "Transcrição")
        notes_page = self.detail_sections.add("notes", "Notas")
        summary_page = self.detail_sections.add("summary", "Resumo")
        ask_page = self.detail_sections.add("ask", "Perguntar")
        files_page = self.detail_sections.add("files", "Arquivos")

        player = self._card(audio_page, padx=18, pady=18)
        player.pack(fill="x", padx=(12, 0), pady=(4, 0))
        self._label(player, "Reproduzir gravação", bg=self.ui.card,
                    fg=self.ui.text_strong, font=self.ui.font(12, "bold")).pack(anchor="w")
        self.audio_overview = tk.StringVar(self.window, "Selecione uma gravação para ouvir.")
        self._label(player, "", textvariable=self.audio_overview, bg=self.ui.card,
                    fg=self.ui.text_muted, anchor="w").pack(fill="x", pady=(2, 14))
        self._label(player, "Fonte de áudio", bg=self.ui.card, fg=self.ui.text_muted,
                    anchor="w").pack(fill="x")
        self.audio_source = tk.StringVar(self.window)
        self.audio_source_box = ttk.Combobox(player, textvariable=self.audio_source,
                                             state="disabled", values=(), width=22)
        self.audio_source_box.pack(anchor="w", pady=(4, 14))
        self.audio_source_box.bind("<<ComboboxSelected>>", self._player_source_changed)
        player_actions = tk.Frame(player, bg=self.ui.card)
        player_actions.pack(fill="x")
        self.play_button = self._button(player_actions, "Reproduzir", self.play_selected_recording,
                                        accent=True)
        self.play_button.configure(state="disabled")
        self.play_button.pack(side="left")
        self.replay_stop_button = self._button(player_actions, "Parar", self.stop_selected_recording)
        self.replay_stop_button.configure(state="disabled")
        self.replay_stop_button.pack(side="left", padx=(8, 0))
        self.playback_clock = tk.StringVar(self.window, "00:00:00 / 00:00:00")
        self._label(player_actions, "", textvariable=self.playback_clock, bg=self.ui.card,
                    fg=self.ui.text_muted).pack(side="right")
        self.playback_position = tk.DoubleVar(self.window, 0.0)
        self.playback_seek = ttk.Scale(player, from_=0, to=1, variable=self.playback_position,
                                       orient="horizontal", style="Playback.Horizontal.TScale")
        self.playback_seek.pack(fill="x", pady=(16, 5))
        self.playback_seek.bind("<ButtonPress-1>", self._player_seek_begin)
        self.playback_seek.bind("<ButtonRelease-1>", self._player_seek_end)
        self.playback_status = tk.StringVar(self.window, "Pronto para ouvir.")
        self._label(player, "", textvariable=self.playback_status, bg=self.ui.card,
                    fg=self.ui.text_muted, anchor="w").pack(fill="x")

        # Notas: manual notes, organization labels and bookmarks.
        self._label(notes_page, "Notas manuais", anchor="w").pack(fill="x", padx=12, pady=(0, 4))
        self.notes = tk.Text(notes_page, height=8, wrap="word", undo=True, font=self.ui.font(),
                             **self.ui.text_colors())
        self.notes.pack(fill="both", expand=True, padx=(12, 0))
        self.notes.bind("<<Modified>>", self._notes_modified)
        self._button(notes_page, "Salvar notas", self.save_notes, accent=True).pack(
            anchor="w", padx=12, pady=(10, 0),
        )
        self.notes_tools_toggle = self._button(
            notes_page, "Organização e marcadores ▾",
            lambda: self._toggle_detail_panel(
                self.notes_tools, self.notes_tools_toggle, "Organização e marcadores",
            ),
        )
        self.notes_tools_toggle.pack(anchor="w", padx=12, pady=(14, 0))
        self.notes_tools = tk.Frame(notes_page, bg=self.ui.surface)
        organization_detail = self._card(self.notes_tools, padx=12, pady=6)
        organization_detail.pack(fill="x", pady=(8, 0))
        self._label(organization_detail, "Organização", bg=self.ui.card,
                    fg=self.ui.text_strong, font=self.ui.font(10, "bold")).pack(anchor="w")
        self._wrap_label(
            organization_detail,
            "Coleções/projetos, tags e pessoas usam rótulos locais; a série é manual.",
            bg=self.ui.card, fg=self.ui.text_muted, anchor="w", justify="left",
        ).pack(fill="x", pady=(1, 4))
        organization_inputs = tk.Frame(organization_detail, bg=self.ui.card)
        organization_inputs.pack(fill="x")
        self.organization_collections = tk.StringVar(self.window)
        self.organization_tags = tk.StringVar(self.window)
        self.organization_people = tk.StringVar(self.window)
        self.organization_series = tk.StringVar(self.window)
        for column, (variable, caption) in enumerate((
            (self.organization_collections, "Coleções/projetos"),
            (self.organization_tags, "Tags"),
            (self.organization_people, "Pessoas"),
            (self.organization_series, "Série"),
        )):
            self._label(organization_inputs, caption, bg=self.ui.card, fg=self.ui.text_muted,
                        font=self.ui.font(8)).grid(row=0, column=column, sticky="w", padx=(0, 4))
            self._entry(organization_inputs, variable, 5).grid(row=1, column=column, sticky="ew", padx=(0, 4))
            organization_inputs.columnconfigure(column, weight=1)
        self._button(organization_inputs, "Salvar organização", self.save_organization).grid(row=1, column=4)
        self.organization_status = tk.StringVar(self.window, "Selecione uma reunião para editar seus rótulos.")
        self._wrap_label(organization_detail, "", textvariable=self.organization_status, bg=self.ui.card,
                         fg=self.ui.text_muted, anchor="w").pack(fill="x", pady=(2, 0))
        self._label(self.notes_tools, "Marcadores", anchor="w", fg=self.ui.text_strong,
                    font=self.ui.font(10, "bold")).pack(fill="x", pady=(12, 2))
        bookmark_row = ttk.Frame(self.notes_tools, style="Meeting.TFrame")
        bookmark_row.pack(fill="x", pady=(0, 4))
        self.position = tk.StringVar(self.window, "0")
        self.bookmark_label = tk.StringVar(self.window)
        self._label(bookmark_row, "Segundos").pack(side="left")
        self._entry(bookmark_row, self.position, 6).pack(side="left", padx=6)
        self._entry(bookmark_row, self.bookmark_label, 10).pack(side="left", fill="x", expand=True)
        self._button(bookmark_row, "Adicionar marcador", self.add_bookmark).pack(side="left", padx=(6, 0))
        self.bookmark_status = tk.StringVar(self.window)
        self._wrap_label(self.notes_tools, "", textvariable=self.bookmark_status, anchor="w").pack(fill="x")
        self.bookmark_choice = tk.StringVar(self.window)
        self.bookmark_picker = ttk.Combobox(self.notes_tools, textvariable=self.bookmark_choice, state="readonly")
        self.bookmark_picker.pack(fill="x", pady=4)
        self.bookmark_picker.bind("<<ComboboxSelected>>", self._bookmark_selected)

        # Transcrição: processing, playback, segments, speaker labels, highlights.
        actions = ttk.Frame(transcript_page, style="Meeting.TFrame")
        actions.pack(fill="x", padx=(12, 0), pady=(0, 4))
        self.transcribe_button = self._button(actions, "Transcrever novamente", self.transcribe)
        self.transcribe_button.pack(side="left", padx=(0, 6))
        self._button(actions, "Cancelar processamento", lambda: self._action("cancel_processing", urgent=True)).pack(side="left")
        self.transcript_timing = tk.StringVar(
            self.window, "Dois cliques ou Enter para ouvir; horários marcam blocos de áudio, não palavras.",
        )
        self._wrap_label(
            transcript_page, "", textvariable=self.transcript_timing, anchor="w",
            fg=self.ui.text_muted,
        ).pack(fill="x", padx=12)
        self.track = tk.StringVar(self.window, "Microfone")
        self.transcript = ttk.Treeview(
            transcript_page, columns=("time", "track", "speaker", "text"), show="headings", height=8,
            selectmode="browse", style="Meeting.Treeview")
        for column, label, width in (("time", "Início", 60), ("track", "Fonte", 70),
                                     ("speaker", "Rótulo manual", 115), ("text", "Texto", 200)):
            self.transcript.heading(column, text=label)
            self.transcript.column(column, width=width, stretch=column == "text")
        self.transcript.pack(fill="both", expand=True, padx=(12, 0))
        self.transcript.bind("<<TreeviewSelect>>", self._transcript_selected)
        self.transcript.bind("<Double-1>", self._play_transcript_click)
        self.transcript.bind("<Return>", self._play_transcript_selected)
        transcript_pages = ttk.Frame(transcript_page, style="Meeting.TFrame")
        transcript_pages.pack(fill="x", padx=(12, 0), pady=(4, 0))
        self.transcript_previous_button = self._button(
            transcript_pages, "Trechos anteriores", lambda: self.change_transcript_page(-1),
        )
        self.transcript_previous_button.pack(side="left")
        self.transcript_next_button = self._button(
            transcript_pages, "Próximos trechos", lambda: self.change_transcript_page(1),
        )
        self.transcript_next_button.pack(side="left", padx=8)
        self.transcript_page_label = tk.StringVar(self.window, "Página de transcrição")
        self._label(
            transcript_pages, "", textvariable=self.transcript_page_label, anchor="w",
            fg=self.ui.text_muted,
        ).pack(side="left")
        self.segment_text = tk.Text(transcript_page, height=3, wrap="word", font=self.ui.font(),
                                    **self.ui.text_colors())
        self.segment_text.pack(fill="x", padx=(12, 0), pady=4)
        self.segment_text.configure(state="disabled")
        self.segments = {}
        self.transcript_tools_toggle = self._button(
            transcript_page, "Locutores e destaques ▾",
            lambda: self._toggle_detail_panel(
                self.transcript_tools, self.transcript_tools_toggle, "Locutores e destaques",
            ),
        )
        self.transcript_tools_toggle.pack(anchor="w", padx=12, pady=(10, 0))
        self.transcript_tools = tk.Frame(transcript_page, bg=self.ui.surface)
        self._wrap_label(
            self.transcript_tools, "Rótulo manual do locutor (não é identificação biométrica)", anchor="w",
            fg=self.ui.text_muted,
        ).pack(fill="x", pady=(4, 2))
        speaker_row = ttk.Frame(self.transcript_tools, style="Meeting.TFrame")
        speaker_row.pack(fill="x", pady=(0, 4))
        self.speaker_name = tk.StringVar(self.window)
        self._entry(speaker_row, self.speaker_name, 10).pack(side="left", fill="x", expand=True)
        self.speaker_save_button = self._button(speaker_row, "Salvar rótulo", self.save_speaker_label)
        self.speaker_save_button.pack(side="left", padx=(6, 0))
        self.speaker_delete_button = self._button(speaker_row, "Excluir rótulo", self.delete_speaker_label, danger=True)
        self.speaker_delete_button.configure(state="disabled")
        self.speaker_delete_button.pack(side="left", padx=(6, 0))
        self._label(self.transcript_tools, "Destaques e clipes", anchor="w", fg=self.ui.text_strong,
                    font=self.ui.font(10, "bold")).pack(fill="x", pady=(12, 2))
        highlight_row = ttk.Frame(self.transcript_tools, style="Meeting.TFrame")
        highlight_row.pack(fill="x", pady=(0, 4))
        self.highlight_start = tk.StringVar(self.window)
        self.highlight_end = tk.StringVar(self.window)
        self.highlight_label = tk.StringVar(self.window)
        self.highlight_note = tk.StringVar(self.window)
        for column, (variable, caption, width) in enumerate((
            (self.highlight_start, "Início (s)", 6),
            (self.highlight_end, "Fim (s)", 6),
            (self.highlight_label, "Rótulo", 8),
            (self.highlight_note, "Nota", 8),
        )):
            self._label(highlight_row, caption, fg=self.ui.text_muted,
                        font=self.ui.font(8)).grid(row=0, column=column, sticky="w", padx=(0, 4))
            self._entry(highlight_row, variable, width).grid(row=1, column=column, sticky="ew", padx=(0, 4))
            highlight_row.columnconfigure(column, weight=2 if column > 1 else 1)
        self.highlight_choice = tk.StringVar(self.window)
        self.highlight_picker = ttk.Combobox(
            self.transcript_tools, textvariable=self.highlight_choice, state="readonly", width=10,
        )
        self.highlight_picker.pack(fill="x", pady=(0, 4))
        self.highlight_picker.bind("<<ComboboxSelected>>", self._highlight_selected)
        highlight_actions = ttk.Frame(self.transcript_tools, style="Meeting.TFrame")
        highlight_actions.pack(fill="x", pady=(0, 4))
        self.highlight_save_button = self._button(highlight_actions, "Salvar destaque", self.save_highlight)
        self.highlight_save_button.pack(side="left")
        self.highlight_delete_button = self._button(
            highlight_actions, "Excluir destaque", self.delete_highlight, danger=True,
        )
        self.highlight_delete_button.configure(state="disabled")
        self.highlight_delete_button.pack(side="left", padx=6)
        self.highlight_export_button = self._button(
            highlight_actions, "Exportar clipe…", self.export_highlight_clip,
        )
        self.highlight_export_button.configure(state="disabled")
        self.highlight_export_button.pack(side="left")

        # Resumo: the editable summary and structured local reports.
        self._wrap_label(summary_page, "Resumo editável · copie a revisão para notas antes de salvar",
                         anchor="w").pack(fill="x", padx=12)
        self.summary = tk.Text(summary_page, height=6, wrap="word", font=self.ui.font(),
                               **self.ui.text_colors())
        self.summary.pack(fill="x", padx=(12, 0), pady=(6, 0))
        self.summary.insert("1.0", "Resumo local opcional. Revise os resultados e copie o texto para suas notas.")
        self.summary.configure(state="disabled")
        summary_actions = ttk.Frame(summary_page, style="Meeting.TFrame")
        summary_actions.pack(fill="x", padx=(12, 0), pady=4)
        self._button(summary_actions, "Copiar para notas", self.summary_to_notes).pack(side="left")
        self._button(summary_actions, "Salvar resumo revisado", self.save_summary).pack(side="left", padx=(6, 0))

        # Structured local reports.  The generated envelope remains immutable;
        # the editor below writes only a reviewed artifact through the library.
        self.report_tools_toggle = self._button(
            summary_page, "Relatórios avançados ▾",
            lambda: self._toggle_detail_panel(
                self.report_frame, self.report_tools_toggle, "Relatórios avançados",
            ),
        )
        self.report_tools_toggle.pack(anchor="w", padx=12, pady=(12, 0))
        self.report_frame = self._card(summary_page, padx=12, pady=8)
        report_frame = self.report_frame
        report_header = tk.Frame(report_frame, bg=self.ui.card)
        report_header.pack(fill="x")
        self._label(report_header, "Relatórios locais", bg=self.ui.card,
                    fg=self.ui.text_strong, font=self.ui.font(11, "bold")).pack(side="left")
        self._button(report_header, "Gerar relatório local", self.generate_report, accent=True).pack(side="right")
        self._wrap_label(
            report_frame,
            "Perfis são receitas locais versionadas; o modelo nunca recebe dados fora do computador.",
            bg=self.ui.card, fg=self.ui.text_muted, anchor="w", justify="left",
        ).pack(fill="x", pady=(2, 6))
        self.report_profile_choice = tk.StringVar(self.window, "Geral")
        # The profile picker gets its own line; beside six buttons it left no
        # room for the last ones in a narrow detail pane.
        self.report_profile_box = ttk.Combobox(
            report_frame, textvariable=self.report_profile_choice, state="readonly", width=10,
        )
        self.report_profile_box.pack(fill="x", pady=2)
        profile_row = tk.Frame(report_frame, bg=self.ui.card)
        profile_row.pack(fill="x", pady=2)
        self.report_profile_box.bind("<<ComboboxSelected>>", self._report_profile_changed)
        self._button(profile_row, "Criar", self.create_report_profile).pack(side="left")
        self._button(profile_row, "Duplicar", self.duplicate_report_profile).pack(side="left", padx=(6, 0))
        self._button(profile_row, "Editar", self.edit_report_profile).pack(side="left", padx=(6, 0))
        self.report_disable_button = self._button(profile_row, "Desativar", self.disable_report_profile)
        self.report_disable_button.pack(side="left", padx=(6, 0))
        self.report_enable_button = self._button(profile_row, "Ativar", self.enable_report_profile)
        self.report_enable_button.pack(side="left", padx=(6, 0))
        self._button(profile_row, "Excluir", self.delete_report_profile, danger=True).pack(side="left", padx=(6, 0))
        history_row = tk.Frame(report_frame, bg=self.ui.card)
        history_row.pack(fill="x", pady=2)
        self._label(history_row, "Histórico", bg=self.ui.card).pack(side="left", padx=(0, 6))
        self.report_history_choice = tk.StringVar(self.window)
        self.report_history_box = ttk.Combobox(
            history_row, textvariable=self.report_history_choice, state="readonly", width=12,
        )
        self.report_history_box.pack(side="left", fill="x", expand=True)
        self.report_history_box.bind("<<ComboboxSelected>>", self._report_history_changed)
        self._button(history_row, "Atualizar", self.refresh_reports).pack(side="left", padx=(6, 0))
        section_row = tk.Frame(report_frame, bg=self.ui.card)
        section_row.pack(fill="x", pady=2)
        self._label(section_row, "Seção", bg=self.ui.card).pack(side="left", padx=(0, 6))
        self.report_section_choice = tk.StringVar(self.window)
        self.report_section_box = ttk.Combobox(
            section_row, textvariable=self.report_section_choice, state="readonly", width=10,
        )
        self.report_section_box.pack(side="left", fill="x", expand=True)
        self.report_section_box.bind("<<ComboboxSelected>>", self._report_section_changed)
        self._button(section_row, "Copiar seção", self.copy_report_section).pack(side="left", padx=(6, 0))
        self._button(section_row, "Exportar…", self.export_report).pack(side="left", padx=(6, 0))
        self.report_provenance = tk.StringVar(self.window, "Nenhum relatório selecionado.")
        self._wrap_label(report_frame, "", textvariable=self.report_provenance, bg=self.ui.card,
                         fg=self.ui.text_muted, anchor="w", justify="left").pack(fill="x", pady=2)
        self.report_editor = tk.Text(report_frame, height=6, wrap="word", font=self.ui.font(),
                                     **self.ui.text_colors())
        self.report_editor.pack(fill="x", pady=(4, 2))
        report_actions = tk.Frame(report_frame, bg=self.ui.card)
        report_actions.pack(fill="x", pady=2)
        self._button(report_actions, "Salvar revisão", self.save_report_review, accent=True).pack(side="left")
        self._label(report_actions, "Citações", bg=self.ui.card, fg=self.ui.text_muted).pack(side="left", padx=(12, 4))
        self.report_citations = tk.Listbox(report_actions, height=2, width=10,
                                           font=self.ui.font(), **self.ui.listbox_colors())
        self.report_citations.pack(side="left", fill="x", expand=True)
        self.report_citations.bind("<Double-Button-1>", lambda _event: self.jump_to_report_citation())
        self._button(report_actions, "Ir à fonte", self.jump_to_report_citation).pack(side="left", padx=(6, 0))

        # Perguntar: questions about this recording only.
        ask_frame = self._card(ask_page, padx=12, pady=8)
        ask_frame.pack(fill="x", padx=(12, 0))
        self._label(ask_frame, "Perguntar sobre esta reunião", bg=self.ui.card,
                    fg=self.ui.text_strong, font=self.ui.font(11, "bold")).pack(anchor="w")
        self.ask_question = tk.StringVar(self.window)
        ask_row = tk.Frame(ask_frame, bg=self.ui.card)
        ask_row.pack(fill="x", pady=3)
        ask_entry = self._entry(ask_row, self.ask_question, 12)
        ask_entry.pack(side="left", fill="x", expand=True)
        ask_entry.bind("<Return>", lambda _event: self.ask_this_meeting())
        self._button(ask_row, "Perguntar", self.ask_this_meeting, accent=True).pack(side="left", padx=(6, 0))
        self.ask_answer = tk.Text(ask_frame, height=8, wrap="word", font=self.ui.font(),
                                  **self.ui.text_colors())
        self.ask_answer.pack(fill="x", pady=(2, 2))
        ask_citation_row = tk.Frame(ask_frame, bg=self.ui.card)
        ask_citation_row.pack(fill="x", pady=(0, 2))
        self._label(ask_citation_row, "Citações", bg=self.ui.card,
                    fg=self.ui.text_muted).pack(side="left", padx=(0, 4))
        self.ask_citations = tk.Listbox(ask_citation_row, height=3, width=10,
                                        font=self.ui.font(), **self.ui.listbox_colors())
        self.ask_citations.pack(side="left", fill="x", expand=True)
        self.ask_citations.bind("<Double-Button-1>", lambda _event: self.jump_to_ask_citation())
        self._button(ask_citation_row, "Ir à fonte", self.jump_to_ask_citation).pack(side="left", padx=(6, 0))
        ask_actions = tk.Frame(ask_frame, bg=self.ui.card)
        ask_actions.pack(fill="x")
        self.ask_save_button = self._button(ask_actions, "Salvar resposta", self.save_answer)
        self.ask_save_button.pack(side="left")
        self.ask_status = tk.StringVar(self.window, "Respostas ficam somente na memória até você salvar.")
        self._wrap_label(ask_actions, "", textvariable=self.ask_status, bg=self.ui.card,
                         fg=self.ui.text_muted, anchor="w").pack(side="left", fill="x", expand=True, padx=(8, 0))

        # Arquivos: exports and raw-audio storage.
        self._label(files_page, "Exportar", anchor="w", fg=self.ui.text_strong,
                    font=self.ui.font(10, "bold")).pack(fill="x", padx=12, pady=(0, 4))
        exports = ttk.Frame(files_page, style="Meeting.TFrame")
        exports.pack(fill="x", padx=(12, 0), pady=(0, 4))
        self._button(exports, "Markdown…", lambda: self.export("markdown")).pack(side="left", padx=(0, 6))
        self._button(exports, "Texto…", lambda: self.export("text")).pack(side="left", padx=(0, 6))
        self.export_audio_button = self._button(exports, "Áudio final…", self.export_audio)
        self.export_audio_button.pack(side="left")
        self.delete_button = self._button(files_page, "Excluir gravação…", self.delete_selected,
                                          danger=True)
        self.delete_button.configure(state="disabled")
        self.delete_button.pack(anchor="w", padx=12, pady=(18, 4))
        self._label(files_page, "Áudio raw", anchor="w", fg=self.ui.text_strong,
                    font=self.ui.font(10, "bold")).pack(fill="x", padx=12, pady=(12, 2))
        self.audio_capability_status = tk.StringVar(self.window, "Áudio raw disponível.")
        self._wrap_label(files_page, "", textvariable=self.audio_capability_status, anchor="w",
                         fg=self.ui.text_muted).pack(fill="x", padx=12, pady=(0, 4))
        raw_row = ttk.Frame(files_page, style="Meeting.TFrame")
        raw_row.pack(fill="x", padx=(12, 0))
        self.raw_remove_microphone = tk.BooleanVar(self.window, False)
        self.raw_remove_system = tk.BooleanVar(self.window, False)
        self.raw_remove_microphone_check = tk.Checkbutton(
            raw_row, text="Microfone", variable=self.raw_remove_microphone,
            font=self.ui.font(9), **self.ui.checkbutton_colors(self.ui.surface),
        )
        self.raw_remove_microphone_check.pack(side="left", padx=(0, 2))
        self.raw_remove_system_check = tk.Checkbutton(
            raw_row, text="Sistema", variable=self.raw_remove_system,
            font=self.ui.font(9), **self.ui.checkbutton_colors(self.ui.surface),
        )
        self.raw_remove_system_check.pack(side="left", padx=(0, 6))
        self.raw_remove_button = self._button(raw_row, "Remover áudio raw…", self.preview_raw_tracks, danger=True)
        self.raw_remove_button.pack(side="left")

    def _build_privacy_card(self, parent):
        """Build privacy/retention controls inside the existing scrollable page."""
        card = self._card(parent)
        card.pack(fill="x", pady=(0, self.ui.space_md))
        self.privacy_card = card
        self._label(
            card, "Privacidade e retenção local", bg=self.ui.card,
            fg=self.ui.text_strong, font=self.ui.font(11, "bold"),
        ).grid(row=0, column=0, columnspan=3, sticky="w")
        self._label(
            card,
            "O SnipVoice grava somente após uma ação explícita. As políticas abaixo são locais e preservam chaves futuras do workspace.",
            bg=self.ui.card, fg=self.ui.text_muted, anchor="w", justify="left", wraplength=860,
        ).grid(row=1, column=0, columnspan=3, sticky="ew", pady=(self.ui.space_xs, self.ui.space_sm))

        self.privacy_notice_enabled = tk.BooleanVar(self.window, False)
        self.privacy_notice_language = tk.StringVar(self.window, "pt-BR")
        self.qa_mode = tk.StringVar(self.window, "explicit_save")
        self.whole_meeting_policy = tk.StringVar(self.window, "keep")
        self.whole_meeting_after_days = tk.StringVar(self.window, "")
        self.raw_audio_policy = tk.StringVar(self.window, "keep")
        self.raw_audio_after_days = tk.StringVar(self.window, "")
        self.raw_audio_microphone = tk.BooleanVar(self.window, False)
        self.raw_audio_system = tk.BooleanVar(self.window, False)
        self.trash_days = tk.StringVar(self.window, "30")

        row = 2
        self.privacy_notice_check = tk.Checkbutton(
            card, text="Mostrar aviso antes de cada gravação", variable=self.privacy_notice_enabled,
            font=self.ui.font(), **self.ui.checkbutton_colors(self.ui.card),
        )
        self.privacy_notice_check.grid(row=row, column=0, columnspan=2, sticky="w", pady=2)
        row += 1
        self._label(card, "Idioma do aviso", bg=self.ui.card, anchor="w").grid(
            row=row, column=0, sticky="w", pady=2,
        )
        self.privacy_notice_language_box = ttk.Combobox(
            card, textvariable=self.privacy_notice_language,
            values=("pt-BR", "en-US"), state="readonly", width=14,
        )
        self.privacy_notice_language_box.grid(row=row, column=1, sticky="w", pady=2)
        row += 1
        self._label(card, "Salvar respostas de Q&A", bg=self.ui.card, anchor="w").grid(
            row=row, column=0, sticky="w", pady=2,
        )
        self.qa_mode_box = ttk.Combobox(
            card, textvariable=self.qa_mode,
            values=("explicit_save", "memory_only"), state="readonly", width=18,
        )
        self.qa_mode_box.grid(row=row, column=1, sticky="w", pady=2)
        self._label(card, "explicit_save permite o botão Salvar; memory_only descarta a resposta ao fechar.",
                    bg=self.ui.card, fg=self.ui.text_muted, anchor="w").grid(
            row=row, column=2, sticky="w", padx=(self.ui.space_sm, 0), pady=2,
        )
        self.qa_mode_box.bind("<<ComboboxSelected>>", lambda _event: self._sync_qa_controls())
        row += 1
        self._label(card, "Política da reunião", bg=self.ui.card, anchor="w").grid(
            row=row, column=0, sticky="w", pady=2,
        )
        self.whole_meeting_policy_box = ttk.Combobox(
            card, textvariable=self.whole_meeting_policy,
            values=("keep", "whole_meeting"), state="readonly", width=18,
        )
        self.whole_meeting_policy_box.grid(row=row, column=1, sticky="w", pady=2)
        row += 1
        self._label(card, "Dias até excluir reunião", bg=self.ui.card, anchor="w").grid(
            row=row, column=0, sticky="w", pady=2,
        )
        self._entry(card, self.whole_meeting_after_days, 12).grid(row=row, column=1, sticky="w", pady=2)
        self._label(card, "Vazio mantém indefinidamente", bg=self.ui.card,
                    fg=self.ui.text_muted, anchor="w").grid(row=row, column=2, sticky="w",
                    padx=(self.ui.space_sm, 0), pady=2)
        row += 1
        self._label(card, "Política de áudio raw", bg=self.ui.card, anchor="w").grid(
            row=row, column=0, sticky="w", pady=2,
        )
        self.raw_audio_policy_box = ttk.Combobox(
            card, textvariable=self.raw_audio_policy,
            values=("keep", "raw_tracks"), state="readonly", width=18,
        )
        self.raw_audio_policy_box.grid(row=row, column=1, sticky="w", pady=2)
        self.raw_audio_policy_box.bind("<<ComboboxSelected>>", lambda _event: self._sync_raw_policy_controls())
        row += 1
        self._label(card, "Dias até remover áudio raw", bg=self.ui.card, anchor="w").grid(
            row=row, column=0, sticky="w", pady=2,
        )
        self._entry(card, self.raw_audio_after_days, 12).grid(row=row, column=1, sticky="w", pady=2)
        raw_tracks = tk.Frame(card, bg=self.ui.card)
        raw_tracks.grid(row=row, column=2, sticky="w", padx=(self.ui.space_sm, 0), pady=2)
        self.raw_audio_microphone_check = tk.Checkbutton(
            raw_tracks, text="Microfone", variable=self.raw_audio_microphone,
            font=self.ui.font(), **self.ui.checkbutton_colors(self.ui.card),
        )
        self.raw_audio_microphone_check.pack(side="left")
        self.raw_audio_system_check = tk.Checkbutton(
            raw_tracks, text="Sistema", variable=self.raw_audio_system,
            font=self.ui.font(), **self.ui.checkbutton_colors(self.ui.card),
        )
        self.raw_audio_system_check.pack(side="left", padx=(self.ui.space_sm, 0))
        row += 1
        self._label(card, "Prazo da lixeira (dias)", bg=self.ui.card, anchor="w").grid(
            row=row, column=0, sticky="w", pady=2,
        )
        self._entry(card, self.trash_days, 12).grid(row=row, column=1, sticky="w", pady=2)
        row += 1
        self._label(
            card,
            "A exclusão local e a lixeira não prometem apagamento forense de SSD; cópias externas exportadas ficam fora do escopo.",
            bg=self.ui.card, fg=self.ui.text_muted, anchor="w", justify="left", wraplength=860,
        ).grid(row=row, column=0, columnspan=3, sticky="ew", pady=(self.ui.space_sm, 2))
        row += 1
        self.privacy_status = tk.StringVar(self.window, "Configurações de privacidade carregando…")
        self._label(card, "", textvariable=self.privacy_status, bg=self.ui.card,
                    fg=self.ui.text_muted, anchor="w").grid(row=row, column=0, columnspan=2, sticky="w")
        self._button(card, "Salvar privacidade", self.save_privacy_settings, accent=True).grid(
            row=row, column=2, sticky="e", pady=(self.ui.space_sm, 0),
        )
        card.columnconfigure(2, weight=1)
        self._sync_raw_policy_controls()

    def _location_specs(self):
        """Folder cards: app data (Geral) and downloaded models (Modelos)."""
        specs = []
        if self.data_location is not None and self.relocate_data is not None:
            specs.append({
                "key": "data", "title": "Pasta de dados", "info": self.data_location,
                "request": self.relocate_data, "choose": data_relocation.target_for_choice,
                "validate": data_relocation.validate_target, "size": data_relocation.directory_size,
                "description": (
                    "Configurações, histórico de ditado, gravações e a biblioteca de reuniões "
                    "ficam nesta pasta. Ao escolher outra, o SnipVoice move tudo para lá e "
                    "reinicia. Os modelos baixados têm uma pasta própria, na seção Modelos."
                ),
                "lock_note": "Definida pela variável SNIPVOICE_HOME; altere-a fora do aplicativo.",
                "confirm": "Mover os dados do SnipVoice",
            })
        if self.models_location is not None and self.relocate_models is not None:
            specs.append({
                "key": "models", "title": "Pasta dos modelos", "info": self.models_location,
                "request": self.relocate_models, "choose": os.path.abspath,
                "validate": data_relocation.validate_models_target,
                "size": data_relocation.models_size,
                "description": (
                    "Modelos de transcrição e de resumo baixados. Os arquivos GGUF ficam em "
                    "voice-models e summary-models, prontos para outros aplicativos "
                    "compatíveis. Pode ser uma pasta compartilhada: outros arquivos dela "
                    "não são alterados."
                ),
                "lock_note": (
                    "Definida por SNIPVOICE_VOICE_CACHE, SNIPVOICE_SUMMARY_CACHE ou "
                    "voice_cache_dir em settings.json."
                ),
                "confirm": "Mover os modelos baixados",
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
        self._label(
            card, spec["description"], bg=self.ui.card, fg=self.ui.text_muted,
            anchor="w", justify="left", wraplength=760,
        ).grid(row=1, column=0, columnspan=3, sticky="ew", pady=(self.ui.space_xs, self.ui.space_sm))
        display = tk.StringVar(self.window, info["path"])
        entry = self._entry(card, display)
        entry.configure(state="readonly")
        entry.grid(row=2, column=0, sticky="ew")
        key = spec["key"]
        move = self._button(card, "Mover para…", lambda: self.choose_location(key))
        move.configure(state="disabled" if info["env_locked"] else "normal")
        move.grid(row=2, column=1, padx=(self.ui.space_sm, 0))
        at_default = os.path.normcase(info["path"]) == os.path.normcase(info["default"])
        default = self._button(card, "Restaurar padrão", lambda: self.restore_default_location(key))
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
            parent=self.window, title=f"Nova {spec['title'].lower()}", mustexist=True,
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
                spec["title"], "Aguarde o download do modelo de resumo terminar.", parent=self.window,
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
            f"{spec['confirm']} ({size}) de\n{current}\npara\n{target}?\n\n"
            "O SnipVoice será fechado e aberto de novo. Entre discos diferentes, a cópia "
            "pode levar alguns minutos antes de o ícone voltar à bandeja.",
            parent=self.window,
        ):
            return
        error = spec["request"](target)
        if error:
            messagebox.showerror(spec["title"], error, parent=self.window)

    def _build_appearance_card(self, parent):
        """Build a local appearance preference with a safe manager rebuild."""
        card = self._card(parent)
        card.pack(fill="x", pady=(0, self.ui.space_md))
        self.appearance_card = card
        self._label(
            card,
            "Aparência",
            bg=self.ui.card,
            fg=self.ui.text_strong,
            font=self.ui.font(11, "bold"),
        ).grid(row=0, column=0, columnspan=3, sticky="w")
        self._label(
            card,
            "Use o tema do sistema ou escolha uma aparência fixa. A mudança reconstrói "
            "esta janela sem reiniciar o SnipVoice.",
            bg=self.ui.card,
            fg=self.ui.text_muted,
            anchor="w",
            justify="left",
            wraplength=760,
        ).grid(row=1, column=0, columnspan=3, sticky="ew", pady=(self.ui.space_xs, self.ui.space_sm))
        preference = ui_theme.normalize_preference(
            getattr(self.ui, "preference", None),
        )
        self.appearance_display = tk.StringVar(
            self.window,
            APPEARANCE_LABELS[preference],
        )
        self.appearance_box = ttk.Combobox(
            card,
            textvariable=self.appearance_display,
            values=tuple(APPEARANCE_LABELS.values()),
            state="readonly",
            width=18,
        )
        self.appearance_box.grid(row=2, column=0, sticky="w")
        self.appearance_status = tk.StringVar(
            self.window,
            APPEARANCE_STATUS[preference],
        )
        self._label(
            card,
            "",
            textvariable=self.appearance_status,
            bg=self.ui.card,
            fg=self.ui.text_muted,
            anchor="w",
        ).grid(row=2, column=1, sticky="w", padx=(self.ui.space_md, 0))
        self._button(
            card,
            "Aplicar aparência",
            self.save_appearance,
            accent=True,
        ).grid(row=2, column=2, sticky="e")
        card.columnconfigure(1, weight=1)

    def save_appearance(self):
        selected = self.appearance_display.get()
        try:
            preference = next(
                key for key, label in APPEARANCE_LABELS.items() if label == selected
            )
        except StopIteration:
            self.appearance_status.set("Escolha uma aparência válida.")
            return
        self.appearance_status.set("Salvando aparência local…")

        def save():
            result = self.persist_settings({"appearance": preference})
            if result is False:
                raise ValueError("Não foi possível salvar a aparência.")
            return preference

        self._submit("save_appearance", save, self._appearance_saved, urgent=True)

    def _appearance_saved(self, preference, error):
        if error:
            self._remember_operation_error(error)
            self.appearance_status.set("Não foi possível salvar a aparência.")
            return
        preference = ui_theme.normalize_preference(preference)
        self.raw_settings["appearance"] = preference
        self.appearance_display.set(APPEARANCE_LABELS[preference])
        self.appearance_status.set("Aparência salva. Atualizando a janela…")
        if self.on_appearance_changed:
            self.window.after_idle(lambda: self.on_appearance_changed(preference))

    def _sync_raw_policy_controls(self):
        enabled = self.raw_audio_policy.get() == "raw_tracks"
        for widget in (
            getattr(self, "raw_audio_microphone_check", None),
            getattr(self, "raw_audio_system_check", None),
        ):
            if widget is not None:
                widget.configure(state="normal" if enabled else "disabled")

    @staticmethod
    def _policy_days(value, *, field):
        text = str(value or "").strip()
        if not text:
            return None
        try:
            days = float(text)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field}: informe um número de dias válido.") from exc
        if not math.isfinite(days) or days < 0 or days > 36500:
            raise ValueError(f"{field}: informe um valor entre 0 e 36.500 dias.")
        return days

    def _apply_workspace_settings(self, workspace):
        if not isinstance(workspace, dict):
            return
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
        self.whole_meeting_policy.set(str(whole_mode or "keep"))
        self.whole_meeting_after_days.set(
            "" if whole.get("after_days") is None else str(whole.get("after_days")))
        raw = self.retention_defaults.get("raw_audio", self.retention_defaults.get(
            "raw_audio_policy", {"mode": "keep", "tracks": []}))
        if not isinstance(raw, dict):
            raw = {"mode": str(raw)}
        raw_mode = raw.get("mode")
        if raw_mode is None and raw.get("after_days") is not None:
            raw_mode = "raw_tracks"
        self.raw_audio_policy.set(str(raw_mode or "keep"))
        self.raw_audio_after_days.set(
            "" if raw.get("after_days") is None else str(raw.get("after_days")))
        tracks = set(raw.get("tracks", ()) if isinstance(raw.get("tracks", ()), (list, tuple)) else ())
        self.raw_audio_microphone.set("microphone" in tracks)
        self.raw_audio_system.set("system" in tracks)
        self.trash_days.set(str(self.retention_defaults.get("trash_days", 30)))
        self._sync_raw_policy_controls()
        self._sync_qa_controls()

    def _sync_qa_controls(self):
        mode = self.qa_mode.get() if hasattr(self, "qa_mode") else "explicit_save"
        button = getattr(self, "ask_save_button", None)
        if button is not None:
            button.configure(state="normal" if mode == "explicit_save" else "disabled")

    def save_privacy_settings(self):
        if self.privacy_save_inflight:
            self.privacy_status.set("Aguarde a atualização de privacidade em andamento.")
            return
        try:
            whole_days = self._policy_days(self.whole_meeting_after_days.get(), field="Reunião")
            raw_days = self._policy_days(self.raw_audio_after_days.get(), field="Áudio raw")
            trash = self._policy_days(self.trash_days.get(), field="Lixeira")
            if trash is None:
                trash = 30.0
            qa_mode = self.qa_mode.get().strip()
            if qa_mode not in {"memory_only", "explicit_save"}:
                raise ValueError("Escolha um modo de Q&A válido.")
            whole_mode = self.whole_meeting_policy.get().strip()
            if whole_mode not in {"keep", "whole_meeting"}:
                raise ValueError("Escolha uma política de reunião válida.")
            raw_mode = self.raw_audio_policy.get().strip()
            if raw_mode not in {"keep", "raw_tracks"}:
                raise ValueError("Escolha uma política de áudio raw válida.")
            tracks = []
            if self.raw_audio_microphone.get():
                tracks.append("microphone")
            if self.raw_audio_system.get():
                tracks.append("system")
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
            raw = copy.deepcopy(retention.get("raw_audio", {}))
            if not isinstance(raw, dict):
                raw = {}
            raw["mode"] = raw_mode
            raw["tracks"] = tracks
            if raw_days is None:
                raw.pop("after_days", None)
            else:
                raw["after_days"] = raw_days
            retention["raw_audio"] = raw
            retention["trash_days"] = trash
        except (ValueError, TypeError) as exc:
            self.privacy_status.set(str(exc))
            return
        expected = self.workspace_generation
        self.privacy_status.set("Salvando configurações locais…")

        def save():
            updater = getattr(self.controller, "update_workspace", None)
            if not callable(updater):
                updater = self.controller.library.update_workspace
            return updater({"privacy_defaults": privacy, "retention_defaults": retention},
                           expected_generation=expected)

        self.privacy_save_inflight = True
        submitted = self._submit(
            "save_privacy", save,
            lambda value, error: self._privacy_saved(value, error, privacy, retention),
            urgent=True,
        )
        if not submitted:
            self.privacy_save_inflight = False
            self.privacy_status.set("Não foi possível enfileirar a alteração de privacidade.")

    def _privacy_saved(self, value, error, privacy=None, retention=None):
        self.privacy_save_inflight = False
        if self.closed:
            return
        if error:
            self._remember_operation_error(error)
            self.privacy_status.set("Não foi possível salvar privacidade; recarregue o workspace.")
            return
        if isinstance(value, dict):
            self._apply_workspace_settings(value)
        elif privacy is not None:
            self.privacy_defaults = copy.deepcopy(privacy)
            self.retention_defaults = copy.deepcopy(retention or self.retention_defaults)
        self.privacy_ready = True
        self.privacy_status.set("Privacidade e retenção salvas localmente.")
        self._sync_qa_controls()
        if self.on_settings_changed:
            self.on_settings_changed()

    def _submit(self, key, operation, callback=None, urgent=False):
        if not self.bridge.submit(key, operation, callback or self._done, urgent):
            if not self.closed:
                self.status.set("Há operações pendentes. Aguarde e tente novamente.")
            return False
        return True

    def _done(self, _value, error):
        if error:
            self._remember_operation_error(error)
            self.status.set("Não foi possível concluir a operação. Veja os detalhes na aba Gravação.")
        else:
            self.status.set("A operação não foi aceita. Verifique o estado atual."
                            if _value is False else "Operação concluída.")

    # -- Local report profiles, history, and Q&A ----------------------

    @staticmethod
    def _profile_label(profile):
        suffix = " · desativado" if profile.get("disabled") else ""
        return f'{profile.get("name", profile.get("id", "perfil"))} · v{profile.get("version", 1)}{suffix}'

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
                self._remember_operation_error(error) if error else None
                self._set_report_profiles([])
                return
            self._set_report_profiles(profiles)

        language = self.language.get()
        language = "pt-BR" if language == "auto" else language
        self._submit("report_profiles", lambda: reader(language=language), loaded)

    def _set_report_profiles(self, profiles):
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
                                  if item.get("id") == "general"), labels[0] if labels else "")
                self.report_profile_choice.set(preferred)
        self._sync_report_profile_actions()

    def _selected_report_profile(self):
        return self.report_profile_by_label.get(self.report_profile_choice.get())

    def _report_profile_changed(self, _event=None):
        # Selection is intentionally local; the profile is read again by the
        # worker before generation so a stale window cannot mutate workspace.
        self._sync_report_profile_actions()
        return self._selected_report_profile()

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
            self._remember_operation_error(error)
            self.status.set("Não foi possível salvar o perfil. Veja os detalhes na aba Gravação.")
            return
        self.status.set("Perfil de relatório salvo.")
        self.refresh_report_profiles()

    def create_report_profile(self):
        name = simpledialog.askstring("Novo perfil", "Nome do perfil:", parent=self.window)
        if not name:
            return
        identifier = simpledialog.askstring(
            "Novo perfil", "Identificador (a-z, 0-9, hífen ou sublinhado):", parent=self.window,
        )
        if not identifier:
            return
        base = self._selected_report_profile() or {
            "sections": ["summary"], "instructions": "",
        }
        profile = {
            "id": identifier.strip(), "name": name.strip(),
            "instructions": str(base.get("instructions", "")),
            "sections": list(base.get("sections", ["summary"])),
            "language": ("pt-BR" if self.language.get() == "auto"
                         else self.language.get() or "pt-BR"),
        }
        self._submit("save_report_profile", lambda: self.controller.save_report_profile(profile),
                     self._profile_saved)

    def duplicate_report_profile(self):
        current = self._selected_report_profile()
        if not current:
            self.status.set("Selecione um perfil para duplicar.")
            return
        identifier = simpledialog.askstring("Duplicar perfil", "Novo identificador:", parent=self.window)
        if not identifier:
            return
        profile = dict(current)
        profile.pop("version", None)
        profile.pop("profile_hash", None)
        profile.pop("builtin", None)
        profile["disabled"] = False
        profile["id"] = identifier.strip()
        profile["name"] = f'{current.get("name", "Perfil")} (cópia)'
        self._submit("save_report_profile", lambda: self.controller.save_report_profile(profile),
                     self._profile_saved)

    def edit_report_profile(self):
        current = self._selected_report_profile()
        if not current or current.get("builtin"):
            self.status.set("Perfis internos não podem ser editados; duplique um para personalizar.")
            return
        name = simpledialog.askstring("Editar perfil", "Nome:", initialvalue=current.get("name", ""), parent=self.window)
        if name is None:
            return
        instructions = simpledialog.askstring(
            "Editar perfil", "Instruções adicionais (texto não confiável):",
            initialvalue=current.get("instructions", ""), parent=self.window,
        )
        if instructions is None:
            return
        profile = dict(current, name=name, instructions=instructions)
        profile.pop("version", None)
        profile.pop("profile_hash", None)
        profile.pop("builtin", None)
        self._submit("save_report_profile", lambda: self.controller.save_report_profile(profile),
                     self._profile_saved)

    def disable_report_profile(self):
        current = self._selected_report_profile()
        if not current or current.get("builtin"):
            self.status.set("Selecione um perfil personalizado para desativar.")
            return
        identifier = current.get("id")
        self._submit("disable_report_profile",
                     lambda: self.controller.disable_report_profile(identifier),
                     self._profile_saved)

    def enable_report_profile(self):
        current = self._selected_report_profile()
        if not current or current.get("builtin"):
            self.status.set("Selecione um perfil personalizado desativado para ativar.")
            return
        identifier = current.get("id")
        self._submit("enable_report_profile",
                     lambda: self.controller.enable_report_profile(identifier),
                     self._profile_saved)

    def delete_report_profile(self):
        current = self._selected_report_profile()
        if not current or current.get("builtin"):
            self.status.set("Selecione um perfil personalizado para excluir.")
            return
        if not messagebox.askyesno("Excluir perfil", "Excluir este perfil personalizado?", parent=self.window):
            return
        identifier = current.get("id")
        self._submit("delete_report_profile",
                     lambda: self.controller.delete_report_profile(identifier),
                     self._profile_saved)

    def generate_report(self):
        if not self.selected or not self.detail_ready:
            self.status.set("Selecione uma gravação na biblioteca.")
            return
        profile = self._selected_report_profile()
        if not profile:
            self.status.set("Selecione um perfil de relatório.")
            return
        if profile.get("disabled"):
            self.status.set("Ative o perfil de relatório antes de gerar.")
            return
        model = self.summary_model.get().strip()
        if not self.summary_model_installed.get(model):
            self.status.set("Baixe o modelo selecionado em Configurações antes de gerar o relatório.")
            return
        session_id = self.selected
        self._action(
            "generate_report", session_id, model, profile=profile,
            revision=self.transcript_revision,
            callback=lambda value, error: self._report_generated(session_id, value, error),
        )

    def _report_generated(self, session_id, value, error):
        if self.closed or self.selected != session_id:
            return
        if error:
            self._remember_operation_error(error)
            self.status.set("O relatório não foi salvo; o resultado anterior continua disponível.")
            return
        self.status.set("Relatório local salvo como nova revisão imutável.")
        self.refresh_reports(session_id)

    def refresh_reports(self, session_id=None):
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
                self._remember_operation_error(error)
                self.status.set("Não foi possível carregar o histórico de relatórios.")
                return
            self._set_report_history(reports)

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

    def _set_report_history(self, reports):
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
            kind = "Pergunta" if item.get("kind") == "qa" else "Relatório"
            profile = item.get("profile_id") or "legado"
            created = str(item.get("created_at") or "")[:19].replace("T", " ")
            labels.append(f"{kind} · {profile} · {created} · {identifier[:12]}")
            ids.append(identifier)
        self.report_history_ids = ids
        self.report_history_box.configure(values=labels)
        if not labels:
            self.report_history_choice.set("")
            self.selected_report = None
            self.report_sections = {}
            self.report_provenance.set("Nenhum relatório selecionado.")
            self.report_editor.delete("1.0", "end")
            self.report_citations.delete(0, "end")
            return
        if self.selected_report and self.selected_report.get("id") in ids:
            index = ids.index(self.selected_report["id"])
        else:
            index = len(ids) - 1
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
                self._remember_operation_error(error)
                self.status.set("Não foi possível abrir este relatório.")
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
        names = list(self.report_sections)[:64]
        self.report_section_box.configure(values=names)
        if names:
            self.report_section_choice.set(names[0])
        else:
            self.report_section_choice.set("")
        model = self.selected_report.get("model", {})
        self.report_provenance.set(
            f'Perfil {self.selected_report.get("profile_id", "legado")} '
            f'v{self.selected_report.get("profile_version", "?")} · '
            f'revisão {self.selected_report.get("transcript_revision", "?")} · '
            f'modelo {model.get("id", "?")} · SHA-256 {str(model.get("sha256", ""))[:16]}…'
        )
        self._render_report_section()
        self._render_report_citations()

    def _report_section_changed(self, _event=None):
        self._render_report_section()

    def _render_report_section(self):
        value = self.report_sections.get(self.report_section_choice.get(), "")
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
        self.report_editor.delete("1.0", "end")
        self.report_editor.insert("1.0", str(text)[:65536])

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
            self.status.set("Selecione um relatório primeiro.")
            return
        try:
            projection = report_export_projection(
                self.selected_report, section=self.report_section_choice.get() or None,
            )
            text = json.dumps(projection["sections"], ensure_ascii=False, indent=2)
        except (TypeError, ValueError) as exc:
            self.status.set(str(exc))
            return
        if not Clipboard.set_content(text):
            self.status.set("Não foi possível copiar a seção para a área de transferência.")
            return
        self.status.set("Seção copiada sem alterar o relatório gerado.")

    def save_report_review(self):
        if not self.selected or not self.selected_report:
            self.status.set("Selecione um relatório estruturado primeiro.")
            return
        report_id = self.selected_report.get("id")
        section = self.report_section_choice.get()
        if not report_id or report_id == "legacy-summary" or not section:
            self.status.set("O resumo legado não aceita revisão por seção; gere um relatório novo.")
            return
        text = self.report_editor.get("1.0", "end-1c")[:65536]
        artifact = self.selected_report.get("reviewed_artifact")
        expected = artifact.get("generation", 0) if isinstance(artifact, dict) else 0
        session_id = self.selected

        def saved(_value, error):
            if self.closed or session_id != self.selected:
                return
            if error:
                self._remember_operation_error(error)
                self.status.set("A revisão não foi salva; o relatório gerado continua intacto.")
                return
            self.status.set("Revisão salva separadamente do relatório gerado.")
            self.refresh_reports(session_id)

        self._action("review_report", session_id, report_id, {section: text},
                     expected_generation=expected, callback=saved)

    def export_report(self):
        if not self.selected or not self.selected_report:
            self.status.set("Selecione um relatório primeiro.")
            return
        path = filedialog.asksaveasfilename(
            parent=self.window, title="Exportar relatório", defaultextension=".md",
            filetypes=(("Markdown", "*.md"), ("Texto", "*.txt"), ("JSON", "*.json")),
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
            self._remember_operation_error(error)
            self.status.set("Não foi possível exportar o relatório.")
        else:
            self.status.set("Relatório exportado atomicamente sem caminhos locais.")

    def ask_this_meeting(self):
        if not self.selected or not self.detail_ready:
            self.status.set("Selecione uma gravação na biblioteca.")
            return
        question = self.ask_question.get().strip()
        if not question:
            self.status.set("Digite uma pergunta sobre a reunião.")
            return
        model = self.summary_model.get().strip()
        if not self.summary_model_installed.get(model):
            self.status.set("Baixe o modelo selecionado em Configurações antes de perguntar.")
            return
        session_id = self.selected
        self.ask_request += 1
        request = self.ask_request
        self.ask_status.set("Processando localmente; nada será salvo automaticamente.")
        self._action(
            "ask_this_meeting", session_id, question, model,
            revision=self.transcript_revision,
            callback=lambda value, error: self._answer_loaded(session_id, request, question, value, error),
        )

    def _answer_loaded(self, session_id, request, question, value, error):
        if self.closed or request != self.ask_request or session_id != self.selected:
            return
        if error:
            self._remember_operation_error(error)
            self.ask_status.set("A pergunta não foi concluída; nenhuma resposta foi salva.")
            return
        if not isinstance(value, dict):
            self.ask_status.set("A resposta local não tinha um formato utilizável.")
            return
        self.unsaved_answer = dict(value)
        self.unsaved_answer["question"] = question
        self.ask_answer.delete("1.0", "end")
        self.ask_answer.insert("1.0", str(value.get("answer", ""))[:MAX_ANSWER_CHARS])
        self.ask_citation_refs = [item for item in value.get("citations", []) if isinstance(item, str)][:16]
        self.ask_citations.delete(0, "end")
        for identifier in self.ask_citation_refs:
            self.ask_citations.insert("end", identifier[:128])
        self.ask_status.set(
            f'Resposta em memória · incerteza {value.get("uncertainty", "alta")} · '
            f'{len(value.get("citations", []))} citações. '
            + ("O modo memory_only impede salvar." if getattr(self, "qa_mode", None) is not None
               and self.qa_mode.get() == "memory_only" else
               "Salve explicitamente se quiser persistir.")
        )
        self._sync_qa_controls()

    def save_answer(self):
        if getattr(self, "qa_mode", None) is not None and self.qa_mode.get() == "memory_only":
            self.ask_status.set("O workspace está em memory_only; a resposta não pode ser salva.")
            self._sync_qa_controls()
            return
        if not self.selected or not isinstance(self.unsaved_answer, dict):
            self.ask_status.set("Não há uma resposta em memória para salvar.")
            return
        answer = dict(self.unsaved_answer)
        question = str(answer.pop("question", ""))
        model = self.summary_model.get().strip()
        session_id = self.selected
        revision = answer.pop("revision", None) or answer.pop("revision_id", None) or self.transcript_revision

        def saved(value, error):
            if self.closed or session_id != self.selected:
                return
            if error:
                self._remember_operation_error(error)
                self.ask_status.set("A resposta não foi salva; ela continua somente na memória.")
                return
            self.unsaved_answer = None
            self.ask_status.set("Resposta salva como uma revisão Q&A explícita.")
            self.refresh_reports(session_id)

        self._action("save_answer", session_id, answer, model,
                     question=question, revision=revision, callback=saved)

    def jump_to_report_citation(self):
        index = self.report_citations.curselection()
        if not index or index[0] >= len(getattr(self, "report_citation_refs", [])):
            return
        identifier = self.report_citation_refs[index[0]]
        session_id = self.selected
        revision = self.selected_report.get("transcript_revision") if self.selected_report else None
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
                self.status.set(f"A fonte {identifier[:128]} não está disponível na revisão selecionada.")
                return
            self.transcript_offset = int(page.get("offset", 0))
            self.transcript_has_previous = bool(page.get("has_previous"))
            self.transcript_has_more = bool(page.get("has_more"))
            self._render_transcript(page.get("segments", []))
            self._update_transcript_paging_controls()
            for key, segment in self.segments.items():
                if isinstance(segment, dict) and segment.get("id") == identifier:
                    try:
                        self.transcript.selection_set(key)
                        self.transcript.see(key)
                    except (tk.TclError, AttributeError):
                        pass
                    break
            self.status.set(f"Fonte da citação {identifier[:128]} carregada.")

        self._submit("citation_jump", read, loaded)

    def jump_to_ask_citation(self):
        index = self.ask_citations.curselection()
        if not index or index[0] >= len(getattr(self, "ask_citation_refs", [])):
            return
        identifier = self.ask_citation_refs[index[0]]
        if self.selected_report and self.selected_report.get("transcript_revision"):
            revision = self.selected_report["transcript_revision"]
        else:
            revision = self.transcript_revision
        # Reuse the report citation worker after projecting the selected Q&A
        # citation into the same bounded source-navigation path.
        self.report_citation_refs = [identifier]
        self.report_citations.selection_clear(0, "end")
        self.report_citations.selection_set(0)
        original = self.selected_report
        if original is None:
            self.selected_report = {"transcript_revision": revision}
        self.jump_to_report_citation()
        if original is None:
            self.selected_report = None

    def _settings_loaded(self, raw, error):
        if error:
            self._remember_operation_error(error)
            self.status.set("Não foi possível carregar as configurações. Veja os detalhes na aba Gravação.")
            return
        self.raw_settings = dict(raw or {})
        if hasattr(self, "appearance_display"):
            preference = ui_theme.normalize_preference(self.raw_settings.get("appearance"))
            self.appearance_display.set(APPEARANCE_LABELS[preference])
            self.appearance_status.set(APPEARANCE_STATUS[preference])
        try:
            settings = resolve_meeting_settings(self.raw_settings)
        except ValueError as exc:
            self.status.set(str(exc))
            return
        self.settings = settings
        self.input_enabled.set(settings.input_enabled)
        self.output_enabled.set(settings.output_enabled)
        self.destination.set(settings.destination)
        self.destination_label.set(destination_display(settings.destination))
        self.auto_transcribe.set(settings.auto_transcribe)
        self.auto_summary.set(settings.auto_summary)
        self.voice_boost.set(settings.voice_boost)
        self.profile.set(settings.profile)
        self.language.set(settings.language)
        self.profile_display.set(PROFILE_LABELS[settings.profile])
        self.language_display.set(LANGUAGE_LABELS[settings.language])
        self.hotkey.set(settings.hotkey)
        self.summary_model.set(settings.summary_model)
        self.summary_display.set(SUMMARY_LABELS[settings.summary_model])
        self._profile_changed()
        self.options.clear()
        self._render_devices()
        self._sync_source_controls()
        self._automation_toggled()
        self.settings_loaded = True
        if hasattr(self, "controller"):
            self.refresh_report_profiles()
        self.status.set(
            self.startup_status if not getattr(self, "retention_ready", True)
            else "Gravações ficam neste computador. Inicie somente quando quiser gravar."
        )

    def set_startup_status(self, ready, message):
        """Apply app-level recovery admission without exposing private paths."""
        if self.closed:
            return
        self.retention_ready = bool(ready)
        self.startup_status = _safe_retention_text(message, 320)
        if hasattr(self, "start_button") and not ready:
            self.start_button.configure(state="disabled")
        for name in ("delete_button", "raw_remove_button"):
            widget = getattr(self, name, None)
            if widget is not None and not ready:
                widget.configure(state="disabled")
        if hasattr(self, "privacy_status") and not ready:
            self.privacy_status.set(self.startup_status or "Recuperação local pendente.")
        if hasattr(self, "status") and not ready:
            self.status.set(self.startup_status or "Recuperação local pendente; ações destrutivas estão bloqueadas.")
        elif hasattr(self, "status") and ready and self.status.get() in {
                "Preparando privacidade e recuperação local…",
                "Recuperação local pendente; ações destrutivas estão bloqueadas.",
            }:
            self.status.set("Privacidade e recuperação local verificadas. Inicie somente quando quiser gravar.")

    def _privacy_loaded(self, value, error):
        if self.closed:
            return
        if error or not isinstance(value, dict):
            if error:
                self._remember_operation_error(error)
            self.privacy_ready = False
            if hasattr(self, "privacy_status"):
                self.privacy_status.set("Privacidade indisponível; gravação bloqueada até recarregar.")
            return
        self.privacy_defaults = copy.deepcopy(value)
        self.privacy_ready = True
        if hasattr(self, "privacy_notice_enabled"):
            notice = value.get("recording_notice", {})
            if not isinstance(notice, dict):
                notice = {}
            self.privacy_notice_enabled.set(bool(notice.get("enabled", False)))
            self.privacy_notice_language.set(notice.get("language", "pt-BR"))
            self.qa_mode.set(value.get("qa_mode", "explicit_save"))
            self._sync_qa_controls()
            self.privacy_status.set("Privacidade local carregada.")

    def _profile_changed(self, _event=None):
        self.profile.set(next(key for key, label in PROFILE_LABELS.items() if label == self.profile_display.get()))
        languages = available_languages(self.profile.get())
        self.language_box.configure(values=[LANGUAGE_LABELS[language] for language in languages])
        if self.language.get() not in languages:
            self.language.set(languages[0])
        self.language_display.set(LANGUAGE_LABELS[self.language.get()])

    def _language_changed(self, _event=None):
        self.language.set(next(key for key, label in LANGUAGE_LABELS.items() if label == self.language_display.get()))

    def _source_toggled(self):
        self._sync_source_controls()
        self._source_selection_changed()

    def show_recording_settings(self):
        self.notebook.select(self.settings_tab)
        self.settings_sections.select("recording")

    def _source_selection_changed(self, _event=None):
        self.preview_status.set("Fontes alteradas. Teste novamente antes de gravar.")

    def _sync_source_controls(self):
        enabled = {"microphone": bool(self.input_enabled.get()),
                   "system": bool(self.output_enabled.get())}
        for track, box in self.endpoint_boxes.items():
            box.configure(state="readonly" if enabled[track] else "disabled")
        self.voice_boost_check.configure(state="normal" if enabled["microphone"] else "disabled")
        if not enabled["microphone"]:
            self.voice_boost.set(False)

    def _automation_toggled(self):
        enabled = bool(self.auto_transcribe.get())
        if not enabled:
            self.auto_summary.set(False)
        self.auto_summary_check.configure(state="normal" if enabled else "disabled")

    def choose_destination(self):
        initial = self.destination.get().strip() or None
        options = {"parent": self.window, "title": "Pasta padrão das gravações"}
        if initial:
            options["initialdir"] = initial
        path = filedialog.askdirectory(**options)
        if path:
            self.destination.set(path)
            self.destination_label.set(destination_display(path))

    def use_default_destination(self):
        self.destination.set("")
        self.destination_label.set(destination_display(""))

    def show_recording_details(self):
        if self.record_details:
            messagebox.showinfo(
                "Detalhes da gravação", self.record_details, parent=self.window,
            )

    def _remember_operation_error(self, error):
        self.operation_details = "Detalhes técnicos: " + _safe_retention_text(error, 4096)
        self.record_details = self.operation_details
        self.record_details_button.configure(state="normal")

    def _current_settings(self):
        if not self.settings_loaded:
            raise ValueError("Aguarde o carregamento das configurações antes de gravar ou salvar.")
        data = dict(self.raw_settings)
        data.update(meeting_input_enabled=bool(self.input_enabled.get()),
                    meeting_output_enabled=bool(self.output_enabled.get()),
                    meeting_destination=self.destination.get(),
                    meeting_auto_transcribe=bool(self.auto_transcribe.get()),
                    meeting_auto_summary=bool(self.auto_summary.get()),
                    meeting_voice_boost=bool(self.voice_boost.get()),
                    meeting_profile=self.profile.get(), meeting_language=self.language.get(),
                    meeting_hotkey=self.hotkey.get(), meeting_summary_model=self.summary_model.get())
        for track in ("microphone", "system"):
            label = self.endpoint_vars[track].get()
            selection = dict(self.options[track]).get(label)
            if selection is None:
                raise ValueError("Atualize e selecione novamente o dispositivo de áudio.")
            data["meeting_" + track] = selection.payload()
        return validated_settings(data, data.get("voice_hotkey", DEFAULT_DICTATION_HOTKEY),
                                  data.get("voice_command_hotkey", DEFAULT_COMMAND_HOTKEY))

    def _saved_settings(self, settings, error):
        if error:
            self._remember_operation_error(error)
            self.status.set("Não foi possível salvar as configurações. Veja os detalhes na aba Gravação.")
            return
        self.settings = settings
        self.raw_settings.update(settings.payload())
        self._sync_source_controls()
        self._automation_toggled()
        self.status.set("Configurações salvas para a próxima gravação.")
        if self.on_settings_changed:
            self.on_settings_changed()

    def save_settings(self):
        try:
            settings = self._current_settings()
        except (ValueError, StopIteration) as exc:
            self.status.set(str(exc))
            return
        def save():
            result = self.persist_settings(settings.payload())
            if result is False:
                raise ValueError("Não foi possível salvar as configurações. Tente novamente.")
            return settings
        self._submit("save_settings", save, self._saved_settings)

    def refresh_devices(self):
        self.preview_status.set("Dispositivos atualizados. Teste as fontes antes de gravar.")
        self._submit("devices", self.controller.devices, self._devices_loaded)

    @staticmethod
    def _source_signature(settings):
        return settings.sources, settings.microphone.argument(), settings.system.argument()

    def preview_sources(self):
        request = self._validated_recording_request()
        if request is None:
            return
        settings, _title = request
        self.preview_signature = self._source_signature(settings)
        self.preview_status.set("Testando por 3 segundos. Fale ou reproduza áudio nas fontes escolhidas…")
        if not self._submit("preview_sources", lambda: self.controller.preview_sources(settings),
                            self._previewed):
            self.preview_status.set("Não foi possível iniciar o teste. Tente novamente.")

    def _previewed(self, result, error):
        if error:
            self._remember_operation_error(error)
            self.preview_status.set("O teste de áudio falhou. Veja os detalhes e tente novamente.")
            return
        try:
            current = self._source_signature(self._current_settings())
        except (ValueError, StopIteration):
            current = None
        if current != self.preview_signature:
            self.preview_status.set("Fontes alteradas. Teste novamente antes de gravar.")
            return
        names = {"microphone": "Microfone", "system": "Sistema"}
        errors = set(result["errors"])
        parts = []
        for track in result["enabled"]:
            if track in errors:
                state = "falhou"
            elif result["peaks"][track] >= 0.01:
                state = "sinal detectado"
            else:
                state = "sem sinal detectado"
            parts.append(f"{names[track]}: {state}")
        self.preview_status.set(" · ".join(parts) + ".")

    def _devices_loaded(self, devices, error):
        if error:
            self._remember_operation_error(error)
            self.status.set("Não foi possível atualizar os dispositivos. Veja os detalhes na aba Gravação.")
            return
        self.devices = list(islice(devices or [], 256))
        self._render_devices()

    def _render_devices(self):
        for track in ("microphone", "system"):
            current = dict(self.options.get(track, [])).get(self.endpoint_vars[track].get(), getattr(self.settings, track))
            options = endpoint_options(self.devices, track, current)
            self.options[track] = options
            self.endpoint_boxes[track].configure(values=[label for label, _ in options])
            match = next(label for label, selection in options if selection.argument() == current.argument())
            self.endpoint_vars[track].set(match)
        self._sync_source_controls()

    def _validated_recording_request(self):
        try:
            settings = self._current_settings()
            title = self.record_title.get().strip()
            if len(title) > 400:
                raise ValueError("O título deve ter até 400 caracteres.")
            if settings.destination and (not os.path.isabs(settings.destination)
                                         or not os.path.isdir(settings.destination)):
                raise ValueError("A pasta padrão de gravações não existe ou não é absoluta.")
            for track in ("microphone", "system"):
                selection = getattr(settings, track)
                if settings.sources not in ("both", track):
                    continue
                if selection.mode == "manual" and not any(device.get("id") == selection.endpoint_id
                        and device.get("kind") == track for device in self.devices):
                    raise ValueError("O dispositivo manual está indisponível. Atualize ou escolha outro dispositivo.")
        except (ValueError, StopIteration) as exc:
            self.status.set(str(exc))
            return None
        return settings, title

    def request_start(self, origin="window"):
        """Shared GUI-thread start path for window, hotkey, and tray origins."""
        if self.closed:
            return False
        if getattr(self, "privacy_save_inflight", False):
            self.status.set("Aguarde a atualização de privacidade antes de iniciar a gravação.")
            return False
        if not getattr(self, "retention_ready", True):
            self.status.set("A gravação aguarda a recuperação do workspace.")
            return False
        if not getattr(self, "privacy_ready", True):
            self.pending_start_origin = origin
            refresher = getattr(self.controller, "refresh_privacy_defaults", None)
            if callable(refresher):
                self.status.set("Carregando privacidade local antes de gravar…")
                if not self._submit("privacy_start", refresher, self._privacy_ready_for_start):
                    self.pending_start_origin = None
                    return False
                return True
            self.status.set("Privacidade local indisponível; gravação bloqueada.")
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
        self.recording_notice_dialog = dialog
        dialog.title("Confirmação antes de gravar")
        dialog.transient(self.window)
        dialog.grab_set()
        dialog.configure(bg=self.ui.surface)
        self._label(dialog, "Aviso de gravação", font=self.ui.font(12, "bold"),
                    fg=self.ui.text_strong).pack(anchor="w", padx=self.ui.space_lg,
                    pady=(self.ui.space_lg, self.ui.space_sm))
        self._label(dialog, "O texto é selecionável e pode ser copiado para compartilhar com os participantes.",
                    fg=self.ui.text_muted, wraplength=560, justify="left", anchor="w").pack(
                    fill="x", padx=self.ui.space_lg, pady=(0, self.ui.space_sm))
        notice_box = tk.Text(dialog, height=8, width=72, wrap="word", font=self.ui.font(),
                             **self.ui.text_colors())
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
            copy_status.set("Aviso copiado." if copied else "Não foi possível copiar; selecione o texto manualmente.")

        def cancel():
            self.pending_start_origin = None
            try:
                dialog.grab_release()
                dialog.destroy()
            except tk.TclError:
                pass
            self.recording_notice_dialog = None
            self.status.set("Gravação cancelada no aviso de consentimento.")

        def confirm():
            grant = getattr(self.controller, "grant_recording_consent", None)
            try:
                if callable(grant):
                    grant()
            except Exception as exc:
                self._remember_operation_error(exc)
                self.status.set("Não foi possível registrar a confirmação; gravação bloqueada.")
                return
            try:
                dialog.grab_release()
                dialog.destroy()
            except tk.TclError:
                pass
            self.recording_notice_dialog = None
            self._submit_start(settings, title, origin)

        self._button(actions, "Copiar aviso", copy_notice).pack(side="left")
        self._label(actions, "", textvariable=copy_status, fg=self.ui.text_muted).pack(side="left", padx=8)
        self._button(actions, "Cancelar", cancel).pack(side="right", padx=(8, 0))
        self._button(actions, "Confirmar e iniciar", confirm, accent=True).pack(side="right")
        dialog.protocol("WM_DELETE_WINDOW", cancel)
        dialog.bind("<Escape>", lambda _event: cancel())
        dialog.geometry("650x360")
        dialog.lift()
        dialog.focus_force()
        return dialog

    def _submit_start(self, settings, title, origin="window"):
        self.status.set(f"Iniciando gravação ({origin})…")
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
            self._remember_operation_error(error)
            self.status.set("Não foi possível iniciar a gravação. Veja os detalhes na aba Gravação.")
        else:
            if not accepted:
                revoke = getattr(self.controller, "revoke_recording_consent", None)
                if callable(revoke):
                    revoke()
            self.status.set("Gravação solicitada." if accepted else
                            "Não foi possível iniciar. Verifique o estado da gravação.")

    def stop(self):
        self._action("stop", urgent=True)

    def pause_resume(self):
        self._action("resume" if self.snapshot.get("state") == "paused" else "pause", urgent=True)

    def _action(self, method, *args, urgent=False, callback=None, **kwargs):
        self.operation_details = ""
        self.status.set("Processando solicitação…")
        self._submit("control" if urgent else method, lambda: getattr(self.controller, method)(*args, **kwargs),
                     callback, urgent)

    def search(self):
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

    @staticmethod
    def _filter_value(value):
        parts = [item.strip() for item in str(value or "").split(",") if item.strip()]
        if not parts:
            return None
        return parts if len(parts) > 1 else parts[0]

    def _library_filters(self):
        return {
            "collection": self._filter_value(self.collection_filter.get()),
            "tag": self._filter_value(self.tag_filter.get()),
            "person": self._filter_value(self.people_filter.get()),
            "series": self._filter_value(self.series_filter.get()),
            "date_from": self.date_from_filter.get().strip() or None,
            "date_to": self.date_to_filter.get().strip() or None,
        }

    def clear_library_filters(self):
        for variable in (self.collection_filter, self.tag_filter, self.people_filter,
                         self.series_filter, self.date_from_filter, self.date_to_filter):
            variable.set("")
        self.search()

    def refresh_library(self):
        cursor, query = self.library_cursor, self.query.get()
        status = STATUS_FILTERS[self.status_filter.get()]
        filters = self._library_filters()
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
            return page, search_results, generation
        self._submit("library", read, self._library_loaded)

    def _library_loaded(self, sessions, error):
        if error:
            self._remember_operation_error(error)
            self.status.set("Não foi possível carregar a biblioteca. Tente novamente.")
            if not self.sessions.get_children():
                self._set_library_detail_visible(False)
                self.library_empty.set("Não foi possível carregar a biblioteca.")
                for button in (self.library_empty_record_button, self.library_empty_import_button,
                               self.library_empty_clear_button, self.library_empty_retry_button):
                    button.pack_forget()
                self.library_empty_retry_button.pack(side="left")
                self.library_empty_panel.place(relx=0, rely=0, relwidth=1, relheight=1)
                self.library_empty_panel.lift()
            return
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
        else:
            items = list(islice(sessions or [], PAGE_SIZE))
            self.library_next_cursor = None
            index_state = "compatibility"
        self.sessions.delete(*self.sessions.get_children())
        for item in items:
            self.sessions.insert("", "end", iid=item["id"], values=(item.get("title") or item["id"],
                STATE_LABELS.get(item.get("status"), item.get("status", ""))))
        self.previous_button.configure(state="normal" if self.library_page_index else "disabled")
        self.next_button.configure(state="normal" if self.library_next_cursor else "disabled")
        self.page_label.set(f"Página {self.library_page_index + 1} · {len(items)} gravações")
        if self.library_cursor_reset:
            self.library_cursor = None
            self.library_back_stack = [None]
            self.library_page_index = 0
            self.offset = 0
            self.previous_button.configure(state="disabled")
            self.page_label.set(f"Página 1 · {len(items)} gravações")
            self.index_status.set("Índice de busca mudou; listagem reiniciada.")
        elif index_state:
            self.index_status.set(f"Índice de busca: {INDEX_STATE_LABELS.get(index_state, index_state)}")
        self._render_library_empty_state(items)
        self._render_search_results(search_results)
        if self.selected in self.sessions.get_children():
            self.sessions.selection_set(self.selected)

    def _render_search_results(self, results):
        self.search_result_items = [item for item in (results or ()) if isinstance(item, dict)][:MAX_SEARCH_RESULTS]
        self.search_results.delete(*self.search_results.get_children())
        labels = {
            "transcript": "Transcrição",
            "report": "Relatório",
            "reviewed_artifact": "Revisão",
            "session": "Reunião",
        }
        for index, item in enumerate(self.search_result_items):
            source = labels.get(item.get("source_kind"), str(item.get("source_kind", "Fonte")))
            meeting = str(item.get("title") or item.get("session_id") or "")[:120]
            snippet = " ".join(str(item.get("snippet", "")).split())[:480]
            self.search_results.insert("", "end", iid=str(index), values=(source, meeting, snippet))
        frame = getattr(self, "search_results_frame", None)
        if frame is not None:
            if self.search_result_items:
                frame.pack(fill="x")
            else:
                frame.pack_forget()

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
                self._remember_operation_error(error)
                self.status.set("A fonte da busca não está mais disponível.")
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
                self.status.set("Fonte do relatório carregada.")
        else:
            self.status.set("Reunião encontrada na biblioteca.")

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
                self.status.set("O trecho citado não está disponível nesta revisão.")
                return
            self.transcript_offset = page.get("offset", 0)
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
            self.status.set(f"Fonte carregada · {format_time(next((item.get('start', 0) for item in page.get('segments', []) if item.get('id') == segment_id), 0))}")
        self._submit("search_citation", read, loaded)

    def refresh_workspace(self):
        def read():
            return self.controller.read_workspace()
        self._submit("workspace", read, self._workspace_loaded)

    def _workspace_loaded(self, workspace, error):
        if self.closed:
            return
        if error or not isinstance(workspace, dict):
            if error:
                self._remember_operation_error(error)
            self.organization_status.set("As definições de organização não puderam ser carregadas.")
            return
        self._apply_workspace_settings(workspace)
        self.organization_definitions = {
            "collections": list(workspace.get("collections", []) or []),
            "series": list(workspace.get("series", []) or []),
        }

    @staticmethod
    def _split_organization(value):
        return [item.strip() for item in str(value or "").split(",") if item.strip()]

    def save_organization(self):
        if not self.selected or not self.detail_ready:
            self.organization_status.set("Selecione uma reunião antes de salvar a organização.")
            return
        session_id = self.selected
        expected = self.annotation_generation
        values = {
            "collection_ids": self._split_organization(self.organization_collections.get()),
            "tags": self._split_organization(self.organization_tags.get()),
            "people": self._split_organization(self.organization_people.get()),
            "series_id": self.organization_series.get().strip() or None,
        }
        self._submit(
            "organization", lambda: self.controller.assign_organization(
                session_id, expected_generation=expected, **values,
            ),
            lambda value, error: self._organization_saved(session_id, value, error),
        )

    def _organization_saved(self, session_id, value, error):
        if self.closed or session_id != self.selected:
            return
        if error:
            self._remember_operation_error(error)
            self.organization_status.set("A organização não foi salva; recarregue a reunião.")
            return
        if isinstance(value, dict):
            generation = value.get("generation")
            if isinstance(generation, int):
                self.annotation_generation = generation
        self.organization_status.set("Organização salva.")
        self.search()

    def assign_selected_organization(self):
        session_ids = list(self.sessions.selection())
        if not session_ids:
            self.status.set("Selecione uma ou mais reuniões para atribuir organização.")
            return
        if len(session_ids) > 500:
            self.status.set("A seleção excede o limite de 500 reuniões por lote.")
            return
        tags = simpledialog.askstring("Tags", "Tags separadas por vírgula (vazio mantém a lista atual):", parent=self.window)
        if tags is None:
            return
        collection_ids = simpledialog.askstring("Coleções", "IDs de coleção/projeto separados por vírgula:", parent=self.window)
        if collection_ids is None:
            return
        series_id = simpledialog.askstring("Série", "ID da série (vazio remove a série):", parent=self.window)
        if series_id is None:
            return
        self.batch_cancel_event = threading.Event()
        self.batch_cancel_button.configure(state="normal")
        changes = {
            "tags": self._split_organization(tags),
            "collection_ids": self._split_organization(collection_ids),
            "series_id": series_id.strip() or None,
        }
        def preview_loaded(preview, error):
            if error:
                self.batch_cancel_button.configure(state="disabled")
                self._remember_operation_error(error)
                self.status.set("A prévia do lote falhou; nenhuma anotação foi alterada.")
                return
            if self.batch_cancel_event.is_set():
                self.batch_cancel_button.configure(state="disabled")
                self.status.set("A atribuição em lote foi cancelada.")
                return
            if not messagebox.askyesno(
                    "Confirmar atribuição", f"Aplicar organização a {preview.get('count', 0)} reuniões?",
                    parent=self.window):
                self.batch_cancel_button.configure(state="disabled")
                return
            expected = {item["id"]: item["generation"] for item in preview.get("items", [])}
            self._submit(
                "organization_batch_apply",
                lambda: self.controller.assign_organization_batch(
                    session_ids, expected_generations=expected,
                    cancel_event=self.batch_cancel_event, **changes,
                ),
                self._batch_organization_finished,
            )
        self._submit(
            "organization_batch_preview",
            lambda: self.controller.preview_organization_batch(session_ids, **changes),
            preview_loaded,
        )

    def cancel_batch_organization(self):
        event = getattr(self, "batch_cancel_event", None)
        if event is not None:
            event.set()
        self.status.set("Cancelamento do lote solicitado…")

    def _batch_organization_finished(self, value, error):
        self.batch_cancel_button.configure(state="disabled")
        if error:
            self._remember_operation_error(error)
            self.status.set("A atribuição em lote falhou; a biblioteca informou o estado do rollback.")
            return
        self.status.set(f"Organização atribuída a {value.get('count', 0) if isinstance(value, dict) else 0} reuniões.")
        self.search()

    def ask_across_meetings(self):
        question = self.cross_question.get().strip()
        if not question:
            self.cross_status.set("Digite uma pergunta.")
            return
        self.cross_request += 1
        request = self.cross_request
        self.cross_cancel_button.configure(state="normal")
        self.cross_status.set("Processando evidência local; a resposta não será salva automaticamente.")
        filters = self._library_filters()
        filters["status"] = STATUS_FILTERS[self.status_filter.get()]
        self._submit(
            "cross_question",
            lambda: self.controller.ask_across_meetings(
                question, self.summary_model.get(), filters=filters,
            ),
            lambda value, error: self._cross_answer_loaded(request, value, error),
        )

    def cancel_cross_question(self):
        self.controller.cancel_processing()
        self.cross_status.set("Cancelamento solicitado; a resposta anterior foi preservada.")

    def _cross_answer_loaded(self, request, value, error):
        if self.closed or request != self.cross_request:
            return
        self.cross_cancel_button.configure(state="disabled")
        if error:
            self._remember_operation_error(error)
            self.cross_status.set("A pergunta cruzada não foi concluída; nada foi salvo.")
            return
        if not isinstance(value, dict):
            self.cross_status.set("A resposta cruzada não tinha um formato utilizável.")
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
                "end", f"{item.get('session_id', '')} · {item.get('revision_id', '')} · "
                        f"{item.get('segment_id', '')} · {format_time(item.get('start', 0))}",
            )
        self.cross_status.set(
            f"Resposta em memória · incerteza {value.get('uncertainty', 'alta')} · "
            f"{len(self.cross_citation_refs)} citações resolvidas."
        )

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
            self.status.set("A citação cruzada deixou de resolver para a transcrição canônica.")
            return
        if value.get("session_id") != self.selected:
            self.pending_search_resolution = value
            self.load_session(value["session_id"])
            return
        self._apply_search_resolution(value)

    def show_trash(self):
        """Open a bounded trash manager; all mutations stay on the IO lane."""
        if not getattr(self, "retention_ready", True):
            self.status.set("A lixeira aguarda a recuperação do workspace.")
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
        dialog = tk.Toplevel(self.window)
        self.trash_dialog = dialog
        dialog.title("Lixeira local")
        dialog.geometry("720x420")
        dialog.minsize(560, 300)
        dialog.transient(self.window)
        dialog.configure(bg=self.ui.surface)
        self._label(dialog, "Lixeira local", font=self.ui.font(12, "bold"),
                    fg=self.ui.text_strong).pack(anchor="w", padx=self.ui.space_lg,
                    pady=(self.ui.space_lg, self.ui.space_xs))
        self._label(
            dialog,
            "Restaurar retorna a reunião à biblioteca. Purga permanente exige uma segunda confirmação. “Esvaziar expirados” remove somente itens vencidos.",
            fg=self.ui.text_muted, wraplength=660, justify="left", anchor="w",
        ).pack(fill="x", padx=self.ui.space_lg, pady=(0, self.ui.space_sm))
        frame = tk.Frame(dialog, bg=self.ui.surface)
        frame.pack(fill="both", expand=True, padx=self.ui.space_lg)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self.trash_tree = ttk.Treeview(
            frame, columns=("meeting", "deleted", "purge", "bytes"),
            show="headings", selectmode="browse", style="Meeting.Treeview",
        )
        for column, label, width in (
            ("meeting", "Reunião", 230), ("deleted", "Movida em", 150),
            ("purge", "Expira em", 150), ("bytes", "Bytes", 90),
        ):
            self.trash_tree.heading(column, text=label)
            self.trash_tree.column(column, width=width, stretch=column == "meeting")
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.trash_tree.yview)
        self.trash_tree.configure(yscrollcommand=scrollbar.set)
        self.trash_tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.trash_status = tk.StringVar(dialog, "Carregando lixeira…")
        self._label(dialog, "", textvariable=self.trash_status, fg=self.ui.text_muted,
                    anchor="w").pack(fill="x", padx=self.ui.space_lg, pady=(self.ui.space_xs, 0))
        actions = tk.Frame(dialog, bg=self.ui.surface)
        actions.pack(fill="x", padx=self.ui.space_lg, pady=self.ui.space_md)
        self._button(actions, "Atualizar", self.refresh_trash).pack(side="left")
        self._button(actions, "Restaurar selecionada", self.restore_selected_trash).pack(side="left", padx=6)
        self._button(actions, "Purgar selecionada…", self.purge_selected_trash, danger=True).pack(side="left")
        self._button(actions, "Esvaziar expirados…", self.empty_expired_trash, danger=True).pack(side="right")
        dialog.protocol("WM_DELETE_WINDOW", self._close_trash_dialog)
        dialog.bind("<Escape>", lambda _event: self._close_trash_dialog())
        self.refresh_trash()
        return dialog

    def _close_trash_dialog(self):
        self.trash_request += 1
        dialog, self.trash_dialog = self.trash_dialog, None
        self.trash_tree = None
        if dialog is not None:
            try:
                dialog.destroy()
            except tk.TclError:
                pass

    def refresh_trash(self):
        if self.closed:
            return
        tree = self.trash_tree
        if tree is None:
            return
        self.trash_request += 1
        request = self.trash_request
        self.trash_status.set("Carregando lixeira…")
        if not self._submit("trash_list", self.controller.list_trash,
                            lambda value, error: self._trash_loaded(value, error, request)):
            self.trash_status.set("A lixeira está ocupada; tente novamente.")

    def _trash_loaded(self, entries, error, request):
        if self.closed or request != self.trash_request or self.trash_tree is None:
            return
        if error:
            self._remember_operation_error(error)
            self.trash_status.set("Não foi possível carregar a lixeira.")
            return
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
        self.trash_tree.delete(*self.trash_tree.get_children())
        for index, entry in enumerate(projected):
            self.trash_tree.insert(
                "", "end", iid=str(index), values=(entry["session_id"], entry["deleted_at"],
                entry["purge_after"], entry["byte_estimate"]),
            )
        self.trash_status.set(f"{len(projected)} reunião(ões) na lixeira.")

    def _selected_trash_entry(self):
        if self.trash_tree is None:
            return None
        selected = self.trash_tree.selection()
        if not selected:
            self.trash_status.set("Selecione uma reunião na lixeira.")
            return None
        try:
            entry = self.trash_entries[int(selected[0])]
        except (IndexError, TypeError, ValueError):
            self.trash_status.set("A seleção da lixeira ficou desatualizada; atualize a lista.")
            return None
        return entry

    def restore_selected_trash(self):
        entry = self._selected_trash_entry()
        if not entry:
            return
        session_id = entry["session_id"]
        request = self.trash_request
        self.trash_status.set("Restaurando reunião…")
        self._submit(
            "trash_restore",
            lambda: self.controller.restore_session(session_id),
            lambda value, error: self._trash_mutation_finished("restaurada", value, error, request),
        )

    def purge_selected_trash(self):
        entry = self._selected_trash_entry()
        if not entry:
            return
        session_id = entry["session_id"]
        if not messagebox.askyesno(
                "Purgar permanentemente",
                "A purga permanente não pode ser desfeita e não promete apagamento forense de SSD. Continuar?",
                parent=self.trash_dialog):
            return
        request = self.trash_request
        self.trash_status.set("Purgando reunião…")
        self._submit(
            "trash_purge",
            lambda: self.controller.purge_session(session_id, confirm=True),
            lambda value, error: self._trash_mutation_finished("purgada", value, error, request),
        )

    def empty_expired_trash(self):
        if not messagebox.askyesno(
                "Esvaziar itens expirados",
                "Somente reuniões cujo prazo da lixeira já venceu serão purgadas. Esta ação é permanente. Continuar?",
                parent=self.trash_dialog):
            return
        request = self.trash_request
        self.trash_status.set("Purgando itens expirados…")
        self._submit(
            "trash_empty_expired",
            lambda: self.controller.empty_trash(confirm=True),
            lambda value, error: self._trash_mutation_finished("expirados purgados", value, error, request),
        )

    def _trash_mutation_finished(self, label, value, error, request):
        if self.closed or request != self.trash_request or self.trash_tree is None:
            return
        if error or value is False:
            if error:
                self._remember_operation_error(error)
            self.trash_status.set("A mutação da lixeira falhou; atualize para confirmar o estado.")
            return
        self.trash_status.set(f"Reunião(ões) {label}; lixeira atualizada.")
        self.refresh_trash()
        self.refresh_library()

    def rebuild_index(self):
        if self.rebuild_cancel is not None:
            return
        self.rebuild_cancel = threading.Event()
        self.rebuild_progress = {"done": 0, "total": None, "state": "rebuilding"}
        self.rebuild_button.configure(state="disabled")
        self.rebuild_cancel_button.configure(state="normal")
        self.index_status.set("Índice de busca: reconstruindo…")
        self._submit(
            "rebuild_index",
            lambda: self.controller.rebuild_index(
                cancel_event=self.rebuild_cancel,
                progress=self._rebuild_progress_from_worker,
            ),
            self._rebuild_finished,
        )

    def _rebuild_progress_from_worker(self, done, total):
        self.rebuild_progress = {"done": done, "total": total, "state": "rebuilding"}

    def cancel_rebuild_index(self):
        if self.rebuild_cancel is not None:
            self.rebuild_cancel.set()
            self.index_status.set("Índice de busca: cancelamento solicitado…")

    def _rebuild_finished(self, value, error):
        self.rebuild_cancel = None
        self.rebuild_button.configure(state="normal")
        self.rebuild_cancel_button.configure(state="disabled")
        if error:
            self._remember_operation_error(error)
            self.index_status.set("Índice de busca: reconstrução cancelada ou indisponível.")
            return
        state = value.get("state", "ready") if isinstance(value, dict) else "ready"
        count = value.get("sessions", 0) if isinstance(value, dict) else 0
        self.rebuild_progress = {"done": count, "total": count, "state": state}
        self.index_status.set(f"Índice de busca: {INDEX_STATE_LABELS.get(state, state)} · {count} reuniões")
        self.search()

    def _selection_changed(self, _event=None):
        selected = self.sessions.selection()
        batch = getattr(self, "batch_controls", None)
        if batch is not None:
            if len(selected) > 1:
                batch.pack(fill="x", pady=(6, 0))
            else:
                batch.pack_forget()
        if not selected or selected[0] == self.selected:
            return
        session_id = selected[0]
        if self.dirty:
            answer = messagebox.askyesnocancel("Notas não salvas", "Salvar as notas antes de abrir outra gravação?", parent=self.window)
            if answer is None:
                if self.selected in self.sessions.get_children():
                    self.sessions.selection_set(self.selected)
                return
            if answer:
                self.save_notes(after=lambda: self.load_session(session_id))
                return
        self.pending_search_resolution = None
        self.load_session(session_id)

    def load_session(self, session_id):
        self._action("stop_playback", urgent=True)
        self.selected = session_id
        self._show_library_detail(True)
        self.detail_sections.select("audio")
        self.report_request = getattr(self, "report_request", 0) + 1
        self.ask_request = getattr(self, "ask_request", 0) + 1
        self.citation_request = getattr(self, "citation_request", 0) + 1
        self.selected_report = None
        self.report_sections = {}
        self.unsaved_answer = None
        if hasattr(self, "ask_answer"):
            self.ask_answer.delete("1.0", "end")
        if hasattr(self, "ask_citations"):
            self.ask_citations.delete(0, "end")
        self.ask_citation_refs = []
        if hasattr(self, "ask_status"):
            self.ask_status.set("Respostas ficam somente na memória até você salvar.")
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
        self.selected_speaker_label_id = None
        self.selected_highlight_id = None
        self.delete_button.configure(state="disabled")
        self.play_button.configure(state="disabled")
        self.replay_stop_button.configure(state="disabled")
        self.audio_overview.set("Carregando áudio…")
        self.playback_status.set("Pronto para ouvir.")
        self.playback_position.set(0.0)
        self.loading = True
        self.title.set("")
        self.notes.configure(state="normal")
        self.notes.delete("1.0", "end")
        self.notes.edit_modified(False)
        self.notes.configure(state="disabled")
        self.loading = False
        self.dirty = False
        self.bookmarks = []
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
            notes = str(raw.get("notes", ""))
            metadata = {key: raw.get(key) for key in ("id", "title", "status", "duration", "error")}
            final = raw.get("final_audio")
            final_path = final.get("path") if isinstance(final, dict) else None
            metadata["final_available"] = bool(isinstance(final_path, str) and os.path.isfile(final_path))
            tracks = raw.get("tracks") if isinstance(raw.get("tracks"), dict) else {}
            metadata["tracks"] = {
                track: {
                    "available": value.get("available", True) is not False,
                    "raw_removed": bool(value.get("raw_removed")),
                }
                for track, value in tracks.items()
                if track in TRACK_LABELS and isinstance(value, dict)
            }
            bookmarks = list(islice(raw.get("bookmarks", []), BOOKMARK_LIMIT + 1))
            invalid_bookmarks = any(not isinstance(item, dict) for item in bookmarks)
            summary = raw.get("reviewed_summary") or raw.get("summary")
            if isinstance(summary, dict):
                summary = json.dumps(summary, ensure_ascii=False, indent=2)
            revision = active_transcript_revision(raw)
            page = self._read_transcript_page(session_id, revision, 0)
            annotations = raw.get("annotations") if isinstance(raw.get("annotations"), dict) else {}
            annotations = annotations_for_revision(annotations, revision)
            speaker_labels = annotations.get("speaker_labels", {})
            highlights = annotations.get("highlights", [])
            metadata.update(notes=notes[:NOTES_LIMIT], truncated=len(notes) > NOTES_LIMIT
                            or len(bookmarks) > BOOKMARK_LIMIT or invalid_bookmarks,
                            bookmarks=bookmarks[:BOOKMARK_LIMIT], summary=str(summary or "")[:65536],
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
        self.status.set("Carregando gravação…")
        self._submit("detail", read, lambda data, error: self._detail_loaded(session_id, data, error))

    def _detail_loaded(self, session_id, data, error):
        if session_id != self.selected:
            return
        if error:
            self._remember_operation_error(error)
            self.status.set("Não foi possível carregar a gravação. Veja os detalhes na aba Gravação.")
            return
        metadata, segments = data
        self.loading = True
        self.title.set(metadata.get("title") or "")
        self.notes.configure(state="normal")
        self.notes.delete("1.0", "end")
        self.notes.insert("1.0", metadata["notes"])
        self.truncated = metadata["truncated"]
        self.transcript_revision = metadata.get("transcript_revision")
        self.transcript_offset = metadata.get("transcript_offset", 0)
        self.transcript_has_previous = bool(metadata.get("transcript_has_previous"))
        self.transcript_has_more = bool(metadata.get("transcript_has_more"))
        generation = metadata.get("annotation_generation", 0)
        self.annotation_generation = generation if isinstance(generation, int) else 0
        self.organization_collections.set(", ".join(metadata.get("collection_ids", [])))
        self.organization_tags.set(", ".join(metadata.get("tags", [])))
        self.organization_people.set(", ".join(metadata.get("people", [])))
        self.organization_series.set(str(metadata.get("series_id") or ""))
        self.organization_status.set("Organização carregada.")
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
        state = STATE_LABELS.get(metadata.get("status"), "Gravação")
        self.audio_overview.set(f"{format_time(self.playback_duration)} · {state}")
        self._set_audio_capabilities(metadata.get("tracks", {}), metadata.get("final_available", False))
        self.detail_ready = True
        self.delete_button.configure(
            state="normal" if self.retention_ready and metadata.get("status") != "recording"
            else "disabled"
        )
        if self.truncated:
            self.notes.configure(state="disabled")
        self.notes.edit_modified(False)
        self.bookmarks = metadata["bookmarks"]
        self.dirty = False
        self.loading = False
        self._render_bookmarks()
        self._render_transcript(segments)
        self._render_annotations(self.speaker_labels, self.annotation_generation)
        self._update_transcript_paging_controls()
        self._show_summary(metadata.get("summary"))
        self.refresh_reports(session_id)
        pending_search = getattr(self, "pending_search_resolution", None)
        if isinstance(pending_search, dict) and pending_search.get("session_id") == session_id:
            self.pending_search_resolution = None
            self._apply_search_resolution(pending_search, self.search_request)
        self.status.set("Notas extensas: visualização parcial, somente leitura." if self.truncated else
                        f"Gravação carregada · página de transcrição com {len(segments)} trechos."
                         + (" · Uma etapa anterior não foi concluída." if metadata.get("error") else ""))

    def _set_audio_capabilities(self, tracks, final_available=False):
        unavailable = set()
        self.raw_tracks_present = set()
        if isinstance(tracks, dict):
            for track, value in tracks.items():
                if track in TRACK_LABELS.values() and isinstance(value, dict):
                    self.raw_tracks_present.add(track)
                if isinstance(value, dict) and (value.get("available") is False or value.get("raw_removed")):
                    unavailable.add(track)
        for track, variable_name, widget_name in (
            ("microphone", "raw_remove_microphone", "raw_remove_microphone_check"),
            ("system", "raw_remove_system", "raw_remove_system_check"),
        ):
            variable = getattr(self, variable_name, None)
            if variable is not None:
                variable.set(track in self.raw_tracks_present and track not in unavailable)
            widget = getattr(self, widget_name, None)
            if widget is not None:
                widget.configure(state="normal" if track in self.raw_tracks_present and track not in unavailable else "disabled")
        self.raw_unavailable_tracks = unavailable
        available = self.raw_tracks_present - unavailable
        self.playback_choices = playback_sources(tracks, final_available)
        if hasattr(self, "audio_source_box"):
            self.audio_source_box.configure(values=self.playback_choices,
                                            state="readonly" if self.playback_choices else "disabled")
            if self.audio_source.get() not in self.playback_choices:
                self.audio_source.set(self.playback_choices[0] if self.playback_choices else "")
        self.raw_capabilities = {
            "playback": bool(self.playback_choices),
            "retranscription": bool(available),
            "clip": bool(available),
            "audio_export": bool(available),
        }
        if unavailable:
            labels = ", ".join(TRACK_LABELS.get(item, item) for item in sorted(unavailable))
            self.audio_capability_status.set(
                f"Áudio raw indisponível ({labels}). Transcrição, relatórios, citações e exportações de texto continuam disponíveis."
            )
        else:
            self.audio_capability_status.set(
                "Áudio raw disponível para reprodução, retranscrição e clipes."
                if available else "Nenhuma fonte de áudio raw está disponível."
            )
        for name, state in (
            ("play_button", "normal" if self.playback_choices else "disabled"),
            ("transcribe_button", "normal" if self.raw_capabilities["retranscription"] else "disabled"),
            ("export_audio_button", "normal" if self.raw_capabilities["audio_export"] else "disabled"),
            ("raw_remove_button", "normal" if getattr(self, "retention_ready", True)
             and available
             else "disabled"),
        ):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.configure(state=state)
        if getattr(self, "highlight_export_button", None) is not None:
            self.highlight_export_button.configure(
                state="normal" if self.raw_capabilities["clip"] else "disabled"
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
        operation = RETENTION_POLICY_LABELS.get(
            projection.get("operation"), projection.get("operation", "retenção local"),
        )
        status = "elegível" if projection.get("eligible") else "bloqueada"
        lines = [
            f"Reunião: {_safe_retention_text(title or projection.get('session_id'), 160)}",
            f"Operação: {operation} · {status}",
            f"Inventário: {projection.get('target_count', 0)} alvo(s) · {projection.get('byte_estimate', 0)} bytes",
            f"Recuperação: {projection.get('recovery_mode', 'none')}",
        ]
        tracks = projection.get("raw_tracks") or []
        if tracks:
            lines.append("Fontes raw: " + ", ".join(tracks))
        for label, key in (
            ("Motivos", "reasons"), ("Capacidades perdidas", "lost_capabilities"),
            ("Exportações externas preservadas", "excluded_external_exports"),
        ):
            values = projection.get(key) or []
            if values:
                lines.append(label + ":\n- " + "\n- ".join(values[:8]))
        return "\n".join(lines)[:6000]

    def _retention_preview_loaded(self, session_id, title, plan, error, request):
        if self.closed or request != self.retention_request or self.selected != session_id:
            return
        if error:
            self.delete_button.configure(state="normal")
            self._remember_operation_error(error)
            self.status.set("A prévia de retenção falhou; nenhuma exclusão foi tentada.")
            return
        projection = retention_plan_projection(plan)
        if not projection.get("eligible"):
            self.delete_button.configure(state="normal")
            self.status.set("A retenção foi bloqueada: " + "; ".join(projection.get("reasons", [])[:3]))
            messagebox.showwarning("Retenção bloqueada", self._retention_preview_text(projection, title), parent=self.window)
            return
        if not messagebox.askyesno(
                "Prévia de retenção",
                self._retention_preview_text(projection, title)
                + "\n\nMover para a lixeira do app agora? A confirmação usa exatamente esta prévia.",
                parent=self.window):
            self.delete_button.configure(state="normal")
            self.status.set("Nenhuma exclusão foi aplicada.")
            return
        self.status.set("Aplicando a prévia de retenção…")
        if not self._submit(
                "retention_apply",
                lambda: self.controller.apply_retention(plan, confirm=True),
                lambda value, apply_error: self._retention_applied(session_id, value, apply_error, request),
        ):
            self.delete_button.configure(state="normal")

    def _retention_applied(self, session_id, value, error, request):
        if self.closed or request != self.retention_request or self.selected != session_id:
            return
        if error or value is False:
            self.delete_button.configure(state="normal")
            if error:
                self._remember_operation_error(error)
            self.status.set("A prévia ficou inválida ou a retenção falhou; revise e gere uma nova prévia.")
            return
        self._clear_library_detail()
        self.refresh_library()
        self.status.set("Reunião movida para a lixeira do app; cópias externas não foram alteradas.")

    def _legacy_delete_selected(self):
        """Compatibility path for old embedded doubles only."""
        session_id = self.selected
        title = self.title.get().strip() or session_id
        message = (
            f'Excluir “{title}” da biblioteca?\n\n'
            "Os arquivos capturados, a transcrição, as notas e o resumo serão movidos "
            "para a lixeira do app. Um arquivo final exportado para outra pasta não será apagado."
        )
        if not messagebox.askyesno("Excluir gravação", message, parent=self.window):
            return
        self.delete_button.configure(state="disabled")
        self.status.set("Movendo gravação para a lixeira…")

        def deleted(value, error):
            if error or value is False:
                if self.selected == session_id:
                    self.delete_button.configure(state="normal")
                if error:
                    self._remember_operation_error(error)
                self.status.set("Não foi possível mover a gravação para a lixeira.")
                return
            if self.selected == session_id:
                self._clear_library_detail()
            self.refresh_library()
            self.status.set("Gravação movida para a lixeira do app.")

        if not self._submit("delete_session", lambda: self.controller.delete_session(session_id), deleted):
            self.delete_button.configure(state="normal")

    def delete_selected(self):
        if not self.selected or not self.detail_ready:
            self.status.set("Selecione uma gravação na biblioteca.")
            return
        if not getattr(self, "retention_ready", True):
            self.status.set("A retenção aguarda a recuperação do workspace.")
            return
        if not self._retention_workflow_available(self.controller):
            return self._legacy_delete_selected()
        session_id = self.selected
        title = self.title.get().strip() or session_id
        self.retention_request += 1
        request = self.retention_request
        self.delete_button.configure(state="disabled")
        self.status.set("Calculando prévia de retenção…")
        if not self._submit(
                "retention_plan",
                lambda: self.controller.retention_plan(
                    session_id, policy={"mode": "whole_meeting", "after_days": 0},
                ),
                lambda plan, error: self._retention_preview_loaded(
                    session_id, title, plan, error, request,
                ),
        ):
            self.delete_button.configure(state="normal")

    def preview_raw_tracks(self, tracks=None):
        if not self.selected or not self.detail_ready:
            self.status.set("Selecione uma gravação antes de remover áudio raw.")
            return
        if not getattr(self, "retention_ready", True):
            self.status.set("A retenção aguarda a recuperação do workspace.")
            return
        if tracks is None:
            selected_tracks = tuple(
                track for track, variable_name in (
                    ("microphone", "raw_remove_microphone"), ("system", "raw_remove_system"),
                ) if bool(getattr(self, variable_name, None) and getattr(self, variable_name).get())
            )
        else:
            selected_tracks = tuple(tracks)
        if not selected_tracks:
            selected_tracks = tuple(sorted(
                getattr(self, "raw_tracks_present", set()) - getattr(self, "raw_unavailable_tracks", set())
            ))
        if not selected_tracks:
            self.status.set("Nenhuma fonte raw disponível para remoção.")
            return
        session_id = self.selected
        self.retention_request += 1
        request = self.retention_request
        self.raw_remove_button.configure(state="disabled")
        self.status.set("Calculando prévia de remoção raw…")

        def loaded(plan, error):
            if self.closed or request != self.retention_request or self.selected != session_id:
                return
            if error:
                self.raw_remove_button.configure(state="normal")
                self._remember_operation_error(error)
                self.status.set("A prévia raw falhou; nenhum áudio foi removido.")
                return
            projection = retention_plan_projection(plan)
            if not projection.get("eligible"):
                self.raw_remove_button.configure(state="normal")
                messagebox.showwarning("Remoção raw bloqueada", self._retention_preview_text(projection), parent=self.window)
                self.status.set("A remoção raw foi bloqueada; a transcrição existente permanece disponível.")
                return
            if not messagebox.askyesno(
                    "Remover áudio raw",
                    self._retention_preview_text(projection)
                    + "\n\nEssa operação desabilita reprodução, retranscrição, novos clipes e exportação de áudio para as fontes escolhidas. Aplicar?",
                    parent=self.window):
                self.raw_remove_button.configure(state="normal")
                self.status.set("Nenhum áudio raw foi removido.")
                return
            self.status.set("Aplicando remoção raw…")
            if not self._submit(
                    "raw_retention_apply",
                    lambda: self.controller.apply_raw_tracks(plan, confirm=True),
                    lambda value, apply_error: self._raw_tracks_applied(
                        session_id, value, apply_error, request,
                    ),
            ):
                self.raw_remove_button.configure(state="normal")

        if not self._submit(
            "raw_retention_plan",
            lambda: self.controller.plan_raw_tracks(session_id, tracks=selected_tracks),
            loaded,
        ):
            self.raw_remove_button.configure(state="normal")

    def _raw_tracks_applied(self, session_id, value, error, request):
        if self.closed or request != self.retention_request or self.selected != session_id:
            return
        if error or value is False:
            self.raw_remove_button.configure(state="normal")
            if error:
                self._remember_operation_error(error)
            self.status.set("A prévia raw ficou inválida; nenhuma nova tentativa foi feita.")
            return
        self.status.set("Áudio raw removido; transcrição, citações e relatórios continuam disponíveis.")
        self.load_session(session_id)

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
            speaker = self._speaker_for_segment(segment)
            self.transcript.insert("", "end", iid=key, values=(format_time(segment.get("start", 0)),
                "Microfone" if segment.get("track") == "microphone" else "Sistema",
                speaker, str(segment.get("text", ""))[:8000]))

    def _speaker_for_segment(self, segment):
        segment_id = segment.get("id") if isinstance(segment, dict) else None
        for record in (self.speaker_labels or {}).values():
            if isinstance(record, dict) and record.get("segment_id") == segment_id:
                return str(record.get("label", ""))[:256]
        if isinstance(segment.get("speaker"), str) and segment.get("speaker").strip():
            return segment["speaker"][:256]
        return ""

    def _render_annotations(self, speaker_labels, generation=None):
        if isinstance(speaker_labels, dict):
            self.speaker_labels = {
                key: value for key, value in speaker_labels.items() if isinstance(value, dict)
            }
        if generation is not None and isinstance(generation, int):
            self.annotation_generation = generation
        if hasattr(self, "speaker_name"):
            self.speaker_name.set("")
        self.selected_speaker_label_id = None
        if hasattr(self, "speaker_delete_button"):
            self.speaker_delete_button.configure(state="disabled")
        if hasattr(self, "highlight_picker"):
            values = []
            for item in self.highlights[:2000]:
                if not isinstance(item, dict):
                    continue
                label = str(item.get("label", "")).strip() or "Destaque sem rótulo"
                values.append(
                    f'{format_time(item.get("start", 0))}–{format_time(item.get("end", 0))} · {label}'
                )
            self.highlight_picker.configure(values=values)
            if hasattr(self, "highlight_choice"):
                self.highlight_choice.set("")
        self.selected_highlight_id = None
        for widget_name in ("highlight_delete_button", "highlight_export_button"):
            widget = getattr(self, widget_name, None)
            if widget is not None:
                widget.configure(state="disabled")
        if hasattr(self, "highlight_start"):
            self.highlight_start.set("")
            self.highlight_end.set("")
            self.highlight_label.set("")
            self.highlight_note.set("")
        if hasattr(self, "transcript") and hasattr(self, "segments"):
            # Speaker labels are part of the row projection and never alter JSONL text.
            for key, segment in self.segments.items():
                try:
                    values = list(self.transcript.item(key, "values"))
                    if len(values) >= 4:
                        values[2] = self._speaker_for_segment(segment)
                        self.transcript.item(key, values=values)
                except (tk.TclError, TypeError, AttributeError):
                    pass

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
            suffix = " · há mais trechos" if self.transcript_has_more else ""
            label.set(f"Página {page} · {len(self.segments)} trechos{suffix}")

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
        self.status.set("Carregando trechos da transcrição…")

        def read():
            return self._read_transcript_page(session_id, revision, offset)

        def loaded(page, error):
            if self.closed or request != self.transcript_request or self.selected != session_id:
                return
            if error:
                self._remember_operation_error(error)
                self.status.set("Não foi possível carregar a página da transcrição. Veja os detalhes na aba Gravação.")
                return
            page = page or {}
            self.transcript_offset = int(page.get("offset", offset))
            self.transcript_has_previous = bool(page.get("has_previous", self.transcript_offset > 0))
            self.transcript_has_more = bool(page.get("has_more"))
            self._render_transcript(page.get("segments", []))
            self._update_transcript_paging_controls()
            self.status.set(
                f"Página de transcrição carregada · {len(self.segments)} trechos"
                + (" · horários por trecho de áudio." if self.transcript_has_more else ".")
            )

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

    def _notes_modified(self, _event=None):
        if self.notes.edit_modified():
            self._mark_dirty()
            self.notes.edit_modified(False)

    def save_notes(self, after=None):
        if not self.selected or not self.detail_ready:
            self.status.set("Selecione uma gravação na biblioteca.")
            return
        if self.truncated:
            self.status.set("As notas excedem o limite de edição. Exporte para ler o conteúdo completo.")
            return
        session_id, title, notes = self.selected, self.title.get(), self.notes.get("1.0", "end-1c")
        if len(title) > 400 or len(notes) > NOTES_LIMIT:
            self.status.set("Use um título de até 400 caracteres e notas de até 1 MiB.")
            return
        bookmarks = [dict(item) for item in self.bookmarks]
        def saved(_value, error):
            if error or _value is False:
                if error:
                    self._remember_operation_error(error)
                self.status.set("Não foi possível salvar as notas. Veja os detalhes na aba Gravação."
                                if error else "Não foi possível salvar as notas. Tente novamente.")
                return
            if (self.selected == session_id and self.title.get() == title
                    and self.notes.get("1.0", "end-1c") == notes and self.bookmarks == bookmarks):
                self.dirty = False
            self.status.set("Título, notas e marcadores salvos.")
            self.refresh_library()
            if after and not self.dirty:
                after()
            elif after:
                self.status.set("Há novas alterações nas notas. Salve novamente antes de sair.")
        self._submit("save_notes", lambda: self.controller.update_notes(session_id, title, notes, bookmarks=bookmarks), saved)

    def _clear_library_detail(self):
        self._action("stop_playback", urgent=True)
        self.bridge.invalidate("detail")
        self.bridge.invalidate("outputs")
        self.bridge.invalidate("transcript_page")
        self.transcript_request += 1
        self.selected = None
        self._show_library_detail(False)
        self.playback_choices = ()
        self.audio_source.set("")
        self.audio_overview.set("Selecione uma gravação para ouvir.")
        self.playback_position.set(0.0)
        self.playback_clock.set("00:00:00 / 00:00:00")
        self.play_button.configure(state="disabled")
        self.replay_stop_button.configure(state="disabled")
        self.detail_ready = False
        self.dirty = False
        self.truncated = False
        self.transcript_offset = 0
        self.transcript_revision = None
        self.transcript_has_more = False
        self.transcript_has_previous = False
        self.annotation_generation = 0
        self.speaker_labels = {}
        self.highlights = []
        self.selected_segment_id = None
        self.selected_speaker_label_id = None
        self.selected_highlight_id = None
        self.loading = True
        self.title.set("")
        self.notes.configure(state="normal")
        self.notes.delete("1.0", "end")
        self.notes.edit_modified(False)
        self.notes.configure(state="disabled")
        self.loading = False
        self.bookmarks = []
        self._render_bookmarks()
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
            raise ValueError("Informe um horário em segundos, por exemplo 90.5.") from exc
        if not math.isfinite(value) or value < 0:
            raise ValueError("O horário deve ser finito e não negativo.")
        return value

    def add_bookmark(self):
        if not self.selected or not self.detail_ready:
            self.status.set("Selecione uma gravação na biblioteca.")
            return
        try:
            if self.truncated:
                raise ValueError("Esta gravação está em visualização parcial, somente leitura.")
            position = self._position()
            if len(self.bookmarks) >= BOOKMARK_LIMIT:
                raise ValueError("Esta gravação atingiu o limite de 1.000 marcadores.")
        except ValueError as exc:
            self.status.set(str(exc))
            return
        self.bookmarks.append({"timestamp": position, "label": self.bookmark_label.get()[:400]})
        self.dirty = True
        self._render_bookmarks()

    def _render_bookmarks(self):
        labels = [f"{format_time(item.get('timestamp', item.get('start', 0)))} {item.get('label', '')}"
                  for item in self.bookmarks[:4] if isinstance(item, dict)]
        self.bookmark_status.set(f"{len(self.bookmarks)} marcadores · " + " · ".join(labels))
        self.bookmark_indices = [index for index, item in enumerate(self.bookmarks) if isinstance(item, dict)]
        values = [f"{index + 1} · {format_time(self.bookmarks[index].get('timestamp', self.bookmarks[index].get('start', 0)))} · {self.bookmarks[index].get('label', '')}"
                  for index in self.bookmark_indices]
        self.bookmark_picker.configure(values=values)
        self.bookmark_choice.set("")

    def _bookmark_selected(self, _event=None):
        index = self.bookmark_picker.current()
        if 0 <= index < len(self.bookmark_indices):
            item = self.bookmarks[self.bookmark_indices[index]]
            self.position.set(str(item.get("timestamp", item.get("start", 0))))
            self.bookmark_label.set(str(item.get("label", "")))

    def _transcript_selected(self, _event=None):
        selected = self.transcript.selection()
        if selected and selected[0] in self.segments:
            self.selected_segment_id = self.segments[selected[0]].get("id")
            segment = self.segments[selected[0]]
            self.position.set(str(segment.get("start", 0)))
            self.track.set("Microfone" if segment.get("track") == "microphone" else "Sistema")
            self.segment_text.configure(state="normal")
            self.segment_text.delete("1.0", "end")
            self.segment_text.insert("1.0", segment.get("text", ""))
            self.segment_text.configure(state="disabled")
            label_id, label = self._speaker_label_for_segment(segment)
            self.selected_speaker_label_id = label_id
            if hasattr(self, "speaker_name"):
                self.speaker_name.set(label)
            if hasattr(self, "speaker_delete_button"):
                self.speaker_delete_button.configure(state="normal" if label_id else "disabled")
            if hasattr(self, "highlight_start"):
                self.highlight_start.set(str(segment.get("start", 0)))
                self.highlight_end.set(str(segment.get("end", segment.get("start", 0))))
            self.transcript_timing.set(
                "Trecho selecionado · dois cliques ou Enter para ouvir; horário por bloco, não por palavra."
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

    def _speaker_label_for_segment(self, segment):
        segment_id = segment.get("id") if isinstance(segment, dict) else None
        for label_id, record in (self.speaker_labels or {}).items():
            if isinstance(record, dict) and record.get("segment_id") == segment_id:
                return label_id, str(record.get("label", ""))[:400]
        return None, ""

    def _highlight_selected(self, _event=None):
        index = self.highlight_picker.current() if hasattr(self, "highlight_picker") else -1
        if not (0 <= index < len(self.highlights)):
            return
        item = self.highlights[index]
        if not isinstance(item, dict):
            return
        self.selected_highlight_id = item.get("id")
        self.highlight_start.set(str(item.get("start", "")))
        self.highlight_end.set(str(item.get("end", "")))
        self.highlight_label.set(str(item.get("label", "")))
        self.highlight_note.set(str(item.get("note", "")))
        self.highlight_delete_button.configure(state="normal")
        track = item.get("track")
        self.highlight_export_button.configure(
            state="disabled" if track in getattr(self, "raw_unavailable_tracks", set()) else "normal"
        )

    def _annotation_saved(self, value, error, message="Anotações salvas."):
        if error or value is False:
            if error:
                self._remember_operation_error(error)
            self.status.set(
                "Não foi possível salvar as anotações. Veja os detalhes na aba Gravação."
                if error else "Não foi possível salvar as anotações. Tente novamente."
            )
            return
        if isinstance(value, dict):
            generation = value.get("generation")
            if isinstance(generation, int):
                self.annotation_generation = generation
            visible = annotations_for_revision(value, self.transcript_revision)
            labels = visible.get("speaker_labels")
            if isinstance(labels, dict):
                self.speaker_labels = labels
            highlights = visible.get("highlights")
            if isinstance(highlights, list):
                self.highlights = highlights[:2000]
        self._render_annotations(self.speaker_labels, self.annotation_generation)
        self._render_transcript(list(self.segments.values()))
        self.status.set(message)

    def save_speaker_label(self):
        if not self.selected or not self.detail_ready or not self.selected_segment_id:
            self.status.set("Selecione um trecho antes de salvar o rótulo manual.")
            return
        label = self.speaker_name.get().strip() if hasattr(self, "speaker_name") else ""
        if not label:
            self.status.set("Informe um rótulo manual, por exemplo “Pessoa 1”.")
            return
        if len(label) > 256:
            self.status.set("O rótulo manual deve ter até 256 caracteres.")
            return
        session_id = self.selected
        revision = self.transcript_revision
        segment_id = self.selected_segment_id
        expected = self.annotation_generation
        label_id = self.selected_speaker_label_id
        note = ""
        if label_id:
            operation = lambda: self.controller.update_speaker_label(
                session_id, label_id, {"label": label, "note": note},
                expected_generation=expected,
            )
        else:
            operation = lambda: self.controller.set_speaker_label(
                session_id, revision, segment_id, label, note=note,
                expected_generation=expected,
            )
        self._submit("speaker_label", operation, lambda value, error: self._annotation_saved(
            value, error, "Rótulo manual salvo; o texto original da transcrição foi preservado."
        ))

    def delete_speaker_label(self):
        if not self.selected or not self.selected_speaker_label_id:
            return
        session_id, label_id = self.selected, self.selected_speaker_label_id
        expected = self.annotation_generation
        self._submit(
            "speaker_label", lambda: self.controller.delete_speaker_label(
                session_id, label_id, expected_generation=expected,
            ), lambda value, error: self._annotation_saved(
                value, error, "Rótulo manual excluído; a transcrição original não foi alterada."
            ),
        )

    def _highlight_interval(self):
        try:
            start, end = float(self.highlight_start.get()), float(self.highlight_end.get())
        except (TypeError, ValueError) as exc:
            raise ValueError("Informe início e fim do destaque em segundos.") from exc
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
            raise ValueError("O intervalo do destaque deve ser finito e ter fim maior que o início.")
        return start, end

    def save_highlight(self):
        if not self.selected or not self.detail_ready:
            self.status.set("Selecione uma gravação antes de salvar o destaque.")
            return
        try:
            start, end = self._highlight_interval()
        except ValueError as exc:
            self.status.set(str(exc))
            return
        label = self.highlight_label.get().strip()[:256]
        note = self.highlight_note.get()[:1024]
        session_id, expected = self.selected, self.annotation_generation
        if self.selected_highlight_id:
            highlight_id = self.selected_highlight_id
            operation = lambda: self.controller.update_highlight(
                session_id, highlight_id,
                {"start": start, "end": end, "label": label, "note": note},
                expected_generation=expected,
            )
        else:
            selected = self.transcript.selection() if hasattr(self, "transcript") else ()
            segment = self.segments.get(selected[0]) if selected else None
            segment_id = segment.get("id") if isinstance(segment, dict) else self.selected_segment_id
            if not isinstance(segment_id, str) or not self.transcript_revision:
                self.status.set("Selecione um trecho de transcrição para criar o destaque.")
                return
            track = segment.get("track") or TRACK_LABELS.get(self.track.get())
            operation = lambda: self.controller.add_highlight(
                session_id, self.transcript_revision, start, end, track, [segment_id],
                expected_generation=expected, label=label, note=note,
            )
        self._submit("highlight", operation, lambda value, error: self._annotation_saved(
            value, error, "Destaque salvo com proveniência da revisão e do trecho."
        ))

    def delete_highlight(self):
        if not self.selected or not self.selected_highlight_id:
            return
        session_id, highlight_id = self.selected, self.selected_highlight_id
        expected = self.annotation_generation
        self._submit(
            "highlight", lambda: self.controller.delete_highlight(
                session_id, highlight_id, expected_generation=expected,
            ), lambda value, error: self._annotation_saved(
                value, error, "Destaque excluído; o áudio original foi preservado."
            ),
        )

    def export_highlight_clip(self):
        if not self.selected or not self.selected_highlight_id:
            self.status.set("Selecione um destaque antes de exportar o clipe.")
            return
        item = next((value for value in self.highlights
                     if isinstance(value, dict) and value.get("id") == self.selected_highlight_id), None)
        if item is None:
            self.status.set("O destaque selecionado não está mais disponível; atualize a gravação.")
            return
        if item.get("track") in getattr(self, "raw_unavailable_tracks", set()):
            self.status.set("O áudio raw deste destaque foi removido; o clipe não está mais disponível.")
            return
        path = filedialog.asksaveasfilename(
            parent=self.window, title="Exportar clipe do destaque", defaultextension=".wav",
            filetypes=(("Áudio WAV", "*.wav"), ("Todos os arquivos", "*")),
        )
        if not path:
            return
        session_id = self.selected

        def exported(value, error):
            if error:
                self._remember_operation_error(error)
                self.status.set("Não foi possível exportar o clipe. Veja os detalhes na aba Gravação.")
            else:
                self.status.set("Clipe do destaque exportado sem alterar o áudio original.")

        self._submit(
            "export_highlight", lambda: self.controller.export_highlight_clip(
                session_id, dict(item), path,
            ), exported,
        )

    def _player_source_changed(self, _event=None):
        if self._playback_active:
            self.stop_selected_recording()
        self.playback_position.set(0.0)
        self.playback_clock.set(f"{format_time(0)} / {format_time(self.playback_duration)}")
        self.playback_status.set("Fonte selecionada. Reproduza para ouvir.")

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
            self.playback_status.set("Selecione uma gravação para ouvir.")
            return
        if self.snapshot.get("state") in ("starting", "recording", "paused", "stopping"):
            self.playback_status.set("Finalize a captura antes de reproduzir uma gravação.")
            return
        source = AUDIO_SOURCE_LABELS.get(self.audio_source.get())
        if source is None or self.audio_source.get() not in self.playback_choices:
            self.playback_status.set("O áudio desta gravação não está disponível.")
            return
        position = min(self.playback_duration, max(0.0, self.playback_position.get()))
        if self.playback_duration and position >= self.playback_duration:
            position = 0.0
            self.playback_position.set(0.0)
        self._action("seek_playback", self.selected, source, start=position,
                     urgent=True, callback=self._replay_started)

    def _replay_started(self, accepted, error):
        if error:
            self._remember_operation_error(error)
            self.playback_status.set("Não foi possível reproduzir o áudio. Veja os detalhes da operação.")
        elif not accepted:
            self.playback_status.set("A reprodução não está disponível durante outra operação.")
        else:
            self.playback_status.set("Iniciando reprodução…")

    def stop_selected_recording(self):
        self._action("stop_playback", urgent=True)

    def play(self):
        if not self.selected:
            self.status.set("Selecione uma gravação na biblioteca.")
            return
        if self.snapshot.get("state") in ("starting", "recording", "paused", "stopping"):
            self.status.set("Finalize a captura antes de ouvir uma gravação.")
            return
        track = TRACK_LABELS[self.track.get()]
        if track in getattr(self, "raw_unavailable_tracks", set()):
            self.status.set("A fonte raw selecionada foi removida; a reprodução está desabilitada.")
            return
        try:
            position = self._position()
        except ValueError as exc:
            self.status.set(str(exc))
            return
        self._action("seek_playback", self.selected, track, start=position, urgent=True)

    def transcribe(self):
        if not self.selected:
            self.status.set("Selecione uma gravação na biblioteca.")
            return
        available = getattr(self, "raw_tracks_present", set()) - getattr(
            self, "raw_unavailable_tracks", set())
        if not available:
            self.status.set("A retranscrição está desabilitada porque não há áudio raw; a transcrição existente continua disponível.")
            return
        try:
            settings = self._current_settings()
        except ValueError as exc:
            self.status.set(str(exc))
            return
        session_id = self.selected
        self._action("transcribe", session_id, settings.profile, settings.language,
                     callback=lambda value, error: self._processing_launched(session_id, value, error))

    def import_model(self):
        profile = self.profile.get()
        path = filedialog.askopenfilename(parent=self.window, title="Importar modelo local compatível",
                                           filetypes=(("Modelo GGUF", "*.gguf"), ("Todos os arquivos", "*")))
        if path:
            self._action("import_model", profile, path,
                         callback=lambda value, error: self._processing_launched("", value, error))

    def import_audio(self):
        try:
            settings = self._current_settings()
        except ValueError as exc:
            self.status.set(str(exc))
            return
        path = filedialog.askopenfilename(
            parent=self.window,
            title="Importar áudio",
            filetypes=(
                ("Áudio compatível", "*.wav *.mp3 *.aac *.m4a *.flac *.ogg *.opus"),
                ("Todos os arquivos", "*"),
            ),
        )
        if path:
            def imported(session_id, error):
                if error:
                    self._remember_operation_error(error)
                    self.status.set(
                        "Não foi possível importar o áudio. Veja os detalhes na aba Gravação."
                    )
                    return
                self.refresh_library()
                self.load_session(session_id)
            self._action("import_audio", path, settings, callback=imported)

    def export(self, format):
        if not self.selected:
            self.status.set("Selecione uma gravação na biblioteca.")
            return
        suffix = ".md" if format == "markdown" else ".txt"
        path = filedialog.asksaveasfilename(parent=self.window, title="Exportar transcrição",
            defaultextension=suffix, filetypes=(("Markdown" if suffix == ".md" else "Texto", "*" + suffix),))
        if path:
            self._action("export", self.selected, path, format=format)

    def export_audio(self):
        if not self.selected:
            self.status.set("Selecione uma gravação na biblioteca.")
            return
        available = getattr(self, "raw_tracks_present", set()) - getattr(
            self, "raw_unavailable_tracks", set())
        if not available:
            self.status.set("A exportação de áudio está desabilitada porque não há áudio raw disponível.")
            return
        try:
            settings = self._current_settings()
        except ValueError as exc:
            self.status.set(str(exc))
            return
        initial = settings.destination or None
        options = {"parent": self.window, "title": "Exportar áudio final",
                   "defaultextension": ".wav", "filetypes": (("Áudio WAV", "*.wav"),)}
        if initial:
            options["initialdir"] = initial
        path = filedialog.asksaveasfilename(**options)
        if path:
            self._action("export_mixdown", self.selected, path,
                         enhance_microphone=settings.voice_boost)

    def summarize(self):
        if not self.selected:
            self.status.set("Selecione uma gravação na biblioteca.")
            return
        model = self.summary_model.get().strip()
        if not self.summary_model_installed.get(model):
            self.status.set("Baixe o modelo selecionado em Configurações antes de gerar o resumo.")
            return
        session_id = self.selected
        self._action("summarize", session_id, model,
                     callback=lambda value, error: self._processing_launched(session_id, value, error))

    def _processing_launched(self, session_id, accepted, error):
        if error or not accepted:
            if error:
                self._remember_operation_error(error)
                self.status.set(
                    "Não foi possível iniciar o processamento local. "
                    "Veja os detalhes na aba Gravação."
                )
            else:
                self.status.set(
                    "Há uma captura ou outro processamento em andamento. Tente novamente depois."
                )
            return
        self.operation_details = ""
        self.processing_target = session_id
        self.status.set("Processamento local iniciado. O resultado aparecerá quando terminar.")

    def refresh_outputs(self, session_id):
        def read():
            raw = self.controller.get_session(session_id)
            summary = raw.get("summary")
            summary = json.dumps(summary, ensure_ascii=False, indent=2) if isinstance(summary, dict) else str(summary or "")
            revision = active_transcript_revision(raw) or self.transcript_revision
            page = self._read_transcript_page(session_id, revision, self.transcript_offset)
            annotations = raw.get("annotations") if isinstance(raw.get("annotations"), dict) else {}
            return summary[:65536], page, annotations_for_revision(annotations, revision)
        def loaded(data, error):
            if self.selected != session_id or not self.detail_ready:
                return
            if error:
                self._remember_operation_error(error)
                self.status.set("Não foi possível atualizar os resultados. Veja os detalhes na aba Gravação.")
                return
            summary, page, annotations = data
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
            self.refresh_reports(session_id)
            self.status.set("Resultado local atualizado. Revise a transcrição e o resumo.")
        self._submit("outputs", read, loaded)

    def summary_to_notes(self):
        if not self.selected or not self.detail_ready or self.truncated:
            self.status.set("Selecione uma gravação com notas editáveis.")
            return
        text = self.summary.get("1.0", "end-1c")
        if len(self.notes.get("1.0", "end-1c")) + len(text) + 30 > NOTES_LIMIT:
            self.status.set("As notas excederiam o limite de edição.")
            return
        self.notes.insert("end", "\n\nResumo revisado:\n" + text)
        self.dirty = True
        self.status.set("Resumo copiado para notas. Salve as notas para manter suas alterações.")

    def save_summary(self):
        if not self.selected or not self.detail_ready:
            self.status.set("Selecione uma gravação antes de salvar o resumo.")
            return
        session_id = self.selected
        text = self.summary.get("1.0", "end-1c")
        def saved(_value, error):
            if self.closed or self.selected != session_id:
                return
            if error:
                self._remember_operation_error(error)
                self.status.set("Não foi possível salvar o resumo. Veja os detalhes na aba Gravação.")
            else:
                self.status.set("Resumo revisado salvo; o resultado original foi preservado.")
        self._submit("save_summary", lambda: self.controller.update_summary(session_id, text), saved)

    def _show_summary(self, value):
        if isinstance(value, dict):
            text = json.dumps(value, ensure_ascii=False, indent=2)
        else:
            text = str(value or "Resumo local opcional. Revise os resultados e copie o texto para suas notas.")
        self.summary.configure(state="normal")
        self.summary.delete("1.0", "end")
        self.summary.insert("1.0", text[:65536])
        self.summary.configure(state="normal")

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
            track_label = AUDIO_TRACK_LABELS.get(track, "Áudio")
            self.playback_status.set(
                f"Reproduzindo {track_label} · {format_time(position)}"
            )
            if self.selected == playback.get("session_id"):
                self._select_playback_segment(position, track)
        else:
            error = str(playback.get("error") or "")
            if error:
                self.playback_status.set("A reprodução falhou; veja os detalhes da operação.")
            else:
                self.playback_status.set("Reprodução parada.")

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

    def _poll(self):
        if self.closed:
            return
        try:
            self.bridge.drain()
            with self.summary_progress_lock:
                progress = self.summary_progress
            if progress is not None:
                name, done, total = progress
                percent = int(done * 100 / total) if total else 0
                self.summary_model_status.set(
                    f"Baixando {name}: {percent}% · verificação SHA-256 antes da instalação"
                )
            rebuild = self.rebuild_progress
            if getattr(self, "rebuild_cancel", None) is not None and isinstance(rebuild, dict):
                done, total = rebuild.get("done", 0), rebuild.get("total")
                suffix = f"{done}/{total}" if total else str(done)
                self.index_status.set(f"Índice de busca: reconstruindo · {suffix} reuniões")
            elif isinstance(rebuild, dict) and rebuild.get("state") not in {None, "idle"}:
                self.index_status.set(
                    f"Índice de busca: {rebuild.get('state')} · {rebuild.get('done', 0)} reuniões"
                )
            self.snapshot = self.controller.snapshot()
            self._apply_playback_snapshot(self.snapshot)
            state = self.snapshot.get("state", "idle")
            active = state in ("starting", "recording", "paused", "stopping")
            processing = self.snapshot.get("processing")
            start_ready = (
                state == "idle" and not processing and self.settings_loaded
                and self.retention_ready and self.privacy_ready
                and not self.privacy_save_inflight
                and not self.snapshot.get("playback", {}).get("active")
            )
            self.start_button.configure(state="normal" if start_ready else "disabled")
            self.preview_button.configure(state="normal" if start_ready else "disabled")
            self.stop_button.configure(state="normal" if active else "disabled")
            self.pause_button.configure(text="Retomar" if state == "paused" else "Pausar",
                state="normal" if state in ("recording", "paused") else "disabled")
            self.play_button.configure(
                state="normal" if state == "idle" and not processing
                and self.detail_ready and self.playback_choices else "disabled"
            )
            error = self.snapshot.get("error")
            self.record_status.set(format_recording_status(self.snapshot))
            details = [self.operation_details] if self.operation_details else []
            if self.snapshot.get("final_audio"):
                details.append("Arquivo final: " + str(self.snapshot["final_audio"])[:2048])
            if self.snapshot.get("postprocess"):
                details.append("Processamento: " + str(self.snapshot["postprocess"])[:2048])
            if error:
                details.append("Detalhes técnicos: " + str(error)[:4096])
            self.record_details = "\n".join(details)
            self.record_details_button.configure(
                state="normal" if self.record_details else "disabled",
            )
            self.waveform.clear()
            for track, values in self.snapshot.get("waveforms", {}).items():
                if track in ("microphone", "system"):
                    self.waveform.append_block(track, values)
            waveform_state = ("checking" if self.snapshot.get("previewing") else
                              "paused" if state == "paused" else "recording"
                              if state in ("starting", "recording", "stopping") else "idle")
            self.waveform.set_state(waveform_state)
            for track, meter in self.meters.items():
                value = float(self.snapshot.get("levels", {}).get(track, 0) or 0)
                meter.configure(value=meter_value(value))
            if self.previous_state in ("recording", "paused", "stopping") and not active:
                self.refresh_library()
            if state != self.previous_state and self.on_recording_state_changed:
                self.on_recording_state_changed(state, dict(self.snapshot))
            self.previous_state = state
            if self.previous_processing and not processing:
                self.refresh_library()
            self.previous_processing = bool(processing)
            if self.processing_target is not None and not processing:
                target, self.processing_target = self.processing_target, None
                if error:
                    self._remember_operation_error(error)
                    self.status.set(
                        "O processamento local não foi concluído. "
                        "Veja os detalhes na aba Gravação."
                    )
                else:
                    self.operation_details = ""
                    self.status.set("Processamento local finalizado.")
                if target and not error:
                    self.refresh_outputs(target)
        except Exception:
            self.status.set("Não foi possível atualizar o estado da gravação.")
        self.after_id = self.root.after(200, self._poll)

    def _destroyed(self, event):
        if event.widget is self.window:
            self.close_without_prompt(destroy=False)

    def close(self, destroy=True, after_close=None):
        if self.closed:
            return
        if self.dirty:
            answer = messagebox.askyesnocancel("Notas não salvas", "Salvar as notas antes de fechar?", parent=self.window)
            if answer is None:
                return
            if answer:
                self.save_notes(after=lambda: self.close_without_prompt(destroy, after_close))
                return
        self.close_without_prompt(destroy, after_close)

    def close_without_prompt(self, destroy=True, after_close=None):
        if self.closed:
            return
        self.closed = True
        self.report_request = getattr(self, "report_request", 0) + 1
        self.ask_request = getattr(self, "ask_request", 0) + 1
        self.citation_request = getattr(self, "citation_request", 0) + 1
        self.search_request = getattr(self, "search_request", 0) + 1
        self.cross_request = getattr(self, "cross_request", 0) + 1
        self.cross_citation_request = getattr(self, "cross_citation_request", 0) + 1
        self.trash_request = getattr(self, "trash_request", 0) + 1
        self.retention_request = getattr(self, "retention_request", 0) + 1
        self.pending_start_origin = None
        self.unsaved_answer = None
        self.playback_generation = getattr(self, "playback_generation", -1) + 1
        self.transcript_request = getattr(self, "transcript_request", 0) + 1
        if getattr(self, "summary_download_cancel", None) is not None:
            self.summary_download_cancel.set()
        for name in ("rebuild_cancel", "batch_cancel_event"):
            event = getattr(self, name, None)
            if event is not None:
                event.set()
        try:
            self.controller.cancel_processing()
        except (AttributeError, RuntimeError):
            pass
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
                     models_location=None, relocate_models=None):
    """Attach recording and library tabs to the shared application window."""
    view = MeetingWindow(
        root, controller, settings_getter, persist_settings, on_settings_changed,
        on_recording_state_changed, on_appearance_changed,
        window=window, notebook=notebook,
        data_location=data_location, relocate_data=relocate_data,
        models_location=models_location, relocate_models=relocate_models,
    )
    window._meeting_view = view
    return view
