// Ditado: the controller's actual readiness, the last five dictations, and its settings.
import type { Bridge } from "../bridge";
import { button, h, replace } from "../dom";
import { tr } from "../i18n";
import type { HistoryRow, VoiceToggleResult, VoiceView } from "../types";
import { confirmDialog, MessageSlot, pageHeader, toneClass, toneText } from "../ui";
import { DictationSettings } from "./dictation-settings";

export class VoicePage {
  readonly element: HTMLElement;
  readonly messages = new MessageSlot();
  private view: VoiceView | null = null;
  private rows: HistoryRow[] = [];
  private selected: string | null = null;
  private historyKey = "";
  private readonly toggle = h("input", { attrs: { type: "checkbox", id: "voice-enabled", role: "switch" } });
  private readonly status = h("p", { class: "status-chip" });
  private readonly detail = h("p", { class: "caption" });
  private readonly download = h("progress");
  private readonly attention = h("p", { class: "caption" });
  private readonly table = h("tbody");
  private readonly actions = h("div", { class: "btn-row" });
  private readonly settings: DictationSettings;

  onChange: () => void = () => {};

  constructor(private readonly bridge: Bridge) {
    this.settings = new DictationSettings(bridge, this.messages);
    this.download.hidden = true;
    this.toggle.addEventListener("change", () => void this.setEnabled(this.toggle.checked, false));
    this.element = h(
      "section",
      { class: "page" },
      pageHeader(tr("Ditado"), h("label", { class: "switch", attrs: { for: "voice-enabled" } }, h("span", { text: tr("Ativar entrada por voz") }), this.toggle)),
      this.messages.element,
      h(
        "div",
        { class: "page-body stack" },
        h("div", { class: "card stack-sm" }, this.status, this.detail, this.download),
        h(
          "div",
          { class: "card stack-sm" },
          h("h2", { text: tr("Últimos ditados") }),
          this.attention,
          h(
            "div",
            { class: "table-scroll" },
            h(
              "table",
              { class: "list history" },
              h(
                "thead",
                {},
                h("tr", {}, h("th", { text: tr("Data"), attrs: { scope: "col" } }), h("th", { text: tr("Estado"), attrs: { scope: "col" } }), h("th", { text: tr("Transcrição"), attrs: { scope: "col" } })),
              ),
              this.table,
            ),
          ),
          this.actions,
        ),
        this.settings.element,
      ),
    );
    bridge.on<VoiceView>("voice.state", (view) => this.render(view));
    void bridge.call<VoiceView>("voice.state").then((view) => this.render(view));
    void this.settings.load();
  }

  get navState(): string {
    if (!this.view) return "";
    return this.view.enabled ? tr("Ativo") : tr("Desligado");
  }

  private render(view: VoiceView): void {
    this.view = view;
    this.toggle.checked = view.enabled;
    this.status.className = `status-chip ${toneClass(view.tone)}`;
    this.status.textContent = view.status;
    this.detail.textContent = view.detail;
    this.download.hidden = !view.download;
    if (view.download) {
      this.download.max = view.download.total || 1;
      this.download.value = view.download.done;
    }
    const key = `${view.state}|${view.enabled}`;
    if (key !== this.historyKey) {
      this.historyKey = key;
      void this.refreshHistory();
    }
    this.onChange();
  }

  private async setEnabled(enabled: boolean, confirmed: boolean): Promise<void> {
    this.toggle.disabled = true;
    try {
      const result = await this.bridge.call<VoiceToggleResult>("voice.setEnabled", { enabled, confirmed });
      if (result.status === "confirm") {
        const accepted = await confirmDialog({ title: result.title, body: result.message, confirm: tr("Baixar e ativar") });
        if (accepted) return void (await this.setEnabled(true, true));
      }
    } catch (error) {
      this.messages.fail(tr("Não foi possível ativar o ditado. Tente novamente."), "", { error });
    } finally {
      this.toggle.disabled = false;
      // The engine pushes the real state; until then the switch shows the last known one.
      this.toggle.checked = this.view?.enabled ?? false;
    }
  }

  private async refreshHistory(): Promise<void> {
    try {
      const { rows } = await this.bridge.call<{ rows: HistoryRow[] }>("voice.history", { limit: 5 });
      this.rows = rows;
      const attention = rows.filter((row) => row.tone === "danger" || row.tone === "warning").length;
      this.attention.textContent =
        attention === 0
          ? tr("Ditados recentes sem pendências.")
          : attention === 1
            ? tr("1 ditado precisa de atenção. Selecione um item para ver as ações disponíveis.")
            : tr("{count} ditados precisam de atenção. Selecione um item para ver as ações disponíveis.", { count: attention });
    } catch (error) {
      this.rows = [];
      this.attention.textContent = "";
      this.messages.fail(tr("Não foi possível ler o histórico de ditados"), "", { error });
    }
    this.renderRows();
  }

  private renderRows(): void {
    if (!this.rows.some((row) => row.id === this.selected)) this.selected = null;
    replace(
      this.table,
      ...(this.rows.length
        ? this.rows.map((row) => {
            const tr_ = h(
              "tr",
              { attrs: { tabindex: 0, "aria-selected": row.id === this.selected ? "true" : "false" } },
              h("td", { text: row.date }),
              h("td", { class: toneText(row.tone), text: row.status }),
              h("td", { class: "selectable", text: row.preview }),
            );
            tr_.addEventListener("click", () => this.select(row.id));
            tr_.addEventListener("keydown", (event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                this.select(row.id);
              }
              if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                event.preventDefault();
                const sibling = event.key === "ArrowDown" ? tr_.nextElementSibling : tr_.previousElementSibling;
                const index = sibling ? [...this.table.children].indexOf(sibling) : -1;
                const next = this.rows[index];
                if (sibling instanceof HTMLElement && next) {
                  sibling.focus();
                  this.select(next.id);
                }
              }
            });
            return tr_;
          })
        : [h("tr", {}, h("td", { class: "empty", text: tr("Nenhum ditado ainda."), attrs: { colspan: 3 } }))]),
    );
    this.renderActions();
  }

  // Rows stay in place so keyboard focus survives a selection change.
  private select(id: string): void {
    this.selected = id;
    [...this.table.children].forEach((node, index) =>
      node.setAttribute("aria-selected", String(this.rows[index]?.id === id)));
    this.renderActions();
  }

  private renderActions(): void {
    const row = this.rows.find((item) => item.id === this.selected);
    const retry = button(tr("Tentar novamente"), () => void this.rowAction("voice.retry", row));
    retry.disabled = !row?.retryable;
    const copy = button(tr("Copiar transcrição"), () => void this.rowAction("voice.copy", row));
    copy.disabled = !row?.copyable;
    replace(this.actions, retry, copy, button(tr("Atualizar"), () => void this.refreshHistory(), "quiet"));
  }

  private async rowAction(method: "voice.retry" | "voice.copy", row: HistoryRow | undefined): Promise<void> {
    if (!row) return;
    try {
      const { accepted } = await this.bridge.call<{ accepted: boolean }>(method, { id: row.id });
      if (method === "voice.copy") {
        if (accepted) this.messages.say(tr("Transcrição copiada."));
        else this.messages.fail(tr("Não foi possível copiar a transcrição"), "", { tone: "warning" });
      } else if (!accepted) {
        this.messages.fail(tr("Não foi possível tentar novamente"), tr("Aguarde o ditado atual terminar e tente de novo."), { tone: "warning" });
      }
    } catch (error) {
      this.messages.fail(tr("Não foi possível concluir a ação."), "", { error });
    }
    void this.refreshHistory();
  }
}
