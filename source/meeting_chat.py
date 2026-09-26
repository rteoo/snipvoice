"""Small, theme-aware chat surface for the meeting library.

The widget owns presentation and interaction only.  The caller owns turns,
storage, inference, and stale-result handling.
"""

from __future__ import annotations

import tkinter as tk

from i18n import N_, tr


SUGGESTIONS = (
    N_("Resuma os pontos principais"),
    N_("Quais decisões foram tomadas?"),
    N_("Liste as próximas ações"),
)

UNCERTAINTY_LABELS = {
    "low": N_("Incerteza baixa"),
    "medium": N_("Incerteza média"),
    "high": N_("Incerteza alta"),
}


class MeetingChat(tk.Frame):
    """Scrollable conversation with a fixed multiline composer."""

    def __init__(self, parent, theme, *, on_send, on_new, on_save, on_copy, on_source):
        self.theme = theme
        self.on_send = on_send
        self.on_new = on_new
        self.on_save = on_save
        self.on_copy = on_copy
        self.on_source = on_source
        self.status = tk.StringVar(parent, "")
        self._busy = False
        self._can_save = True
        self._can_send = True
        self._message_widgets = []
        self._answer_controls = []

        super().__init__(parent, bg=theme.surface)
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        history = tk.Frame(self, bg=theme.surface)
        history.grid(row=0, column=0, sticky="nsew")
        history.grid_rowconfigure(0, weight=1)
        history.grid_columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(
            history, bg=theme.surface, highlightthickness=0, borderwidth=0,
        )
        self.canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar = tk.Scrollbar(
            history, orient="vertical", command=self.canvas.yview,
            bg=theme.surface_alt, troughcolor=theme.surface,
            activebackground=theme.control_active, relief="flat", width=12,
        )
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self._history = tk.Frame(self.canvas, bg=theme.surface)
        self._history_window = self.canvas.create_window(
            (0, 0), window=self._history, anchor="nw",
        )
        self._history.bind("<Configure>", self._history_changed)
        self.canvas.bind("<Configure>", self._canvas_changed)
        self._bind_scroll(history)
        self._bind_keyboard_scroll(self.canvas)

        self._empty = tk.Frame(self._history, bg=theme.surface)
        self._empty_title = tk.Label(
            self._empty, text=tr("Pergunte sobre esta gravação"),
            bg=theme.surface, fg=theme.text_strong,
            font=theme.font(12, "bold"), anchor="w", justify="left", wraplength=520,
        )
        self._empty_title.pack(anchor="w", pady=(theme.space_lg, theme.space_xs))
        self._empty_subtitle = tk.Label(
            self._empty, text=tr("Use uma sugestão ou escreva sua própria pergunta."),
            bg=theme.surface, fg=theme.text_muted, font=theme.font(), anchor="w",
            justify="left", wraplength=520,
        )
        self._empty_subtitle.pack(anchor="w", pady=(0, theme.space_sm))
        suggestions = tk.Frame(self._empty, bg=theme.surface)
        suggestions.pack(anchor="w", fill="x")
        for suggestion in SUGGESTIONS:
            button = self._button(suggestions, tr(suggestion), lambda value=tr(suggestion): self._use_suggestion(value))
            button.pack(anchor="w", pady=2)

        composer = tk.Frame(self, bg=theme.card, highlightthickness=1,
                            highlightbackground=theme.border)
        self._composer = composer
        composer.grid(row=1, column=0, sticky="ew", pady=(theme.space_sm, 0))
        composer.grid_columnconfigure(0, weight=1)
        top = tk.Frame(composer, bg=theme.card)
        top.grid(row=0, column=0, sticky="ew", padx=theme.space_sm, pady=(theme.space_sm, 0))
        top.grid_columnconfigure(0, weight=1)
        tk.Label(
            top, text=tr("Pergunte sobre esta gravação"), bg=theme.card,
            fg=theme.text_strong, font=theme.font(9, "bold"), anchor="w",
        ).grid(row=0, column=0, sticky="w")
        self._keyboard_hint = tk.Label(
            top, text=tr("Enter envia · Shift+Enter quebra linha"), bg=theme.card,
            fg=theme.text_muted, font=theme.font(8), anchor="w",
        )
        self._keyboard_hint.grid(row=1, column=0, sticky="w", pady=(1, theme.space_xs))
        self.composer = tk.Text(
            top, height=3, wrap="word", undo=True, font=theme.font(),
            **theme.text_colors(),
        )
        self.composer.grid(row=2, column=0, sticky="ew")
        self.composer.bind("<Return>", self._composer_return)
        self.composer.bind("<Control-Return>", self._send_event)
        self._bind_scroll(self.composer)
        self._status_label = tk.Label(
            composer, textvariable=self.status, bg=theme.card, fg=theme.text_muted,
            font=theme.font(8), anchor="w", justify="left", wraplength=520,
        )
        self._status_label.grid(row=1, column=0, sticky="ew", padx=theme.space_sm,
                                pady=(theme.space_xs, 0))
        actions = tk.Frame(composer, bg=theme.card)
        actions.grid(row=2, column=0, sticky="ew", padx=theme.space_sm,
                     pady=(theme.space_xs, theme.space_sm))
        self.new_button = self._button(actions, tr("Nova conversa"), self._new)
        self.new_button.pack(side="left")
        self.send_button = self._button(actions, tr("Enviar"), self._send, accent=True)
        self.send_button.pack(side="right")
        composer.bind("<Configure>", self._composer_changed)

        self._empty.pack(fill="x", padx=theme.space_lg)
        self._set_empty(True)

    def _button(self, parent, text, command, accent=False):
        colors = {
            "bg": self.theme.accent if accent else self.theme.control,
            "fg": self.theme.text_on_accent if accent else self.theme.text,
            "activebackground": self.theme.accent_active if accent else self.theme.control_active,
            "activeforeground": self.theme.text_on_accent if accent else self.theme.text,
            "disabledforeground": self.theme.text_muted,
            "font": self.theme.font(), "relief": "flat", "bd": 0,
            "highlightthickness": 1, "highlightbackground": self.theme.control_border,
            "highlightcolor": self.theme.focus_ring,
            "padx": self.theme.space_sm, "pady": self.theme.space_xs,
        }
        return tk.Button(parent, text=text, command=command, **colors)

    def _bind_scroll(self, widget):
        widget.bind("<MouseWheel>", self._mousewheel, add="+")
        widget.bind("<Button-4>", lambda _event: self.canvas.yview_scroll(-3, "units"), add="+")
        widget.bind("<Button-5>", lambda _event: self.canvas.yview_scroll(3, "units"), add="+")

    def _bind_keyboard_scroll(self, widget):
        widget.bind("<Prior>", lambda _event: self._page_scroll(-1), add="+")
        widget.bind("<Next>", lambda _event: self._page_scroll(1), add="+")
        widget.bind("<Home>", lambda _event: self.canvas.yview_moveto(0), add="+")
        widget.bind("<End>", lambda _event: self.canvas.yview_moveto(1), add="+")

    def _page_scroll(self, direction):
        self.canvas.yview_scroll(direction, "pages")
        return "break"

    def _bind_history_scroll_tree(self, widget):
        self._bind_scroll(widget)
        self._bind_keyboard_scroll(widget)
        for child in widget.winfo_children():
            self._bind_history_scroll_tree(child)

    def _mousewheel(self, event):
        steps = max(1, abs(int(event.delta)) // 120)
        self.canvas.yview_scroll(-steps if event.delta > 0 else steps, "units")
        return "break"

    def _history_changed(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _canvas_changed(self, event):
        self.canvas.itemconfigure(self._history_window, width=max(1, event.width))
        self._refresh_message_widths(event.width)
        self._refresh_empty_width(event.width)

    def _refresh_message_widths(self, canvas_width=None):
        width = canvas_width if canvas_width is not None else self.canvas.winfo_width()
        width = max(220, width - self.theme.space_lg * 2)
        for message, inset in self._message_widgets:
            try:
                if message.winfo_exists():
                    message.configure(width=max(
                        140, width - inset - self.theme.space_sm * 2 - self.theme.space_xs,
                    ))
            except tk.TclError:
                continue

    def _message_width(self):
        width = self.canvas.winfo_width()
        if width <= 1:
            return 520
        return max(140, width - self.theme.space_lg * 2 - self.theme.space_sm * 2 - self.theme.space_xs)

    def _composer_changed(self, event):
        width = max(180, event.width - self.theme.space_sm * 2)
        for widget in (self._status_label, self._keyboard_hint, self._empty_title):
            try:
                if widget.winfo_exists():
                    widget.configure(wraplength=width)
            except tk.TclError:
                # Tk may deliver a queued Configure event while a parent
                # window is tearing down its child widgets.
                continue

    def _refresh_empty_width(self, canvas_width):
        width = max(180, canvas_width - self.theme.space_lg * 2)
        for widget in (self._empty_title, self._empty_subtitle):
            try:
                if widget.winfo_exists():
                    widget.configure(wraplength=width)
            except tk.TclError:
                continue

    def _set_empty(self, visible):
        if visible:
            self._empty.pack(fill="x", padx=self.theme.space_lg)
        else:
            self._empty.pack_forget()

    def _composer_return(self, event):
        if event.state & 0x0001:
            self.composer.insert("insert", "\n")
            return "break"
        return self._send_event(event)

    def _send_event(self, _event=None):
        self._send()
        return "break"

    def _send(self):
        if str(self.send_button["state"]) == "disabled":
            return
        question = self.get_question().strip()
        if not question:
            self.status.set(tr("Escreva uma pergunta primeiro."))
            self.focus_composer()
            return
        self.on_send(question)

    def _new(self):
        self.on_new()

    def _use_suggestion(self, value):
        self.set_question(value)
        self.focus_composer()

    def set_controls(self, *, busy=False, can_save=True, can_send=True):
        self._busy, self._can_save, self._can_send = bool(busy), bool(can_save), bool(can_send)
        self.send_button.configure(state="normal" if self._can_send and not self._busy else "disabled")
        self.new_button.configure(state="normal" if not self._busy else "disabled")
        for status, copy_button, save_button, saving, saved in self._answer_controls:
            complete = status == "complete"
            copy_button.configure(state="normal" if complete else "disabled")
            save_button.configure(
                state="disabled" if self._busy or saving or saved or not complete or not self._can_save
                else "normal",
            )

    def set_height(self, px):
        self.configure(height=max(240, int(px)))
        self.grid_propagate(False)

    def get_question(self):
        return self.composer.get("1.0", "end-1c")

    def set_question(self, text):
        self.composer.delete("1.0", "end")
        self.composer.insert("1.0", str(text))

    def focus_composer(self):
        self.composer.focus_set()

    def render(self, turns):
        for child in self._history.winfo_children():
            if child is not self._empty:
                child.destroy()
        self._message_widgets = []
        self._answer_controls = []
        turns = list(turns or ())
        self._set_empty(not turns)
        for turn in turns:
            self._render_turn(turn)
        self._history.update_idletasks()
        self._history_changed()
        self._refresh_message_widths()
        if turns:
            self.canvas.yview_moveto(1.0)

    def _render_turn(self, turn):
        turn_id = turn.get("id")
        outer = tk.Frame(self._history, bg=self.theme.surface)
        outer.pack(fill="x", padx=self.theme.space_lg, pady=(self.theme.space_sm, 0))
        question = tk.Message(
            outer, text=str(turn.get("question", "")), bg=self.theme.surface_alt,
            fg=self.theme.text_strong, font=self.theme.font(), justify="left", anchor="w",
            width=max(180, self._message_width() - self.theme.space_xl),
            padx=self.theme.space_sm, pady=self.theme.space_xs,
        )
        question.pack(anchor="e", padx=(self.theme.space_xl, 0))
        self._message_widgets.append((question, self.theme.space_xl))
        tk.Label(outer, text=tr("Você"), bg=self.theme.surface, fg=self.theme.text_muted,
                 font=self.theme.font(8), anchor="e").pack(anchor="e")

        answer_box = tk.Frame(outer, bg=self.theme.surface)
        answer_box.pack(fill="x", pady=(self.theme.space_sm, 0))
        tk.Label(answer_box, text="Snipvoice", bg=self.theme.surface,
                 fg=self.theme.text_muted, font=self.theme.font(8), anchor="w").pack(anchor="w")
        answer_text = turn.get("answer") or (tr("Pensando…") if turn.get("status") == "pending" else "")
        message = tk.Message(
            answer_box, text=str(answer_text), bg=self.theme.card, fg=self.theme.text,
            font=self.theme.font(), justify="left", anchor="nw", width=520,
            padx=self.theme.space_sm, pady=self.theme.space_sm,
        )
        message.pack(fill="x")
        self._message_widgets.append((message, 0))
        if turn.get("status") == "error":
            error_message = tk.Message(
                answer_box, text=str(turn.get("error") or tr("Não foi possível responder.")),
                bg=self.theme.surface, fg=self.theme.danger, font=self.theme.font(8),
                justify="left", anchor="w", width=self._message_width(),
            )
            error_message.pack(fill="x", pady=(2, 0))
            self._message_widgets.append((error_message, 0))
        uncertainty = str(turn.get("uncertainty") or "").strip()
        label = UNCERTAINTY_LABELS.get(uncertainty.casefold())
        uncertainty = tr(label) if label else uncertainty
        if uncertainty:
            uncertainty_message = tk.Message(
                answer_box, text=uncertainty, bg=self.theme.surface,
                fg=self.theme.warning, font=self.theme.font(8),
                justify="left", anchor="w", width=self._message_width(),
            )
            uncertainty_message.pack(fill="x", pady=(2, 0))
            self._message_widgets.append((uncertainty_message, 0))
        sources = tk.Frame(answer_box, bg=self.theme.surface)
        sources.pack(fill="x", pady=(self.theme.space_xs, 0))
        for index, citation in enumerate(turn.get("citations") or (), 1):
            self._button(sources, tr("Fonte {index}", index=index), lambda value=citation: self.on_source(turn_id, value)).pack(
                anchor="w", pady=1,
            )
        actions = tk.Frame(answer_box, bg=self.theme.surface)
        actions.pack(fill="x", pady=(self.theme.space_xs, 0))
        copy = self._button(actions, tr("Copiar"), lambda: self.on_copy(turn_id))
        copy.pack(side="left", pady=1)
        saving = bool(turn.get("saving"))
        saved = bool(turn.get("saved"))
        save = self._button(actions, tr("Salvando…") if saving else (tr("Salvo") if saved else tr("Salvar")),
                            lambda: self.on_save(turn_id))
        status = str(turn.get("status") or "")
        complete = status == "complete"
        copy.configure(state="normal" if complete else "disabled")
        save.configure(state=("disabled" if self._busy or saving or saved or not complete or not self._can_save
                              else "normal"))
        save.pack(side="left", padx=(self.theme.space_xs, 0), pady=1)
        self._answer_controls.append((status, copy, save, saving, saved))
        self._bind_history_scroll_tree(outer)


__all__ = ["MeetingChat", "SUGGESTIONS"]
