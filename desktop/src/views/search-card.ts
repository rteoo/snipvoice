// Busca: word search always works; semantic search needs its model and runtime.
import type { Bridge } from "../bridge";
import { button, h, replace } from "../dom";
import { tr } from "../i18n";
import type { Confirmable, SearchState } from "../types";
import { confirmDialog, type MessageSlot } from "../ui";

interface Finished {
  kind: "download" | "repair";
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
    bridge.on<SearchState>("search.changed", (state) => {
      this.state = state;
      this.render();
    });
    bridge.on<{ kind: "download" | "repair"; done: number; total: number | null }>("search.progress", (progress) => {
      if (!this.state) return;
      this.state[progress.kind] = { done: progress.done, total: progress.total };
      this.render();
    });
    bridge.on<Finished>("search.finished", (outcome) => {
      this.outcome = outcome.ok
        ? outcome.kind === "download" ? tr("Operação semântica concluída.") : tr("Índice de busca reconstruído.")
        : outcome.cancelled
          ? tr("Cancelamento solicitado; busca por palavras disponível.")
          : outcome.kind === "download" ? tr("Operação semântica cancelada ou indisponível. Busca por palavras disponível.") : tr("Não foi possível reconstruir o índice de busca");
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
    const toggle = h("input", { attrs: { type: "checkbox", id: "search-semantic" } });
    toggle.checked = state.enabled;
    toggle.addEventListener("change", () => void this.call("search.setSemantic", { enabled: toggle.checked }));
    const percent = (job: { done: number; total: number | null } | null) => (job?.total ? Math.min(100, Math.floor((100 * job.done) / job.total)) : null);
    const downloading = state.download !== null;
    const modelState = state.installed ? tr("Baixado") : downloading ? (percent(state.download) === null ? tr("Baixando…") : tr("Baixando {percent}%", { percent: percent(state.download) ?? 0 })) : tr("Não baixado");
    const modelAction = downloading
      ? button(tr("Cancelar"), () => void this.call("search.cancel", {}), "quiet")
      : state.installed
        ? null
        : button(tr("Instalar modelo…"), () => void this.download());
    const reindex = button(tr("Reindexar"), () => void this.call("search.reindex", {}));
    reindex.disabled = !state.enabled || state.semantic === "rebuilding";
    const cancelIndex = state.semantic === "rebuilding" ? button(tr("Cancelar"), () => void this.call("search.cancel", {}), "quiet") : null;
    const repairing = state.repair !== null;
    const repair = repairing
      ? button(tr("Cancelar"), () => void this.call("search.cancelRepair", {}), "quiet")
      : button(tr("Reparar busca"), () => void this.call("search.repair", {}));
    replace(
      this.element,
      h("h2", { text: tr("Busca") }),
      h("label", { class: "check" }, toggle, h("span", { text: tr("Busca semântica local") })),
      h(
        "div",
        { class: "table-scroll" },
        h(
          "table",
          { class: "list models" },
          h("thead", {}, h("tr", {}, ...[tr("Modelo"), tr("Uso"), tr("Tamanho"), tr("Licença"), tr("Estado"), ""].map((label) => h("th", { text: label, attrs: { scope: "col" } })))),
          h("tbody", {}, h("tr", {}, h("td", { text: state.model.name, attrs: { title: state.model.name } }), h("td", { text: tr("Busca semântica") }), h("td", { text: state.model.size }), h("td", { text: state.model.license }), h("td", { class: "caption", text: modelState }), h("td", { class: "num" }, modelAction))),
        ),
      ),
      h("div", { class: "btn-row" }, reindex, cancelIndex),
      h("p", { class: "caption", attrs: { role: "status" }, text: state.status }),
      h("div", { class: "btn-row" }, repair),
      repairing ? h("progress", { attrs: { max: state.repair?.total || 1, value: state.repair?.done ?? 0, "aria-label": tr("Índice de busca: reconstruindo…") } }) : null,
      h("p", { class: "caption", text: repairing ? tr("Índice de busca: reconstruindo…") : this.outcome || state.index }),
    );
  }

  private async download(): Promise<void> {
    try {
      const first = await this.bridge.call<Confirmable>("search.downloadModel");
      if (first.status !== "confirm" || !(await confirmDialog({ title: first.title, body: first.message, confirm: tr("Baixar") }))) return;
      await this.bridge.call("search.downloadModel", { confirmed: true });
    } catch (error) {
      this.messages.fail(tr("Não foi possível concluir a ação."), "", { error });
    }
    void this.load();
  }

  private async call(method: string, params: Record<string, unknown>): Promise<void> {
    this.outcome = "";
    try {
      const result = await this.bridge.call<SearchState | { status: string } | { cancelled: boolean }>(method, params);
      if ("enabled" in result) this.state = result;
    } catch (error) {
      this.messages.fail(tr("Não foi possível concluir a ação."), "", { error });
    }
    void this.load();
  }
}
