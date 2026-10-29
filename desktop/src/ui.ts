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
export function confirmDialog(options: { title: string; body: string; confirm: string; selectable?: boolean; danger?: boolean }): Promise<boolean> {
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
    confirm.focus();
  });
}
