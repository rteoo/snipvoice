"""Shared-root meeting workspace with bounded workers and Tk-only updates."""

from itertools import islice
import json
import math
import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from clipboard_support import Clipboard
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
from voice_catalog import available_languages, selectable_catalog
from voice_hotkey import DEFAULT_COMMAND_HOTKEY, DEFAULT_DICTATION_HOTKEY


PAGE_SIZE = 50
TRANSCRIPT_LIMIT = 500
TRANSCRIPT_PAGE_SIZE = 100
NOTES_LIMIT = 1024 * 1024
BOOKMARK_LIMIT = 1000
MAX_ANSWER_CHARS = 4_000
REPORT_HISTORY_LIMIT = 500
STATE_LABELS = {"idle": "Pronto", "starting": "Iniciando", "recording": "Gravando",
                "paused": "Pausado", "stopping": "Finalizando", "postprocessing": "Processando",
                "completed": "Concluído",
                "stopped": "Parado", "interrupted": "Interrompido", "failed": "Falha",
                "partial": "Parcial", "teardown_blocked": "Recursos ainda em uso",
                "unavailable": "Recursos ainda em uso"}
STATUS_FILTERS = {"Todos": "", "Concluídos": "completed", "Parciais": "partial",
                  "Falhas": "failed", "Interrompidos": "interrupted", "Gravando": "recording"}
PROFILE_LABELS = {"balanced": "Equilibrado · Parakeet TDT", "compact": "Compacto · Qwen 0.6B",
                  "accuracy": "Precisão · Qwen 1.7B", "streaming": "Transcrição contínua"}
LANGUAGE_LABELS = {"auto": "Automático", "pt-BR": "Português (Brasil)", "en-US": "Inglês (Estados Unidos)"}
TRACK_LABELS = {"Microfone": "microphone", "Sistema": "system"}
SUMMARY_LABELS = {entry["id"]: f'{entry["name"]} · {entry["parameters"]}'
                  for entry in summary_catalog()}


def format_time(seconds):
    seconds = float(seconds or 0)
    if not math.isfinite(seconds) or seconds < 0:
        seconds = 0
    whole = int(seconds)
    return f"{whole // 3600:02d}:{whole // 60 % 60:02d}:{whole % 60:02d}"


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


class MeetingWindow:
    def __init__(self, root, controller, settings_getter, persist_settings,
                 on_settings_changed=None, *, window=None, notebook=None):
        self.root, self.controller = root, controller
        self.settings_getter, self.persist_settings = settings_getter, persist_settings
        self.on_settings_changed = on_settings_changed
        if (window is None) != (notebook is None):
            raise ValueError("window and notebook must be supplied together")
        self.embedded = window is not None
        self.window = window or tk.Toplevel(root)
        self.notebook = notebook
        if not self.embedded:
            self.window.title("Gravações e reuniões")
            self.window.geometry("1120x820")
            self.window.minsize(920, 700)
        self.ui = ui_theme.bind(self.window)
        self.window.configure(bg=self.ui.surface)
        self.bridge = BackgroundBridge()
        self.closed = False
        self.after_id = None
        self.raw_settings = {}
        self.settings = resolve_meeting_settings({})
        self.devices = []
        self.options = {}
        self.offset = 0
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
        self.playback_generation = -1
        # Report/Q&A requests carry their own generation so a late worker
        # result can never replace a different meeting's selected output.
        self.report_request = 0
        self.ask_request = 0
        self.citation_request = 0
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
        self.refresh_devices()
        self.refresh_library()
        self._poll()

    def _label(self, parent, text, **kwargs):
        kwargs.setdefault("font", self.ui.font())
        kwargs.setdefault("bg", self.ui.surface)
        kwargs.setdefault("fg", self.ui.text)
        return tk.Label(parent, text=text, **kwargs)

    def _button(self, parent, text, command, accent=False, danger=False):
        return tk.Button(parent, text=text, command=command, font=self.ui.font(),
                         **self.ui.button_colors(accent=accent, danger=danger),
                         **self.ui.button_chrome(compact=True))

    def _entry(self, parent, variable, width=30):
        return tk.Entry(parent, textvariable=variable, width=width, font=self.ui.font(),
                        **self.ui.entry_colors())

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
        ui_theme.apply_ttk_theme(style)
        ui_theme.configure_manager_styles(style, self.ui)
        style.configure("Meeting.TFrame", background=self.ui.surface)
        style.configure(
            "Meeting.Treeview",
            background=self.ui.card,
            fieldbackground=self.ui.card,
            foreground=self.ui.text,
            rowheight=self.ui.tree_row_height,
            font=self.ui.font(),
            borderwidth=0,
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
            "Escolha as fontes locais, confira os dispositivos e controle a gravação.",
        )
        self._page_header(
            self.library_tab,
            "Biblioteca",
            "Revise gravações, edite notas e gere transcrições e resumos locais.",
        )
        self._page_header(
            self.settings_tab,
            "Configurações",
            "Gerencie os modelos locais usados para transcrição e resumo sem enviar conteúdo à nuvem.",
        )
        self.status = tk.StringVar(self.window, "Carregando configurações…")
        for tab in (self.recording_tab, self.library_tab, self.settings_tab):
            self._label(
                tab, "", textvariable=self.status, anchor="w", wraplength=940,
                fg=self.ui.text_muted, font=self.ui.font(9),
            ).pack(fill="x", padx=self.ui.space_lg, pady=(0, self.ui.space_sm))
        recording = ttk.Frame(self.recording_tab, padding=(16, 0, 16, 16), style="Meeting.TFrame")
        recording.pack(fill="both", expand=True)
        recording.columnconfigure(0, weight=4, minsize=360)
        recording.columnconfigure(1, weight=7, minsize=440)
        recording.rowconfigure(0, weight=1)
        library = ttk.Frame(self.library_tab, padding=(16, 0, 16, 16), style="Meeting.TFrame")
        library.pack(fill="both", expand=True)
        settings_view = ttk.Frame(self.settings_tab, style="Meeting.TFrame")
        settings_view.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        settings_view.columnconfigure(0, weight=1)
        settings_view.rowconfigure(0, weight=1)
        settings_canvas = tk.Canvas(
            settings_view, background=self.ui.surface, highlightthickness=0,
            borderwidth=0,
        )
        settings_scrollbar = ttk.Scrollbar(
            settings_view, orient="vertical", command=settings_canvas.yview,
        )
        settings_canvas.configure(yscrollcommand=settings_scrollbar.set)
        settings_canvas.grid(row=0, column=0, sticky="nsew")
        settings_scrollbar.grid(row=0, column=1, sticky="ns", padx=(self.ui.space_sm, 0))
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
        self.recording_defaults_parent = self._card(settings_content)
        self.recording_defaults_parent.pack(fill="x", pady=(0, self.ui.space_md))
        self.transcription_models_parent = tk.Frame(
            settings_content, bg=self.ui.surface,
        )
        self.transcription_models_parent.pack(fill="x", pady=(0, self.ui.space_md))
        summary = ttk.Frame(settings_content, style="Meeting.TFrame")
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
        settings_card = self._card(recording, pady=card_pady)
        settings_card.grid(row=0, column=0, sticky="nsew", padx=(0, self.ui.space_md))
        self._label(
            settings_card, "Detalhes e automação",
            bg=self.ui.card, fg=self.ui.text_strong, font=self.ui.font(11, "bold"),
        ).grid(row=0, column=0, sticky="w", pady=(0, self.ui.space_sm))
        rows = [("Título", self._entry(settings_card, self.record_title))]
        destination_row = tk.Frame(settings_card, bg=self.ui.card)
        destination_entry = self._entry(destination_row, self.destination_label)
        destination_entry.configure(state="readonly")
        destination_entry.pack(side="left", fill="x", expand=True)
        self._button(destination_row, "Escolher…", self.choose_destination).pack(
            side="left", padx=(self.ui.space_sm, 0))
        self._button(destination_row, "Usar padrão", self.use_default_destination).pack(
            side="left", padx=(self.ui.space_sm, 0))
        rows.append(("Pasta dos arquivos finais", destination_row))
        next_row = 1
        for label, widget in rows:
            self._label(settings_card, label, anchor="w", bg=self.ui.card).grid(
                row=next_row, column=0, sticky="w", pady=(row_pady, 0),
            )
            widget.grid(row=next_row + 1, column=0, sticky="ew", pady=(0, row_pady))
            next_row += 2
        settings_card.columnconfigure(0, weight=1)
        automation = tk.Frame(settings_card, bg=self.ui.card)
        self.auto_transcribe_check = tk.Checkbutton(
            automation, text="Transcrever automaticamente", variable=self.auto_transcribe,
            command=self._automation_toggled, font=self.ui.font(),
            **self.ui.checkbutton_colors(self.ui.card),
        )
        self.auto_transcribe_check.pack(anchor="w", fill="x")
        self.auto_summary_check = tk.Checkbutton(
            automation, text="Resumir após transcrever", variable=self.auto_summary,
            command=self._automation_toggled, font=self.ui.font(),
            **self.ui.checkbutton_colors(self.ui.card),
        )
        self.auto_summary_check.pack(anchor="w", fill="x", padx=(self.ui.space_xl, 0))
        self.voice_boost_check = tk.Checkbutton(
            automation, text="Melhorar a voz do microfone", variable=self.voice_boost,
            font=self.ui.font(), **self.ui.checkbutton_colors(self.ui.card),
        )
        self.voice_boost_check.pack(anchor="w", fill="x", pady=(self.ui.space_xs, 0))
        automation_row = next_row
        self._label(settings_card, "Depois de gravar", anchor="w", bg=self.ui.card).grid(
            row=automation_row, column=0, sticky="w", pady=(row_pady, 0))
        automation.grid(row=automation_row + 1, column=0, sticky="ew", pady=(0, row_pady))
        note_row = automation_row + 2
        self._label(
            settings_card,
            "Configurações valem para a próxima gravação. A pasta local padrão "
            "fica ao lado da biblioteca.\nO sistema inclui todos os sons do "
            "dispositivo escolhido. Use fones para reduzir duplicação acústica.",
            justify="left", anchor="w", wraplength=360, bg=self.ui.card,
            fg=self.ui.text_muted, font=self.ui.font(9),
        ).grid(row=note_row, column=0, sticky="ew", pady=note_pady)
        commands = tk.Frame(settings_card, bg=self.ui.card)
        commands.grid(row=note_row + 1, column=0, sticky="w")
        self._button(commands, "Atualizar dispositivos", self.refresh_devices).pack(side="left", padx=(0, 8))
        self._button(commands, "Salvar como padrão", self.save_settings).pack(side="left")
        activity = self._card(recording, pady=card_pady)
        activity.grid(row=0, column=1, sticky="nsew")
        self._label(
            activity, "Controles da gravação", bg=self.ui.card, fg=self.ui.text_strong,
            font=self.ui.font(11, "bold"),
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, self.ui.space_sm))
        activity.columnconfigure(1, weight=1)
        activity.rowconfigure(2, weight=1)
        sources = tk.Frame(activity, bg=self.ui.card)
        sources.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, self.ui.space_sm))
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
            self.source_checks[track] = control
            self.endpoint_boxes[track] = combo
        self.waveform = MeetingWaveform(
            activity, theme=self.ui, height=170,
            track_labels={"microphone": "Microfone", "system": "Áudio do sistema"},
            state_labels={"idle": "Pronto", "recording": "Gravando", "paused": "Pausado"},
        )
        self.waveform.grid(row=2, column=0, columnspan=2, sticky="nsew",
                           pady=(0, self.ui.space_sm))
        transport = tk.Frame(activity, bg=self.ui.card)
        transport.grid(row=3, column=0, columnspan=2, sticky="w")
        self.start_button = self._button(transport, "Iniciar gravação", self.start, accent=True)
        self.start_button.pack(side="left", padx=(0, 8))
        self.pause_button = self._button(transport, "Pausar", self.pause_resume)
        self.pause_button.pack(side="left", padx=(0, 8))
        self.stop_button = self._button(transport, "Parar e preservar", self.stop)
        self.stop_button.pack(side="left")
        self.record_status = tk.StringVar(self.window, "Pronto · 00:00:00")
        status_row = tk.Frame(activity, bg=self.ui.card)
        status_row.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(10, 4))
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
        for row, (track, label) in enumerate(meters, 5):
            self._label(activity, label, anchor="w", bg=self.ui.card).grid(
                row=row, column=0, sticky="w", padx=(0, 18), pady=6,
            )
            meter = ttk.Progressbar(activity, maximum=1.0)
            meter.grid(row=row, column=1, sticky="ew")
            self.meters[track] = meter
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
        search_row = self._card(parent, pady=self.ui.space_sm)
        search_row.pack(fill="x", pady=(0, self.ui.space_md))
        self.query = tk.StringVar(self.window)
        self._label(search_row, "Buscar", bg=self.ui.card,
                    font=self.ui.font(9, "bold")).pack(side="left", padx=(0, 8))
        search = self._entry(search_row, self.query)
        search.pack(side="left", fill="x", expand=True)
        search.bind("<Return>", lambda _event: self.search())
        self._button(search_row, "Buscar", self.search).pack(side="left", padx=8)
        self.status_filter = tk.StringVar(self.window, "Todos")
        filters = ttk.Combobox(search_row, textvariable=self.status_filter, state="readonly",
                               values=list(STATUS_FILTERS), width=14)
        filters.pack(side="left", padx=(0, 8))
        filters.bind("<<ComboboxSelected>>", lambda _event: self.search())
        self._button(search_row, "Importar áudio…", self.import_audio).pack(side="left")
        panes = ttk.Panedwindow(parent, orient="horizontal")
        panes.pack(fill="both", expand=True)
        left = ttk.Frame(panes, style="Meeting.TFrame")
        right = ttk.Frame(panes, style="Meeting.TFrame")
        panes.add(left, weight=1)
        panes.add(right, weight=3)
        self.sessions = ttk.Treeview(left, columns=("title", "status"), show="headings", selectmode="browse",
                                     style="Meeting.Treeview", height=14)
        self.sessions.heading("title", text="Gravação")
        self.sessions.heading("status", text="Estado")
        self.sessions.column("title", width=170)
        self.sessions.column("status", width=85)
        scrollbar = ttk.Scrollbar(left, orient="vertical", command=self.sessions.yview)
        self.sessions.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.sessions.pack(fill="both", expand=True)
        self.sessions.bind("<<TreeviewSelect>>", self._selection_changed)
        pages = ttk.Frame(left, style="Meeting.TFrame")
        pages.pack(fill="x", pady=8)
        self.previous_button = self._button(pages, "Anterior", lambda: self.change_page(-1))
        self.previous_button.pack(side="left")
        self.next_button = self._button(pages, "Próxima", lambda: self.change_page(1))
        self.next_button.pack(side="left", padx=8)
        self.page_label = tk.StringVar(self.window, "Página 1")
        self._label(left, "", textvariable=self.page_label).pack(anchor="w")
        title_row = ttk.Frame(right, style="Meeting.TFrame")
        title_row.pack(fill="x", padx=(12, 0))
        self.title = tk.StringVar(self.window)
        self.title.trace_add("write", self._mark_dirty)
        self._label(title_row, "Título").pack(side="left", padx=(0, 8))
        self._entry(title_row, self.title).pack(side="left", fill="x", expand=True)
        self._button(title_row, "Salvar notas", self.save_notes).pack(side="left", padx=(8, 0))
        self.delete_button = self._button(title_row, "Excluir gravação", self.delete_selected, danger=True)
        self.delete_button.configure(state="disabled")
        self.delete_button.pack(side="left", padx=(8, 0))
        self._label(right, "Notas manuais", anchor="w").pack(fill="x", padx=12, pady=(12, 4))
        self.notes = tk.Text(right, height=5, wrap="word", undo=True, font=self.ui.font(), **self.ui.text_colors())
        self.notes.pack(fill="both", expand=True, padx=(12, 0))
        self.notes.bind("<<Modified>>", self._notes_modified)
        bookmark_row = ttk.Frame(right, style="Meeting.TFrame")
        bookmark_row.pack(fill="x", padx=(12, 0), pady=8)
        self.position = tk.StringVar(self.window, "0")
        self.bookmark_label = tk.StringVar(self.window)
        self._label(bookmark_row, "Segundos").pack(side="left")
        self._entry(bookmark_row, self.position, 9).pack(side="left", padx=6)
        self._entry(bookmark_row, self.bookmark_label, 18).pack(side="left", fill="x", expand=True)
        self._button(bookmark_row, "Adicionar marcador", self.add_bookmark).pack(side="left", padx=(6, 0))
        self.bookmark_status = tk.StringVar(self.window)
        self._label(right, "", textvariable=self.bookmark_status, anchor="w",
                    wraplength=600).pack(fill="x", padx=12)
        self.bookmark_choice = tk.StringVar(self.window)
        self.bookmark_picker = ttk.Combobox(right, textvariable=self.bookmark_choice, state="readonly")
        self.bookmark_picker.pack(fill="x", padx=(12, 0), pady=4)
        self.bookmark_picker.bind("<<ComboboxSelected>>", self._bookmark_selected)
        transcript_header = ttk.Frame(right, style="Meeting.TFrame")
        transcript_header.pack(fill="x", padx=12, pady=(10, 4))
        self._label(
            transcript_header, "Transcrição · horários por trecho de áudio", anchor="w",
        ).pack(side="left")
        self.transcript_timing = tk.StringVar(self.window, "Os horários indicam trechos de áudio, não palavras.")
        self._label(
            transcript_header, "", textvariable=self.transcript_timing, anchor="e",
            fg=self.ui.text_muted,
        ).pack(side="right")
        self.transcript = ttk.Treeview(
            right, columns=("time", "track", "speaker", "text"), show="headings", height=4,
            selectmode="browse", style="Meeting.Treeview")
        for column, label, width in (("time", "Início", 75), ("track", "Fonte", 80),
                                     ("speaker", "Rótulo manual", 120), ("text", "Texto", 360)):
            self.transcript.heading(column, text=label)
            self.transcript.column(column, width=width, stretch=column == "text")
        self.transcript.pack(fill="both", expand=True, padx=(12, 0))
        self.transcript.bind("<<TreeviewSelect>>", self._transcript_selected)
        transcript_pages = ttk.Frame(right, style="Meeting.TFrame")
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
        self.segment_text = tk.Text(right, height=3, wrap="word", font=self.ui.font(), **self.ui.text_colors())
        self.segment_text.pack(fill="x", padx=(12, 0), pady=4)
        self.segment_text.configure(state="disabled")
        self.segments = {}
        self._label(
            right, "Rótulo manual do locutor (não é identificação biométrica)", anchor="w",
            fg=self.ui.text_muted,
        ).pack(fill="x", padx=12, pady=(4, 2))
        speaker_row = ttk.Frame(right, style="Meeting.TFrame")
        speaker_row.pack(fill="x", padx=(12, 0), pady=(0, 4))
        self.speaker_name = tk.StringVar(self.window)
        self._entry(speaker_row, self.speaker_name, 24).pack(side="left", fill="x", expand=True)
        self.speaker_save_button = self._button(speaker_row, "Salvar rótulo", self.save_speaker_label)
        self.speaker_save_button.pack(side="left", padx=(6, 0))
        self.speaker_delete_button = self._button(speaker_row, "Excluir rótulo", self.delete_speaker_label, danger=True)
        self.speaker_delete_button.configure(state="disabled")
        self.speaker_delete_button.pack(side="left", padx=(6, 0))
        highlight_row = ttk.Frame(right, style="Meeting.TFrame")
        highlight_row.pack(fill="x", padx=(12, 0), pady=(2, 4))
        self._label(highlight_row, "Destaque", fg=self.ui.text_muted).pack(side="left", padx=(0, 4))
        self.highlight_start = tk.StringVar(self.window)
        self.highlight_end = tk.StringVar(self.window)
        self.highlight_label = tk.StringVar(self.window)
        self.highlight_note = tk.StringVar(self.window)
        self._entry(highlight_row, self.highlight_start, 8).pack(side="left")
        self._label(highlight_row, "até", fg=self.ui.text_muted).pack(side="left", padx=4)
        self._entry(highlight_row, self.highlight_end, 8).pack(side="left")
        self._entry(highlight_row, self.highlight_label, 20).pack(side="left", fill="x", expand=True, padx=6)
        self._entry(highlight_row, self.highlight_note, 24).pack(side="left", fill="x", expand=True)
        self.highlight_choice = tk.StringVar(self.window)
        self.highlight_picker = ttk.Combobox(
            right, textvariable=self.highlight_choice, state="readonly", width=42,
        )
        self.highlight_picker.pack(fill="x", padx=(12, 0), pady=(0, 4))
        self.highlight_picker.bind("<<ComboboxSelected>>", self._highlight_selected)
        highlight_actions = ttk.Frame(right, style="Meeting.TFrame")
        highlight_actions.pack(fill="x", padx=(12, 0), pady=(0, 4))
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
        self.track = tk.StringVar(self.window, "Microfone")
        playback = ttk.Frame(right, style="Meeting.TFrame")
        playback.pack(fill="x", padx=(12, 0), pady=8)
        ttk.Combobox(playback, textvariable=self.track, values=list(TRACK_LABELS),
                     width=12, state="readonly").pack(side="left")
        self.play_button = self._button(playback, "Ouvir neste horário", self.play)
        self.play_button.pack(side="left", padx=8)
        self._button(playback, "Parar reprodução", lambda: self._action("stop_playback", urgent=True)).pack(side="left")
        self.playback_status = tk.StringVar(self.window, "Reprodução parada.")
        self._label(playback, "", textvariable=self.playback_status, anchor="w",
                    fg=self.ui.text_muted).pack(side="left", padx=(8, 0))
        actions = ttk.Frame(right, style="Meeting.TFrame")
        actions.pack(fill="x", padx=(12, 0), pady=4)
        self._button(actions, "Transcrever novamente", self.transcribe).pack(side="left", padx=(0, 6))
        self._button(actions, "Cancelar processamento", lambda: self._action("cancel_processing", urgent=True)).pack(side="left")
        exports = ttk.Frame(right, style="Meeting.TFrame")
        exports.pack(fill="x", padx=(12, 0), pady=4)
        self._button(exports, "Exportar Markdown…", lambda: self.export("markdown")).pack(side="left", padx=(0, 6))
        self._button(exports, "Exportar texto…", lambda: self.export("text")).pack(side="left", padx=(0, 6))
        self._button(exports, "Exportar áudio final…", self.export_audio).pack(side="left", padx=(0, 6))
        self._button(exports, "Gerar relatório local", self.generate_report).pack(side="left")
        self._label(right, "Resumo editável · copie a revisão para notas antes de salvar", anchor="w").pack(
            fill="x", padx=12, pady=(8, 0))
        self.summary = tk.Text(right, height=3, wrap="word", font=self.ui.font(), **self.ui.text_colors())
        self.summary.pack(fill="x", padx=(12, 0), pady=(8, 0))
        self.summary.insert("1.0", "Resumo local opcional. Revise os resultados e copie o texto para suas notas.")
        self.summary.configure(state="disabled")
        self._button(right, "Copiar resumo revisado para notas", self.summary_to_notes).pack(anchor="w", padx=12, pady=4)
        self._button(right, "Salvar resumo revisado", self.save_summary).pack(anchor="w", padx=12, pady=4)

        # Structured local reports.  The generated envelope remains immutable;
        # the editor below writes only a reviewed artifact through the library.
        report_frame = self._card(right, padx=12, pady=8)
        report_frame.pack(fill="x", padx=(12, 0), pady=(8, 0))
        self._label(report_frame, "Relatórios locais", bg=self.ui.card,
                    fg=self.ui.text_strong, font=self.ui.font(11, "bold")).pack(anchor="w")
        self._label(
            report_frame,
            "Perfis são receitas locais versionadas; o modelo nunca recebe dados fora do computador.",
            bg=self.ui.card, fg=self.ui.text_muted, anchor="w", justify="left", wraplength=720,
        ).pack(fill="x", pady=(2, 6))
        profile_row = tk.Frame(report_frame, bg=self.ui.card)
        profile_row.pack(fill="x", pady=2)
        self.report_profile_choice = tk.StringVar(self.window, "Geral")
        self.report_profile_box = ttk.Combobox(
            profile_row, textvariable=self.report_profile_choice, state="readonly", width=28,
        )
        self.report_profile_box.pack(side="left", fill="x", expand=True)
        self.report_profile_box.bind("<<ComboboxSelected>>", self._report_profile_changed)
        self._button(profile_row, "Criar", self.create_report_profile).pack(side="left", padx=(6, 0))
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
            history_row, textvariable=self.report_history_choice, state="readonly", width=48,
        )
        self.report_history_box.pack(side="left", fill="x", expand=True)
        self.report_history_box.bind("<<ComboboxSelected>>", self._report_history_changed)
        self._button(history_row, "Atualizar", self.refresh_reports).pack(side="left", padx=(6, 0))
        section_row = tk.Frame(report_frame, bg=self.ui.card)
        section_row.pack(fill="x", pady=2)
        self._label(section_row, "Seção", bg=self.ui.card).pack(side="left", padx=(0, 6))
        self.report_section_choice = tk.StringVar(self.window)
        self.report_section_box = ttk.Combobox(
            section_row, textvariable=self.report_section_choice, state="readonly", width=24,
        )
        self.report_section_box.pack(side="left", fill="x", expand=True)
        self.report_section_box.bind("<<ComboboxSelected>>", self._report_section_changed)
        self._button(section_row, "Copiar seção", self.copy_report_section).pack(side="left", padx=(6, 0))
        self._button(section_row, "Exportar…", self.export_report).pack(side="left", padx=(6, 0))
        self.report_provenance = tk.StringVar(self.window, "Nenhum relatório selecionado.")
        self._label(report_frame, "", textvariable=self.report_provenance, bg=self.ui.card,
                    fg=self.ui.text_muted, anchor="w", justify="left", wraplength=720).pack(fill="x", pady=2)
        self.report_editor = tk.Text(report_frame, height=5, wrap="word", font=self.ui.font(),
                                    **self.ui.text_colors())
        self.report_editor.pack(fill="x", pady=(4, 2))
        report_actions = tk.Frame(report_frame, bg=self.ui.card)
        report_actions.pack(fill="x", pady=2)
        self._button(report_actions, "Salvar revisão", self.save_report_review, accent=True).pack(side="left")
        self._label(report_actions, "Citações", bg=self.ui.card, fg=self.ui.text_muted).pack(side="left", padx=(12, 4))
        self.report_citations = tk.Listbox(report_actions, height=2, width=42,
                                           font=self.ui.font(), **self.ui.listbox_colors())
        self.report_citations.pack(side="left", fill="x", expand=True)
        self.report_citations.bind("<Double-Button-1>", lambda _event: self.jump_to_report_citation())
        self._button(report_actions, "Ir à fonte", self.jump_to_report_citation).pack(side="left", padx=(6, 0))

        ask_frame = self._card(right, padx=12, pady=8)
        ask_frame.pack(fill="x", padx=(12, 0), pady=(8, 0))
        self._label(ask_frame, "Perguntar sobre esta reunião", bg=self.ui.card,
                    fg=self.ui.text_strong, font=self.ui.font(11, "bold")).pack(anchor="w")
        self.ask_question = tk.StringVar(self.window)
        ask_row = tk.Frame(ask_frame, bg=self.ui.card)
        ask_row.pack(fill="x", pady=3)
        self._entry(ask_row, self.ask_question, 60).pack(side="left", fill="x", expand=True)
        self._button(ask_row, "Perguntar", self.ask_this_meeting, accent=True).pack(side="left", padx=(6, 0))
        self.ask_answer = tk.Text(ask_frame, height=3, wrap="word", font=self.ui.font(),
                                  **self.ui.text_colors())
        self.ask_answer.pack(fill="x", pady=(2, 2))
        ask_citation_row = tk.Frame(ask_frame, bg=self.ui.card)
        ask_citation_row.pack(fill="x", pady=(0, 2))
        self._label(ask_citation_row, "Citações", bg=self.ui.card,
                    fg=self.ui.text_muted).pack(side="left", padx=(0, 4))
        self.ask_citations = tk.Listbox(ask_citation_row, height=2, width=42,
                                        font=self.ui.font(), **self.ui.listbox_colors())
        self.ask_citations.pack(side="left", fill="x", expand=True)
        self.ask_citations.bind("<Double-Button-1>", lambda _event: self.jump_to_ask_citation())
        self._button(ask_citation_row, "Ir à fonte", self.jump_to_ask_citation).pack(side="left", padx=(6, 0))
        ask_actions = tk.Frame(ask_frame, bg=self.ui.card)
        ask_actions.pack(fill="x")
        self._button(ask_actions, "Salvar resposta", self.save_answer).pack(side="left")
        self.ask_status = tk.StringVar(self.window, "Respostas ficam somente na memória até você salvar.")
        self._label(ask_actions, "", textvariable=self.ask_status, bg=self.ui.card,
                    fg=self.ui.text_muted, anchor="w", wraplength=580).pack(side="left", padx=(8, 0))

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
            f'{len(value.get("citations", []))} citações. Salve explicitamente se quiser persistir.'
        )

    def save_answer(self):
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
        self.status.set("Gravações ficam neste computador. Inicie somente quando quiser gravar.")

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
        self.operation_details = "Detalhes técnicos: " + str(error)[:4096]
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
        self._submit("devices", self.controller.devices, self._devices_loaded)

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

    def start(self):
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
            return
        self._submit("control", lambda: self.controller.start(settings, title=title), self._started, urgent=True)

    def _started(self, accepted, error):
        if error:
            self._remember_operation_error(error)
            self.status.set("Não foi possível iniciar a gravação. Veja os detalhes na aba Gravação.")
        else:
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
        self.refresh_library()

    def change_page(self, delta):
        self.offset = max(0, self.offset + delta * PAGE_SIZE)
        self.refresh_library()

    def refresh_library(self):
        offset, query = self.offset, self.query.get()
        status = STATUS_FILTERS[self.status_filter.get()]
        def read():
            return [{key: item.get(key) for key in ("id", "title", "status")}
                    for item in islice(self.controller.list_sessions(offset=offset, limit=PAGE_SIZE, query=query, status=status), PAGE_SIZE)]
        self._submit("library", read, self._library_loaded)

    def _library_loaded(self, sessions, error):
        if error:
            self._remember_operation_error(error)
            self.status.set("Não foi possível carregar a biblioteca. Veja os detalhes na aba Gravação.")
            return
        self.sessions.delete(*self.sessions.get_children())
        items = list(islice(sessions or [], PAGE_SIZE))
        for item in items:
            self.sessions.insert("", "end", iid=item["id"], values=(item.get("title") or item["id"],
                STATE_LABELS.get(item.get("status"), item.get("status", ""))))
        self.previous_button.configure(state="normal" if self.offset else "disabled")
        self.next_button.configure(state="normal" if len(items) == PAGE_SIZE else "disabled")
        self.page_label.set(f"Página {self.offset // PAGE_SIZE + 1} · {len(items)} gravações")
        if self.selected in self.sessions.get_children():
            self.sessions.selection_set(self.selected)

    def _selection_changed(self, _event=None):
        selected = self.sessions.selection()
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
        self.load_session(session_id)

    def load_session(self, session_id):
        self.selected = session_id
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
        self.speaker_labels = metadata.get("speaker_labels", {})
        self.highlights = metadata.get("highlights", [])
        self.detail_ready = True
        self.delete_button.configure(state="disabled" if metadata.get("status") == "recording" else "normal")
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
        self.status.set("Notas extensas: visualização parcial, somente leitura." if self.truncated else
                        f"Gravação carregada · página de transcrição com {len(segments)} trechos."
                        + (" · Uma etapa anterior não foi concluída." if metadata.get("error") else ""))

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

    def delete_selected(self):
        if not self.selected or not self.detail_ready:
            self.status.set("Selecione uma gravação na biblioteca.")
            return
        session_id = self.selected
        title = self.title.get().strip() or session_id
        message = (
            f'Excluir “{title}” da biblioteca?\n\n'
            "Os arquivos capturados, a transcrição, as notas e o resumo serão removidos "
            "permanentemente. Um arquivo final exportado para outra pasta não será apagado."
        )
        if not messagebox.askyesno("Excluir gravação", message, parent=self.window):
            return
        self.delete_button.configure(state="disabled")
        self.status.set("Excluindo gravação…")

        def deleted(value, error):
            if error or value is False:
                if self.selected == session_id:
                    self.delete_button.configure(state="normal")
                if error:
                    self._remember_operation_error(error)
                self.status.set("Não foi possível excluir a gravação. Veja os detalhes na aba Gravação."
                                if error else "Não foi possível excluir a gravação. Tente novamente.")
                return
            if self.selected == session_id:
                self._clear_library_detail()
            self.refresh_library()
            self.status.set("Gravação excluída da biblioteca.")

        if not self._submit("delete_session", lambda: self.controller.delete_session(session_id), deleted):
            self.delete_button.configure(state="normal")

    def _clear_library_detail(self):
        self.bridge.invalidate("detail")
        self.bridge.invalidate("outputs")
        self.bridge.invalidate("transcript_page")
        self.transcript_request += 1
        self.selected = None
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
                "Trecho selecionado · horários representam blocos de áudio, não palavras."
            )

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
        self.highlight_export_button.configure(state="normal")

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

    def play(self):
        if not self.selected:
            self.status.set("Selecione uma gravação na biblioteca.")
            return
        if self.snapshot.get("state") in ("starting", "recording", "paused", "stopping"):
            self.status.set("Finalize a captura antes de ouvir uma gravação.")
            return
        try:
            position = self._position()
        except ValueError as exc:
            self.status.set(str(exc))
            return
        self._action("play", self.selected, TRACK_LABELS[self.track.get()], start=position)

    def transcribe(self):
        if not self.selected:
            self.status.set("Selecione uma gravação na biblioteca.")
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
        position = playback.get("position", playback.get("start", 0))
        try:
            position = max(0.0, float(position))
        except (TypeError, ValueError):
            position = 0.0
        if active:
            track = playback.get("track")
            track_label = "Microfone" if track == "microphone" else "Sistema"
            self.playback_status.set(
                f"Reproduzindo {track_label} · {format_time(position)} · horários por trecho de áudio"
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
            self.snapshot = self.controller.snapshot()
            self._apply_playback_snapshot(self.snapshot)
            state = self.snapshot.get("state", "idle")
            active = state in ("starting", "recording", "paused", "stopping")
            processing = self.snapshot.get("processing")
            self.start_button.configure(state="normal" if state == "idle" and not processing and self.settings_loaded else "disabled")
            self.stop_button.configure(state="normal" if active else "disabled")
            self.pause_button.configure(text="Retomar" if state == "paused" else "Pausar",
                state="normal" if state in ("recording", "paused") else "disabled")
            self.play_button.configure(state="normal" if state == "idle" and not processing else "disabled")
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
            waveform_state = ("paused" if state == "paused" else "recording"
                              if state in ("starting", "recording", "stopping") else "idle")
            self.waveform.set_state(waveform_state)
            for track, meter in self.meters.items():
                value = float(self.snapshot.get("levels", {}).get(track, 0) or 0)
                meter.configure(value=max(0, min(1, value)) if math.isfinite(value) else 0)
            if self.previous_state in ("recording", "paused", "stopping") and not active:
                self.refresh_library()
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
        self.unsaved_answer = None
        self.playback_generation = getattr(self, "playback_generation", -1) + 1
        self.transcript_request = getattr(self, "transcript_request", 0) + 1
        if getattr(self, "summary_download_cancel", None) is not None:
            self.summary_download_cancel.set()
        try:
            self.controller.stop_playback()
        except (AttributeError, RuntimeError):
            # Older controller doubles and an already-torn-down controller are safe.
            pass
        self._unbind_mousewheel_regions()
        self.bridge.close()
        if self.after_id is not None:
            self.root.after_cancel(self.after_id)
            self.after_id = None
        if destroy:
            self.window.destroy()
        if after_close is not None:
            after_close()


def open_meeting_window(root, controller, settings_getter, persist_settings, on_settings_changed=None):
    view = MeetingWindow(root, controller, settings_getter, persist_settings, on_settings_changed)
    view.window._meeting_view = view
    return view.window


def add_meeting_tabs(root, window, notebook, controller, settings_getter,
                     persist_settings, on_settings_changed=None):
    """Attach recording and library tabs to the shared application window."""
    view = MeetingWindow(
        root, controller, settings_getter, persist_settings, on_settings_changed,
        window=window, notebook=notebook,
    )
    window._meeting_view = view
    return view
