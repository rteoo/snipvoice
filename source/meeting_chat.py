"""Small, theme-aware chat surface for the meeting library.

The widget owns presentation and interaction only.  The caller owns turns,
storage, inference, and stale-result handling.
"""

from __future__ import annotations

import tkinter as tk

from i18n import N_, tr
import ui_theme


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
# How much to doubt an answer sets the tone of its label; the written level
# always travels with it. Low is quiet, medium asks for care, high is trouble.
UNCERTAINTY_TONES = {"low": "text_muted", "medium": "warning", "high": "danger"}


class MeetingChat(tk.Frame):
    """Scrollable conversation with a fixed multiline composer."""

    def __init__(self, parent, theme, *, on_send, on_cancel, on_new, on_save, on_copy, on_source,
                 on_retry=None, copy_details=None):
        self.theme = theme
        self.on_send = on_send
        self.on_cancel = on_cancel
        self.on_new = on_new
        self.on_save = on_save
        self.on_copy = on_copy
        self.on_source = on_source
        # ``on_retry(turn_id)`` resends a failed question; ``copy_details(text, done)``
        # writes an InfoBar's technical text to the clipboard off the GUI thread.
        self.on_retry = on_retry
        self.copy_details = copy_details
        self.status = tk.StringVar(parent, "")
        self._busy = False
        self._pending = False
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
            button.pack(anchor="w", pady=self.theme.space_xs)

        # A notice sits between the conversation and the composer, out of the way
        # of both; it takes no height until a page has something to say.
        self._notice = tk.Frame(self, bg=theme.surface)
        self._notice.grid(row=1, column=0, sticky="ew", pady=(theme.space_sm, 0))
        self._notice.grid_remove()
        composer = tk.Frame(self, bg=theme.card, highlightthickness=1,
                            highlightbackground=theme.border)
        self._composer = composer
        composer.grid(row=2, column=0, sticky="ew", pady=(theme.space_sm, 0))
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
        self._keyboard_hint.grid(row=1, column=0, sticky="w", pady=(self.theme.space_xs, theme.space_xs))
        self.composer = tk.Text(
            top, height=3, wrap="word", undo=True, font=theme.font(),
            **theme.text_colors(), **theme.entry_chrome(),
        )
        ui_theme.focus_ring(self.composer, theme, shrink=("bd",))
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
        # Cancel exists only while an answer is pending; it sits left of Enviar,
        # which does not move when it comes and goes.
        self.cancel_button = self._button(actions, tr("Cancelar"), self._cancel)
        self.cancel_button.pack(side="right", padx=(0, theme.space_xs))
        self.cancel_button.pack_forget()
        composer.bind("<Configure>", self._composer_changed)

        self._empty.pack(fill="x", padx=theme.space_lg)
        self._set_empty(True)

    def _button(self, parent, text, command, accent=False):
        return ui_theme.button(parent, text, command, "primary" if accent else "neutral",
                               resolved=self.theme)

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
                    if isinstance(message, tk.Text):
                        self._answer_height(message)
                    else:
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

    @staticmethod
    def _answer_height(message):
        # Text width is in characters, unlike Message's pixel width. Pack owns
        # the available width; display-line counting owns the wrapped height.
        lines = message.count("1.0", "end-1c", "displaylines")
        height = max(1, (int(lines[0]) if lines else 0) + 1)
        if int(message.cget("height")) != height:
            message.configure(height=height)

    @staticmethod
    def _select_answer(message):
        message.tag_add("sel", "1.0", "end-1c")
        return "break"

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

    def _cancel(self):
        if self._pending:
            self.on_cancel()

    def _use_suggestion(self, value):
        self.set_question(value)
        self.focus_composer()

    def set_controls(self, *, busy=False, pending=False, can_save=True, can_send=True):
        """``pending``: this conversation waits for an answer that Cancel can stop."""
        self._busy, self._can_save, self._can_send = bool(busy), bool(can_save), bool(can_send)
        self._pending = bool(pending)
        self.send_button.configure(state="normal" if self._can_send and not self._busy else "disabled")
        if self._pending and not self.cancel_button.winfo_manager():
            self.cancel_button.pack(side="right", padx=(0, self.theme.space_xs))
        elif not self._pending and self.cancel_button.winfo_manager():
            self.cancel_button.pack_forget()
        self.new_button.configure(state="normal" if not self._busy else "disabled")
        for status, copy_button, save_button, saving, saved in self._answer_controls:
            complete = status == "complete"
            copy_button.configure(state="normal" if complete else "disabled")
            save_button.configure(
                state="disabled" if self._busy or saving or saved or not complete or not self._can_save
                else "normal",
            )

    def show_notice(self, tone, title, body, action=None):
        """Say something the conversation cannot, as an InfoBar between it and the composer."""
        self.clear_notice()
        ui_theme.info_bar(self._notice, self.theme, tone, title, body, action=action).pack(fill="x")
        self._notice.grid()

    def clear_notice(self):
        for child in self._notice.winfo_children():
            child.destroy()
        self._notice.grid_remove()

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
        tk.Label(answer_box, text="SnipVoice", bg=self.theme.surface,
                 fg=self.theme.text_muted, font=self.theme.font(8), anchor="w").pack(anchor="w")
        if turn.get("status") == "error":
            retry = ((tr("Tentar novamente"), lambda: self.on_retry(turn_id))
                     if self.on_retry is not None else None)
            ui_theme.info_bar(
                answer_box, self.theme, "danger", tr("Não foi possível responder"),
                str(turn.get("error") or tr("Nada foi salvo. Tente enviar a pergunta de novo.")),
                action=retry, details=turn.get("details") or None, copy=self.copy_details,
            ).pack(fill="x")
            self._bind_history_scroll_tree(outer)
            return
        answer_text = turn.get("answer") or (tr("Pensando…") if turn.get("status") == "pending" else "")
        message = tk.Text(
            answer_box, bg=self.theme.card, fg=self.theme.text,
            font=self.theme.body(), wrap="word", width=1, height=1,
            padx=self.theme.space_sm, pady=self.theme.space_sm, relief="flat", bd=0,
            selectbackground=self.theme.select_bg, selectforeground=self.theme.select_fg,
            highlightthickness=1, highlightbackground=self.theme.border,
            highlightcolor=self.theme.focus_ring, takefocus=True,
        )
        message.insert("1.0", str(answer_text))
        message.configure(state="disabled")
        ui_theme.focus_ring(message, self.theme)
        message.bind("<Control-a>", lambda _event, widget=message: self._select_answer(widget))
        if self.theme.system == "darwin":
            message.bind("<Command-a>", lambda _event, widget=message: self._select_answer(widget))
        message.bind("<Configure>", lambda _event, widget=message: self._answer_height(widget))
        message.pack(fill="x")
        self._message_widgets.append((message, 0))
        uncertainty = str(turn.get("uncertainty") or "").strip()
        level = uncertainty.casefold()
        label = UNCERTAINTY_LABELS.get(level)
        uncertainty = tr(label) if label else uncertainty
        if uncertainty:
            uncertainty_message = tk.Message(
                answer_box, text=uncertainty, bg=self.theme.surface,
                fg=getattr(self.theme, UNCERTAINTY_TONES.get(level, "text_muted")),
                font=self.theme.caption(), justify="left", anchor="w", width=self._message_width(),
            )
            uncertainty_message.pack(fill="x", pady=(self.theme.space_xs, 0))
            self._message_widgets.append((uncertainty_message, 0))
        sources = tk.Frame(answer_box, bg=self.theme.surface)
        sources.pack(fill="x", pady=(self.theme.space_xs, 0))
        for index, citation in enumerate(turn.get("citations") or (), 1):
            self._button(sources, tr("Fonte {index}", index=index), lambda value=citation: self.on_source(turn_id, value)).pack(
                anchor="w", pady=self.theme.space_xs,
            )
        actions = tk.Frame(answer_box, bg=self.theme.surface)
        actions.pack(fill="x", pady=(self.theme.space_xs, 0))
        copy = self._button(actions, tr("Copiar"), lambda: self.on_copy(turn_id))
        copy.pack(side="left", pady=self.theme.space_xs)
        saving = bool(turn.get("saving"))
        saved = bool(turn.get("saved"))
        save = self._button(actions, tr("Salvando…") if saving else (tr("Salvo") if saved else tr("Salvar")),
                            lambda: self.on_save(turn_id))
        status = str(turn.get("status") or "")
        complete = status == "complete"
        copy.configure(state="normal" if complete else "disabled")
        save.configure(state=("disabled" if self._busy or saving or saved or not complete or not self._can_save
                              else "normal"))
        save.pack(side="left", padx=(self.theme.space_xs, 0), pady=self.theme.space_xs)
        self._answer_controls.append((status, copy, save, saving, saved))
        self._bind_history_scroll_tree(outer)


__all__ = ["MeetingChat", "SUGGESTIONS"]
