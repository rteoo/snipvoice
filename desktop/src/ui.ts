// Shared page pieces: header, the page's one message slot, dialogs.
import { RpcError } from "./bridge";
import { button, h, replace } from "./dom";
import { tr } from "./i18n";
import type { Tone } from "./types";

export function pageHeader(title: string, ...actions: (Node | null)[]): HTMLElement {
  return h("header", { class: "page-header" }, h("h1", { text: title }), h("div", { class: "btn-row" }, ...actions));
}

type Severity = "danger" | "warning" | "info" | "success";
const GLYPHS: Record<Severity, string> = { danger: "⊗", warning: "⚠", info: "ⓘ", success: "✓" };
const WORDS: Record<Severity, () => string> = {
  danger: () => tr("Erro"),
  warning: () => tr("Aviso"),
  info: () => tr("Informação"),
  success: () => tr("Concluído"),
};

function clock(): string {
  return new Date().toLocaleTimeString(document.documentElement.lang, { hour: "2-digit", minute: "2-digit" });
}

/** A page's single feedback slot: an InfoBar for trouble, one quiet line otherwise. */
export class MessageSlot {
  readonly element = h("div", { class: "message-slot", attrs: { role: "status", "aria-live": "polite" } });

  clear(): void {
    replace(this.element);
  }

  say(text: string): void {
    replace(this.element, h("p", { class: "quiet-line", text: `${text} · ${clock()}` }));
  }

  fail(title: string, body = "", options: { tone?: Severity; error?: unknown; action?: { label: string; run: () => void } } = {}): void {
    const tone = options.tone ?? "danger";
    const detail = options.error instanceof RpcError ? String((options.error.data as { detail?: string } | undefined)?.detail ?? "") : "";
    const message = options.error instanceof RpcError && !body ? options.error.message : body;
    const bar = h(
      "div",
      { class: `infobar ${tone}`, attrs: { role: tone === "danger" ? "alert" : "status" } },
      h("span", { class: "glyph", text: GLYPHS[tone], attrs: { "aria-hidden": "true" } }),
      h(
        "div",
        { class: "stack-sm" },
        h("div", { class: "title-line" }, h("span", { text: title }), h("small", { text: tr("{severity} às {time}", { severity: WORDS[tone](), time: clock() }) })),
        message ? h("p", { text: message }) : null,
      ),
      h(
        "div",
        { class: "actions" },
        options.action ? button(options.action.label, options.action.run) : null,
        button(tr("Fechar"), () => this.clear(), "quiet"),
      ),
      detail
        ? h("details", {}, h("summary", { text: tr("Detalhes") }), h("pre", { text: detail }), button(tr("Copiar"), () => void copyText(detail), "quiet"))
        : null,
    );
    replace(this.element, bar);
  }
}

export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

export function toneClass(tone: Tone): string {
  return `tone-${tone === "default" ? "muted" : tone}`;
}

export function toneText(tone: Tone): string {
  return tone === "danger" ? "text-danger" : tone === "warning" ? "text-warning" : tone === "ok" ? "text-success" : "";
}

/** A bounded decision. Resolves true for the primary action; Esc and Cancel resolve false. */
export function confirmDialog(options: { title: string; body: string; confirm: string; selectable?: boolean; danger?: boolean; details?: string }): Promise<boolean> {
  return new Promise((resolve) => {
    const opener = document.activeElement as HTMLElement | null;
    const dialog = h("dialog", { attrs: { "aria-labelledby": "dialog-title" } });
    const finish = (value: boolean) => {
      dialog.close();
      dialog.remove();
      opener?.focus();
      resolve(value);
    };
    const confirm = button(options.confirm, () => finish(true), options.danger ? "danger" : "primary");
    const body = h("p", { class: options.selectable ? "selectable" : "", text: options.body });
    body.style.whiteSpace = "pre-wrap";
    dialog.append(
      h("h2", { text: options.title, attrs: { id: "dialog-title" } }),
      body,
      ...(options.details ? [h("details", {}, h("summary", { text: tr("Detalhes") }), h("pre", { class: "selectable", text: options.details }))] : []),
      h(
        "div",
        { class: "dialog-actions" },
        options.selectable ? button(tr("Copiar aviso"), () => void copyText(options.body), "quiet") : null,
        button(tr("Cancelar"), () => finish(false)),
        confirm,
      ),
    );
    dialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      finish(false);
    });
    document.body.append(dialog);
    dialog.showModal();
    // A destructive choice starts on Cancel, as the Tk trash dialog does.
    (options.danger ? dialog.querySelector<HTMLButtonElement>(".dialog-actions button:not(.danger)") ?? confirm : confirm).focus();
  });
}

/** A permanent deletion: the confirm button unlocks only after the word is typed. */
export function typedConfirmDialog(options: { question: string; facts: string }): Promise<boolean> {
  return new Promise((resolve) => {
    const opener = document.activeElement as HTMLElement | null;
    const word = tr("EXCLUIR");
    const dialog = h("dialog", { attrs: { "aria-labelledby": "dialog-title" } });
    const finish = (value: boolean) => {
      dialog.close();
      dialog.remove();
      opener?.focus();
      resolve(value);
    };
    const input = h("input", { attrs: { type: "text", autocomplete: "off" } });
    const confirm = button(tr("Excluir definitivamente"), () => finish(true), "danger");
    confirm.disabled = true;
    input.addEventListener("input", () => {
      confirm.disabled = input.value.trim().toLocaleLowerCase() !== word.toLocaleLowerCase();
    });
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !confirm.disabled) finish(true);
    });
    dialog.append(
      h("h2", { text: tr("Excluir definitivamente"), attrs: { id: "dialog-title" } }),
      h("p", { text: options.question }),
      h("p", { class: "caption", text: options.facts }),
      h(
        "div",
        { class: "infobar danger", attrs: { role: "note" } },
        h("span", { class: "glyph", text: "⚠", attrs: { "aria-hidden": "true" } }),
        h("div", { class: "stack-sm" }, h("strong", { text: tr("Isso não pode ser desfeito.") }),
          h("p", { text: tr("Os arquivos são apagados do app para sempre. Isso não garante apagamento forense do disco.") })),
      ),
      h("label", { class: "field" }, h("span", { text: tr("Digite {word} para confirmar.", { word }) }), input),
      h("div", { class: "dialog-actions" }, button(tr("Cancelar"), () => finish(false)), confirm),
    );
    dialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      finish(false);
    });
    document.body.append(dialog);
    dialog.showModal();
    input.focus();
  });
}
