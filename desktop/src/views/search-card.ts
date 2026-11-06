// Busca: the word-search index and its repair.
import type { Bridge } from "../bridge";
import { button, h, replace } from "../dom";
import { tr } from "../i18n";
import type { SearchState } from "../types";
import type { MessageSlot } from "../ui";

interface Finished {
  ok: boolean;
  cancelled?: boolean;
  detail?: string;
}

export class SearchCard {
  readonly element = h("section", { class: "card stack-sm" });
  private state: SearchState | null = null;
  private outcome = "";

  constructor(
    private readonly bridge: Bridge,
    private readonly messages: MessageSlot,
  ) {
    bridge.on<{ done: number; total: number | null }>("search.progress", (progress) => {
      if (!this.state) return;
      this.state.repair = { done: progress.done, total: progress.total };
      this.render();
    });
    bridge.on<Finished>("search.finished", (outcome) => {
      this.outcome = outcome.ok
        ? tr("Índice de busca reconstruído.")
        : outcome.cancelled
          ? tr("Reparo cancelado.")
          : tr("Não foi possível reconstruir o índice de busca");
      void this.load();
    });
  }

  async load(): Promise<void> {
    try {
      this.state = await this.bridge.call<SearchState>("search.state");
    } catch (error) {
      this.messages.fail(tr("Índice de busca: estado desconhecido"), "", { error });
    }
    this.render();
  }

  private render(): void {
    const state = this.state;
    if (!state) {
      replace(this.element, h("h2", { text: tr("Busca") }), h("p", { class: "caption", text: tr("Carregando…") }));
      return;
    }
    const repairing = state.repair !== null;
    const repair = repairing
      ? button(tr("Cancelar"), () => void this.call("search.cancelRepair"), "quiet")
      : button(tr("Reparar busca"), () => void this.call("search.repair"));
    replace(
      this.element,
      h("h2", { text: tr("Busca") }),
      h("p", { class: "caption", text: tr("Use se a busca da biblioteca não encontrar uma gravação que você sabe que existe.") }),
      h("div", { class: "btn-row" }, repair),
      repairing ? h("progress", { attrs: { max: state.repair?.total || 1, value: state.repair?.done ?? 0, "aria-label": tr("Índice de busca: reconstruindo…") } }) : null,
      h("p", { class: "caption", attrs: { role: "status" }, text: repairing ? tr("Índice de busca: reconstruindo…") : this.outcome || state.index }),
    );
  }

  private async call(method: string): Promise<void> {
    this.outcome = "";
    try {
      await this.bridge.call(method, {});
    } catch (error) {
      this.messages.fail(tr("Não foi possível concluir a ação."), "", { error });
    }
    void this.load();
  }
}
