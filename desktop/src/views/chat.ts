// Chat: questions about one recording, answered locally. Conversations live
// only in this window; an answer persists only when the person saves it.
import { RpcError, type Bridge } from "../bridge";
import { button, h, replace } from "../dom";
import { tr } from "../i18n";
import type { ChatAnswer, Tone } from "../types";
import { copyText, type MessageSlot, toneText } from "../ui";
import { CANCELLED } from "./summary";

const MAX_QUESTION = 2000;
const MAX_TURNS = 24;
const MAX_CONVERSATIONS = 8;

interface Turn {
  question: string;
  status: "pending" | "complete" | "failed";
  answer?: string;
  answerId?: string;
  sources?: number;
  uncertainty?: string;
  uncertaintyTone?: Tone;
  saving?: boolean;
  saved?: boolean;
}

export class ChatPanel {
  readonly element = h("div", { class: "section-body chat" });
  private readonly conversations = new Map<string, Turn[]>();
  private sessionId: string | null = null;
  private modelReady = false;
  private canSave = false;
  private draft = "";
  private pending: string | null = null;

  constructor(
    private readonly bridge: Bridge,
    private readonly messages: MessageSlot,
  ) {}

  show(sessionId: string, modelReady: boolean, canSave: boolean): void {
    this.sessionId = sessionId;
    this.modelReady = modelReady;
    this.canSave = canSave;
    this.render();
  }

  private turns(): Turn[] {
    const id = this.sessionId;
    if (!id) return [];
    let turns = this.conversations.get(id);
    if (!turns) {
      turns = [];
      this.conversations.set(id, turns);
      // ceiling: eight conversations per window, as the Tk manager keeps.
      while (this.conversations.size > MAX_CONVERSATIONS) {
        const oldest = [...this.conversations.keys()].find((key) => key !== id && key !== this.pending);
        if (!oldest) break;
        this.conversations.delete(oldest);
      }
    }
    return turns;
  }

  private render(): void {
    const turns = this.turns();
    const busy = this.pending !== null;
    const log = h("div", { class: "chat-log", attrs: { role: "log", "aria-live": "polite" } });
    if (!turns.length) {
      log.append(
        h("p", { class: "caption", text: tr("Pergunte sobre esta gravação") }),
        h("div", { class: "btn-row" }, ...[tr("Resuma os pontos principais"), tr("Quais decisões foram tomadas?"), tr("Liste as próximas ações")].map((text) => {
          const chip = button(text, () => void this.ask(text), "quiet");
          chip.disabled = busy || !this.modelReady;
          return chip;
        })),
      );
    }
    for (const turn of turns) log.append(...this.renderTurn(turn));

    const composer = h("textarea", { attrs: { rows: 3, maxlength: MAX_QUESTION, "aria-label": tr("Pergunte sobre esta gravação") } });
    composer.placeholder = tr("Enter envia · Shift+Enter quebra linha");
    composer.value = this.draft;
    composer.disabled = !this.modelReady;
    composer.addEventListener("input", () => (this.draft = composer.value));
    composer.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        void this.ask(composer.value);
      }
    });
    const send = button(tr("Enviar"), () => void this.ask(composer.value), "primary");
    send.disabled = busy || !this.modelReady;
    const cancel = this.pending === this.sessionId ? button(tr("Cancelar"), () => void this.cancel(), "quiet") : null;
    const fresh = button(tr("Nova conversa"), () => this.newConversation(), "quiet");
    fresh.disabled = !turns.length || this.pending === this.sessionId;

    replace(
      this.element,
      this.modelReady
        ? null
        : h(
            "div",
            { class: "infobar warning", attrs: { role: "status" } },
            h("span", { class: "glyph", text: "⚠", attrs: { "aria-hidden": "true" } }),
            h("div", { class: "stack-sm" }, h("strong", { text: tr("Falta um modelo de resumo") }), h("p", { text: tr("O chat responde com um modelo local. Instale um para conversar sobre esta gravação.") })),
          ),
      log,
      h("div", { class: "chat-composer" }, composer, h("div", { class: "btn-row" }, fresh, cancel, send)),
    );
    log.scrollTop = log.scrollHeight;
    if (this.modelReady && !busy) composer.focus({ preventScroll: true });
  }

  private renderTurn(turn: Turn): HTMLElement[] {
    const question = h("div", { class: "chat-turn question" }, h("small", { text: tr("Você") }), h("p", { class: "selectable", text: turn.question }));
    if (turn.status === "pending") return [question, h("p", { class: "caption", text: tr("Pensando…") })];
    if (turn.status === "failed") {
      return [question, h("div", { class: "chat-turn" }, h("p", { class: "text-danger", text: tr("Resposta não concluída. Tente novamente.") }), button(tr("Tentar novamente"), () => void this.retry(turn), "quiet"))];
    }
    const actions = h("div", { class: "btn-row" }, button(tr("Copiar"), () => void this.copy(turn), "quiet"));
    if (this.canSave) {
      const save = button(turn.saved ? tr("Salvo") : turn.saving ? tr("Salvando…") : tr("Salvar"), () => void this.save(turn), "quiet");
      save.disabled = Boolean(turn.saved || turn.saving);
      actions.append(save);
    }
    const facts = [turn.sources ? (turn.sources === 1 ? tr("1 fonte na transcrição") : tr("{count} fontes na transcrição", { count: turn.sources })) : ""].filter(Boolean).join(" · ");
    return [
      question,
      h(
        "div",
        { class: "chat-turn answer" },
        h("p", { class: "selectable", text: turn.answer ?? "" }),
        turn.uncertainty ? h("small", { class: toneText(turn.uncertaintyTone ?? "muted") || "caption", text: turn.uncertainty }) : null,
        facts ? h("small", { class: "caption", text: facts }) : null,
        actions,
      ),
    ];
  }

  private async ask(text: string): Promise<void> {
    const sessionId = this.sessionId;
    const question = text.trim();
    if (!sessionId || this.pending) return;
    if (!question) return this.messages.say(tr("Digite uma pergunta sobre a gravação."));
    if (question.length > MAX_QUESTION) return this.messages.say(tr("Use uma pergunta de até 2.000 caracteres."));
    const turns = this.turns();
    if (turns.length >= MAX_TURNS) return this.messages.say(tr("Esta conversa chegou a 24 perguntas. Salve o que precisa e use Nova conversa."));
    const history = turns.filter((turn) => turn.status === "complete").map((turn) => ({ question: turn.question, answer: turn.answer ?? "" }));
    const turn: Turn = { question, status: "pending" };
    turns.push(turn);
    this.pending = sessionId;
    this.draft = "";
    this.messages.clear();
    this.render();
    try {
      const answer = await this.bridge.call<ChatAnswer>("library.ask", { id: sessionId, question, history });
      Object.assign(turn, { status: "complete", ...answer });
    } catch (error) {
      if (error instanceof RpcError && error.code === CANCELLED) {
        turns.splice(turns.indexOf(turn), 1);
        if (this.sessionId === sessionId) this.draft = question;
        this.messages.say(tr("Resposta cancelada. A pergunta voltou para o campo de texto."));
      } else {
        turn.status = "failed";
        this.messages.fail(tr("Não foi possível responder"), "", { error });
      }
    }
    this.pending = null;
    if (this.sessionId === sessionId) this.render();
  }

  private async retry(turn: Turn): Promise<void> {
    const turns = this.turns();
    turns.splice(turns.indexOf(turn), 1);
    await this.ask(turn.question);
  }

  private async cancel(): Promise<void> {
    try {
      await this.bridge.call("library.cancelJob", { kind: "ask" });
    } catch (error) {
      this.messages.fail(tr("Não foi possível concluir a ação."), "", { error });
    }
  }

  private newConversation(): void {
    if (!this.sessionId) return;
    this.conversations.set(this.sessionId, []);
    this.messages.say(tr("Nova conversa. Respostas salvas continuam nos relatórios."));
    this.render();
  }

  private async copy(turn: Turn): Promise<void> {
    if (await copyText(turn.answer ?? "")) this.messages.say(tr("Resposta copiada."));
    else this.messages.fail(tr("Não foi possível copiar a resposta"), tr("Selecione o texto e pressione Ctrl+C"), { tone: "warning" });
  }

  private async save(turn: Turn): Promise<void> {
    const sessionId = this.sessionId;
    if (!sessionId || !turn.answerId || turn.saving || turn.saved) return;
    turn.saving = true;
    this.render();
    try {
      await this.bridge.call("library.saveAnswer", { id: sessionId, answerId: turn.answerId });
      turn.saved = true;
      this.messages.say(tr("Resposta salva nos relatórios."));
    } catch (error) {
      this.messages.fail(tr("A resposta não foi salva"), tr("Ela continua nesta conversa. Tente salvar de novo."), { error });
    }
    turn.saving = false;
    if (this.sessionId === sessionId) this.render();
  }
}
