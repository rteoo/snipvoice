"""Shared-root meeting workspace with bounded workers and Tk-only updates."""

from itertools import islice
import json
import math
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from meeting_settings import EndpointSelection, resolve_meeting_settings, validate_hotkey_conflicts
import ui_theme
from voice_catalog import available_languages, selectable_catalog
from voice_hotkey import DEFAULT_COMMAND_HOTKEY, DEFAULT_DICTATION_HOTKEY


PAGE_SIZE = 50
TRANSCRIPT_LIMIT = 500
NOTES_LIMIT = 1024 * 1024
BOOKMARK_LIMIT = 1000
SOURCE_LABELS = {"both": "Microfone e sistema", "microphone": "Somente microfone",
                 "system": "Somente áudio do sistema"}
STATE_LABELS = {"idle": "Pronto", "starting": "Iniciando", "recording": "Gravando",
                "paused": "Pausado", "stopping": "Finalizando", "completed": "Concluído",
                "stopped": "Parado", "interrupted": "Interrompido", "failed": "Falha",
                "partial": "Parcial", "teardown_blocked": "Recursos ainda em uso",
                "unavailable": "Recursos ainda em uso"}
STATUS_FILTERS = {"Todos": "", "Concluídos": "completed", "Parciais": "partial",
                  "Falhas": "failed", "Interrompidos": "interrupted", "Gravando": "recording"}
PROFILE_LABELS = {"balanced": "Equilibrado · Parakeet TDT", "compact": "Compacto · Qwen 0.6B",
                  "accuracy": "Precisão · Qwen 1.7B", "streaming": "Transcrição contínua"}
LANGUAGE_LABELS = {"auto": "Automático", "pt-BR": "Português (Brasil)", "en-US": "Inglês (Estados Unidos)"}
TRACK_LABELS = {"Microfone": "microphone", "Sistema": "system"}


def format_time(seconds):
    seconds = float(seconds or 0)
    if not math.isfinite(seconds) or seconds < 0:
        seconds = 0
    whole = int(seconds)
    return f"{whole // 3600:02d}:{whole // 60 % 60:02d}:{whole % 60:02d}"


def endpoint_options(devices, track, selection):
    """Keep a missing manually selected endpoint explicit and pinned."""
    options = [("Padrão do sistema · multimídia", EndpointSelection()),
               ("Padrão do sistema · comunicações", EndpointSelection(default_role="communications"))]
    for device in devices:
        if (isinstance(device, dict) and device.get("kind") == track
                and isinstance(device.get("id"), str) and device["id"]):
            identifier = device["id"]
            name = str(device.get("name") or identifier)
            options.append((f"{name} · {identifier}", EndpointSelection("manual", identifier)))
    if (selection.mode == "manual"
            and not any(item.endpoint_id == selection.endpoint_id for _, item in options)):
        options.append((f"Indisponível · {selection.endpoint_id}", selection))
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
        self.dirty = False
        self.loading = False
        self.truncated = False
        self.settings_loaded = False
        self.detail_ready = False
        self.processing_target = None
        self.previous_state = None
        self.snapshot = {}
        self._build()
        if not self.embedded:
            self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.window.bind("<Destroy>", self._destroyed, add="+")
        self._submit("settings", self.settings_getter, self._settings_loaded)
        self.refresh_devices()
        self.refresh_library()
        self._poll()

    def _label(self, parent, text, **kwargs):
        return tk.Label(parent, text=text, bg=self.ui.surface, fg=self.ui.text,
                        font=self.ui.font(), **kwargs)

    def _button(self, parent, text, command, accent=False):
        return tk.Button(parent, text=text, command=command, font=self.ui.font(),
                         **self.ui.button_colors(accent=accent), **self.ui.button_chrome(compact=True))

    def _entry(self, parent, variable, width=30):
        return tk.Entry(parent, textvariable=variable, width=width, font=self.ui.font(),
                        **self.ui.entry_colors())

    def _build(self):
        style = ttk.Style(self.window)
        style.configure("Meeting.TFrame", background=self.ui.surface)
        style.configure("Meeting.Treeview", rowheight=self.ui.tree_row_height, font=self.ui.font())
        notebook = self.notebook
        if notebook is None:
            notebook = ttk.Notebook(self.window)
            notebook.pack(fill="both", expand=True, padx=12, pady=12)
            self.notebook = notebook
        self.recording_tab = ttk.Frame(notebook, style="Meeting.TFrame")
        self.library_tab = ttk.Frame(notebook, style="Meeting.TFrame")
        notebook.add(self.recording_tab, text="Gravar e configurar")
        notebook.add(self.library_tab, text="Biblioteca e transcrição")
        self.status = tk.StringVar(self.window, "Carregando configurações…")
        for tab in (self.recording_tab, self.library_tab):
            self._label(tab, "", textvariable=self.status, anchor="w", wraplength=1050).pack(
                fill="x", padx=18, pady=(12, 0))
        recording = ttk.Frame(self.recording_tab, padding=14, style="Meeting.TFrame")
        recording.pack(fill="both", expand=True)
        library = ttk.Frame(self.library_tab, padding=14, style="Meeting.TFrame")
        library.pack(fill="both", expand=True)
        self.record_title = tk.StringVar(self.window)
        self.sources = tk.StringVar(self.window, SOURCE_LABELS[self.settings.sources])
        self.profile = tk.StringVar(self.window, self.settings.profile)
        self.language = tk.StringVar(self.window, self.settings.language)
        self.profile_display = tk.StringVar(self.window, PROFILE_LABELS[self.settings.profile])
        self.language_display = tk.StringVar(self.window, LANGUAGE_LABELS[self.settings.language])
        self.hotkey = tk.StringVar(self.window)
        self.summary_model = tk.StringVar(self.window)
        self.endpoint_vars = {track: tk.StringVar(self.window) for track in ("microphone", "system")}
        self.endpoint_boxes = {}
        rows = [("Título da próxima gravação", self._entry(recording, self.record_title)),
                ("Fontes de áudio", ttk.Combobox(recording, textvariable=self.sources,
                                               values=list(SOURCE_LABELS.values()), state="readonly"))]
        for track, label in (("microphone", "Microfone"), ("system", "Áudio do sistema")):
            combo = ttk.Combobox(recording, textvariable=self.endpoint_vars[track], state="readonly", width=65)
            self.endpoint_boxes[track] = combo
            rows.append((label, combo))
        self.profile_box = ttk.Combobox(recording, textvariable=self.profile_display,
            values=[PROFILE_LABELS[entry["profile"]] for entry in selectable_catalog()], state="readonly")
        self.profile_box.bind("<<ComboboxSelected>>", self._profile_changed)
        self.language_box = ttk.Combobox(recording, textvariable=self.language_display, state="readonly")
        self.language_box.bind("<<ComboboxSelected>>", self._language_changed)
        rows.extend([("Modelo de transcrição local", self.profile_box), ("Idioma", self.language_box),
                     ("Atalho de gravação (opcional)", self._entry(recording, self.hotkey)),
                     ("Modelo local de resumo (Ollama)", self._entry(recording, self.summary_model))])
        for row, (label, widget) in enumerate(rows):
            self._label(recording, label, anchor="w").grid(row=row, column=0, sticky="w", padx=(0, 18), pady=6)
            widget.grid(row=row, column=1, sticky="ew", pady=6)
        recording.columnconfigure(1, weight=1)
        self._label(recording, "Configurações valem para a próxima gravação. O atalho começa sem atribuição.\n"
                    "O áudio do sistema inclui os sons do dispositivo escolhido. Use fones para reduzir duplicação.",
                    justify="left", anchor="w", wraplength=850).grid(row=8, column=0, columnspan=2, sticky="ew", pady=12)
        commands = ttk.Frame(recording, style="Meeting.TFrame")
        commands.grid(row=9, column=0, columnspan=2, sticky="w")
        self._button(commands, "Atualizar dispositivos", self.refresh_devices).pack(side="left", padx=(0, 8))
        self._button(commands, "Salvar configurações", self.save_settings).pack(side="left", padx=(0, 8))
        self._button(commands, "Importar modelo local…", self.import_model).pack(side="left")
        transport = ttk.Frame(recording, style="Meeting.TFrame")
        transport.grid(row=10, column=0, columnspan=2, sticky="w", pady=18)
        self.start_button = self._button(transport, "Iniciar gravação", self.start, accent=True)
        self.start_button.pack(side="left", padx=(0, 8))
        self.pause_button = self._button(transport, "Pausar", self.pause_resume)
        self.pause_button.pack(side="left", padx=(0, 8))
        self.stop_button = self._button(transport, "Parar e preservar", self.stop)
        self.stop_button.pack(side="left")
        self.record_status = tk.StringVar(self.window, "Pronto · 00:00:00")
        self._label(recording, "", textvariable=self.record_status, anchor="w",
                    wraplength=850).grid(row=11, column=0, columnspan=2, sticky="ew")
        self.meters = {}
        for row, (track, label) in enumerate((("microphone", "Nível do microfone"), ("system", "Nível do sistema")), 12):
            self._label(recording, label, anchor="w").grid(row=row, column=0, sticky="w", pady=8)
            meter = ttk.Progressbar(recording, maximum=1.0)
            meter.grid(row=row, column=1, sticky="ew")
            self.meters[track] = meter
        self._profile_changed()
        self.options.clear()
        self._render_devices()
        self._build_library(library)

    def _build_library(self, parent):
        search_row = ttk.Frame(parent, style="Meeting.TFrame")
        search_row.pack(fill="x", pady=(0, 12))
        self.query = tk.StringVar(self.window)
        self._label(search_row, "Buscar título/notas").pack(side="left", padx=(0, 8))
        search = self._entry(search_row, self.query)
        search.pack(side="left", fill="x", expand=True)
        search.bind("<Return>", lambda _event: self.search())
        self._button(search_row, "Buscar", self.search).pack(side="left", padx=8)
        self.status_filter = tk.StringVar(self.window, "Todos")
        filters = ttk.Combobox(search_row, textvariable=self.status_filter, state="readonly",
                               values=list(STATUS_FILTERS), width=14)
        filters.pack(side="left", padx=(0, 8))
        filters.bind("<<ComboboxSelected>>", lambda _event: self.search())
        self._button(search_row, "Importar WAV…", self.import_wav).pack(side="left")
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
        self._label(right, "Transcrição · horários por trecho de áudio", anchor="w").pack(fill="x", padx=12, pady=(10, 4))
        self.transcript = ttk.Treeview(right, columns=("time", "track", "text"), show="headings", height=4,
                                      selectmode="browse", style="Meeting.Treeview")
        for column, label, width in (("time", "Início", 75), ("track", "Fonte", 80), ("text", "Texto", 360)):
            self.transcript.heading(column, text=label)
            self.transcript.column(column, width=width, stretch=column == "text")
        self.transcript.pack(fill="both", expand=True, padx=(12, 0))
        self.transcript.bind("<<TreeviewSelect>>", self._transcript_selected)
        self.segment_text = tk.Text(right, height=3, wrap="word", font=self.ui.font(), **self.ui.text_colors())
        self.segment_text.pack(fill="x", padx=(12, 0), pady=4)
        self.segment_text.configure(state="disabled")
        self.segments = {}
        self.track = tk.StringVar(self.window, "Microfone")
        playback = ttk.Frame(right, style="Meeting.TFrame")
        playback.pack(fill="x", padx=(12, 0), pady=8)
        ttk.Combobox(playback, textvariable=self.track, values=list(TRACK_LABELS),
                     width=12, state="readonly").pack(side="left")
        self.play_button = self._button(playback, "Ouvir neste horário", self.play)
        self.play_button.pack(side="left", padx=8)
        self._button(playback, "Parar reprodução", lambda: self._action("stop_playback", urgent=True)).pack(side="left")
        actions = ttk.Frame(right, style="Meeting.TFrame")
        actions.pack(fill="x", padx=(12, 0), pady=4)
        self._button(actions, "Transcrever novamente", self.transcribe).pack(side="left", padx=(0, 6))
        self._button(actions, "Cancelar processamento", lambda: self._action("cancel_processing", urgent=True)).pack(side="left")
        exports = ttk.Frame(right, style="Meeting.TFrame")
        exports.pack(fill="x", padx=(12, 0), pady=4)
        self._button(exports, "Exportar Markdown…", lambda: self.export("markdown")).pack(side="left", padx=(0, 6))
        self._button(exports, "Exportar texto…", lambda: self.export("text")).pack(side="left", padx=(0, 6))
        self._button(exports, "Gerar resumo local", self.summarize).pack(side="left")
        self._label(right, "Resumo editável · copie a revisão para notas antes de salvar", anchor="w").pack(
            fill="x", padx=12, pady=(8, 0))
        self.summary = tk.Text(right, height=3, wrap="word", font=self.ui.font(), **self.ui.text_colors())
        self.summary.pack(fill="x", padx=(12, 0), pady=(8, 0))
        self.summary.insert("1.0", "Resumo local opcional. Revise os resultados e copie o texto para suas notas.")
        self.summary.configure(state="disabled")
        self._button(right, "Copiar resumo revisado para notas", self.summary_to_notes).pack(anchor="w", padx=12, pady=4)
        self._button(right, "Salvar resumo revisado", self.save_summary).pack(anchor="w", padx=12, pady=4)

    def _submit(self, key, operation, callback=None, urgent=False):
        if not self.bridge.submit(key, operation, callback or self._done, urgent):
            if not self.closed:
                self.status.set("Há operações pendentes. Aguarde e tente novamente.")
            return False
        return True

    def _done(self, _value, error):
        self.status.set(error or ("A operação não foi aceita. Verifique o estado atual." if _value is False else "Operação concluída."))

    def _settings_loaded(self, raw, error):
        if error:
            self.status.set(error)
            return
        self.raw_settings = dict(raw or {})
        try:
            settings = resolve_meeting_settings(self.raw_settings)
        except ValueError as exc:
            self.status.set(str(exc))
            return
        self.settings = settings
        self.sources.set(SOURCE_LABELS[settings.sources])
        self.profile.set(settings.profile)
        self.language.set(settings.language)
        self.profile_display.set(PROFILE_LABELS[settings.profile])
        self.language_display.set(LANGUAGE_LABELS[settings.language])
        self.hotkey.set(settings.hotkey)
        self.summary_model.set(settings.summary_model)
        self._profile_changed()
        self.options.clear()
        self._render_devices()
        self.settings_loaded = True
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

    def _current_settings(self):
        if not self.settings_loaded:
            raise ValueError("Aguarde o carregamento das configurações antes de gravar ou salvar.")
        data = dict(self.raw_settings)
        data.update(meeting_sources=next(key for key, label in SOURCE_LABELS.items() if label == self.sources.get()),
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
            self.status.set(error)
            return
        self.settings = settings
        self.raw_settings.update(settings.payload())
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
            self.status.set(error)
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

    def start(self):
        try:
            settings = self._current_settings()
            title = self.record_title.get().strip()
            if len(title) > 400:
                raise ValueError("O título deve ter até 400 caracteres.")
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
        self.status.set(error or ("Gravação solicitada." if accepted else "Não foi possível iniciar. Verifique o estado da gravação."))

    def stop(self):
        self._action("stop", urgent=True)

    def pause_resume(self):
        self._action("resume" if self.snapshot.get("state") == "paused" else "pause", urgent=True)

    def _action(self, method, *args, urgent=False, callback=None, **kwargs):
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
            self.status.set(error)
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
        self.detail_ready = False
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
        self.segment_text.configure(state="normal")
        self.segment_text.delete("1.0", "end")
        self.segment_text.configure(state="disabled")
        self.bridge.invalidate("detail")
        self.bridge.invalidate("outputs")
        def read():
            raw = self.controller.get_session(session_id)
            notes = str(raw.get("notes", ""))
            metadata = {key: raw.get(key) for key in ("id", "title", "status", "duration", "error")}
            bookmarks = list(islice(raw.get("bookmarks", []), BOOKMARK_LIMIT + 1))
            invalid_bookmarks = any(not isinstance(item, dict) for item in bookmarks)
            summary = raw.get("reviewed_summary") or raw.get("summary")
            if isinstance(summary, dict):
                summary = json.dumps(summary, ensure_ascii=False, indent=2)
            metadata.update(notes=notes[:NOTES_LIMIT], truncated=len(notes) > NOTES_LIMIT
                            or len(bookmarks) > BOOKMARK_LIMIT or invalid_bookmarks,
                            bookmarks=bookmarks[:BOOKMARK_LIMIT], summary=str(summary or "")[:65536])
            segments = [{key: segment.get(key) for key in ("id", "start", "end", "track")}
                        | {"text": str(segment.get("text", ""))[:8000]}
                        for segment in islice(self.controller.get_transcript(session_id), TRANSCRIPT_LIMIT)]
            return metadata, segments
        self.status.set("Carregando gravação…")
        self._submit("detail", read, lambda data, error: self._detail_loaded(session_id, data, error))

    def _detail_loaded(self, session_id, data, error):
        if session_id != self.selected:
            return
        if error:
            self.status.set(error)
            return
        metadata, segments = data
        self.loading = True
        self.title.set(metadata.get("title") or "")
        self.notes.configure(state="normal")
        self.notes.delete("1.0", "end")
        self.notes.insert("1.0", metadata["notes"])
        self.truncated = metadata["truncated"]
        self.detail_ready = True
        if self.truncated:
            self.notes.configure(state="disabled")
        self.notes.edit_modified(False)
        self.bookmarks = metadata["bookmarks"]
        self.dirty = False
        self.loading = False
        self._render_bookmarks()
        self._render_transcript(segments)
        self._show_summary(metadata.get("summary"))
        self.status.set("Notas extensas: visualização parcial, somente leitura." if self.truncated else
                        f"Gravação carregada · {len(segments)} trechos exibidos (limite {TRANSCRIPT_LIMIT})."
                        + (" · " + str(metadata["error"])[:1024] if metadata.get("error") else ""))

    def _render_transcript(self, segments):
        self.transcript.delete(*self.transcript.get_children())
        self.segments.clear()
        for index, segment in enumerate(segments):
            key = str(index)
            self.segments[key] = segment
            self.transcript.insert("", "end", iid=key, values=(format_time(segment.get("start", 0)),
                "Microfone" if segment.get("track") == "microphone" else "Sistema", str(segment.get("text", ""))[:8000]))

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
                self.status.set(error or "Não foi possível salvar as notas. Tente novamente.")
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
            segment = self.segments[selected[0]]
            self.position.set(str(segment.get("start", 0)))
            self.track.set("Microfone" if segment.get("track") == "microphone" else "Sistema")
            self.segment_text.configure(state="normal")
            self.segment_text.delete("1.0", "end")
            self.segment_text.insert("1.0", segment.get("text", ""))
            self.segment_text.configure(state="disabled")

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

    def import_wav(self):
        try:
            settings = self._current_settings()
        except ValueError as exc:
            self.status.set(str(exc))
            return
        path = filedialog.askopenfilename(parent=self.window, title="Importar áudio WAV",
                                           filetypes=(("Áudio WAV", "*.wav"),))
        if path:
            def imported(session_id, error):
                if error:
                    self.status.set(error)
                    return
                self.refresh_library()
                self.load_session(session_id)
            self._action("import_wav", path, settings, callback=imported)

    def export(self, format):
        if not self.selected:
            self.status.set("Selecione uma gravação na biblioteca.")
            return
        suffix = ".md" if format == "markdown" else ".txt"
        path = filedialog.asksaveasfilename(parent=self.window, title="Exportar transcrição",
            defaultextension=suffix, filetypes=(("Markdown" if suffix == ".md" else "Texto", "*" + suffix),))
        if path:
            self._action("export", self.selected, path, format=format)

    def summarize(self):
        if not self.selected:
            self.status.set("Selecione uma gravação na biblioteca.")
            return
        model = self.summary_model.get().strip()
        if not model:
            self.status.set("Informe o nome de um modelo Ollama local já instalado na aba de configurações.")
            return
        session_id = self.selected
        self._action("summarize", session_id, model,
                     callback=lambda value, error: self._processing_launched(session_id, value, error))

    def _processing_launched(self, session_id, accepted, error):
        if error or not accepted:
            self.status.set(error or "Há uma captura ou outro processamento em andamento. Tente novamente depois.")
            return
        self.processing_target = session_id
        self.status.set("Processamento local iniciado. O resultado aparecerá quando terminar.")

    def refresh_outputs(self, session_id):
        def read():
            raw = self.controller.get_session(session_id)
            summary = raw.get("summary")
            summary = json.dumps(summary, ensure_ascii=False, indent=2) if isinstance(summary, dict) else str(summary or "")
            segments = [{key: segment.get(key) for key in ("id", "start", "end", "track")}
                        | {"text": str(segment.get("text", ""))[:8000]}
                        for segment in islice(self.controller.get_transcript(session_id), TRANSCRIPT_LIMIT)]
            return summary[:65536], segments
        def loaded(data, error):
            if self.selected != session_id or not self.detail_ready:
                return
            if error:
                self.status.set(error)
                return
            summary, segments = data
            self._show_summary(summary)
            self._render_transcript(segments)
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
        self._submit("save_summary", lambda: self.controller.update_summary(session_id, text),
                     lambda value, error: self.status.set(error or "Resumo revisado salvo; o resultado original foi preservado."))

    def _show_summary(self, value):
        if isinstance(value, dict):
            text = json.dumps(value, ensure_ascii=False, indent=2)
        else:
            text = str(value or "Resumo local opcional. Revise os resultados e copie o texto para suas notas.")
        self.summary.configure(state="normal")
        self.summary.delete("1.0", "end")
        self.summary.insert("1.0", text[:65536])
        self.summary.configure(state="normal")

    def _poll(self):
        if self.closed:
            return
        try:
            self.bridge.drain()
            self.snapshot = self.controller.snapshot()
            state = self.snapshot.get("state", "idle")
            active = state in ("starting", "recording", "paused", "stopping")
            processing = self.snapshot.get("processing")
            self.start_button.configure(state="normal" if state == "idle" and not processing and self.settings_loaded else "disabled")
            self.stop_button.configure(state="normal" if active else "disabled")
            self.pause_button.configure(text="Retomar" if state == "paused" else "Pausar",
                state="normal" if state in ("recording", "paused") else "disabled")
            self.play_button.configure(state="normal" if state == "idle" and not processing else "disabled")
            error = self.snapshot.get("error")
            displayed_state = self.snapshot.get("last_status") if state == "idle" else state
            displayed_state = displayed_state or state
            self.record_status.set(f"{STATE_LABELS.get(displayed_state, displayed_state)} · {format_time(self.snapshot.get('elapsed'))}"
                                   + (" · Processando localmente" if processing else "")
                                   + (" · Captura parcial" if self.snapshot.get("partial") else "")
                                   + (f" · {error}" if error else ""))
            for track, meter in self.meters.items():
                value = float(self.snapshot.get("levels", {}).get(track, 0) or 0)
                meter.configure(value=max(0, min(1, value)) if math.isfinite(value) else 0)
            if self.previous_state in ("recording", "paused", "stopping") and not active:
                self.refresh_library()
            self.previous_state = state
            if self.processing_target is not None and not processing:
                target, self.processing_target = self.processing_target, None
                self.status.set(error or "Processamento local finalizado.")
                if target and not error:
                    self.refresh_outputs(target)
        except Exception as exc:
            self.status.set(f"Não foi possível atualizar o estado: {exc}")
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
