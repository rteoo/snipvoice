// Configurações › Modelos: download, cancel and remove local models, and their folder.
import type { Bridge } from "../bridge";
import { button, h, replace } from "../dom";
import { tr } from "../i18n";
import type { Confirmable, LocationView, ModelsView, SummaryProgress, VoiceView } from "../types";
import { confirmDialog, type MessageSlot } from "../ui";
import { LeftoversCard } from "./leftovers-card";
import { LocationCard } from "./location";
import { SearchCard } from "./search-card";

interface Finished {
  id: string;
  ok: boolean;
  cancelled?: boolean;
  message: string;
  detail?: string;
}

interface Row {
  label: string;
  description: string;
  tooltip: string;
  size: string;
  license: string;
  installed: boolean;
  inUse: boolean;
  /** Download percent, or null while the total is unknown; undefined when not downloading. */
  progress: number | null | undefined;
  action: HTMLButtonElement | null;
}

const percentOf = (done: number, total: number | null | undefined): number | null => (total ? Math.min(100, Math.floor((100 * done) / total)) : null);

export class ModelsSettings {
  readonly element = h("div", { class: "stack" });
  private view: ModelsView | null = null;
  private voiceProgress: { profile: string | null; done: number; total: number } | null = null;
  private voiceStatus = "";
  private summaryStatus = "";
  private readonly location: LocationCard;
  private readonly leftovers: LeftoversCard;
  private readonly search: SearchCard;
  private token = 0;

  constructor(
    private readonly bridge: Bridge,
    private readonly messages: MessageSlot,
    enginePid: number,
  ) {
    this.location = new LocationCard(bridge, messages, "models", enginePid);
    this.search = new SearchCard(bridge, messages);
    this.leftovers = new LeftoversCard(bridge, messages);
    bridge.on<VoiceView>("voice.state", (view) => {
      const was = this.voiceProgress?.profile ?? null;
      this.voiceProgress = view.download;
      // A finished or newly started download changes what each row offers.
      if ((view.download?.profile ?? null) !== was) void this.load();
      else this.render();
    });
    bridge.on<SummaryProgress>("models.summaryProgress", (progress) => {
      if (this.view) this.view.summaryProgress = progress;
      this.render();
    });
    bridge.on<Finished>("models.summaryFinished", (outcome) => {
      this.summaryStatus = outcome.message;
      if (!outcome.ok && !outcome.cancelled) this.messages.fail(tr("Não foi possível baixar o modelo."), outcome.message, {});
      void this.load();
    });
  }

  async load(location?: LocationView): Promise<void> {
    if (location) this.location.show(location);
    void this.search.load();
    void this.leftovers.load();
    const token = ++this.token;
    try {
      const view = await this.bridge.call<ModelsView>("settings.models");
      if (token !== this.token) return;
      this.view = view;
    } catch (error) {
      this.messages.fail(tr("Não foi possível carregar os modelos"), "", { error });
    }
    this.render();
  }

  private render(): void {
    const view = this.view;
    if (!view) {
      replace(this.element, h("p", { class: "caption", text: tr("Carregando…") }), this.leftovers.element, this.search.element, this.location.element);
      return;
    }
    const voiceBusy = this.voiceProgress?.profile != null;
    const voiceRows = view.voice.map((model) => {
      const active = model.downloading || this.voiceProgress?.profile === model.id;
      const action = active
        ? button(tr("Cancelar"), () => void this.call("settings.cancelVoiceModel", { id: model.id }), "quiet")
        : model.installed
          ? button(tr("Remover"), () => void this.confirmed("settings.deleteVoiceModel", model.id, "voice"), "quiet")
          : button(tr("Baixar"), () => void this.confirmed("settings.downloadVoiceModel", model.id, "voice"));
      if (!active) action.disabled = voiceBusy;
      return this.row({
        label: model.label,
        description: model.description,
        tooltip: `${model.name}\n${model.purpose}`,
        size: model.size,
        license: model.license,
        installed: model.installed,
        inUse: model.inUse,
        progress: active ? percentOf(this.voiceProgress?.done ?? 0, this.voiceProgress?.total) : undefined,
        action,
      });
    });
    const running = view.summaryProgress;
    const summaryRows = view.summary.map((model) => {
      const active = running?.id === model.id;
      const action = active
        ? button(tr("Cancelar"), () => void this.call("settings.cancelSummaryModel", {}), "quiet")
        : model.installed
          ? button(tr("Remover"), () => void this.confirmed("settings.deleteSummaryModel", model.id, "summary"), "quiet")
          : button(tr("Baixar"), () => void this.confirmed("settings.downloadSummaryModel", model.id, "summary"));
      action.disabled = !active && running !== null;
      return this.row({
        label: model.label,
        description: model.description,
        tooltip: model.name,
        size: model.size,
        license: model.license,
        installed: model.installed,
        inUse: model.inUse,
        progress: active ? percentOf(running.done, running.total) : undefined,
        action,
      });
    });
    replace(
      this.element,
      h(
        "section",
        { class: "card stack-sm" },
        h("h2", { text: tr("Modelos de transcrição") }),
        h("p", { class: "caption", text: tr("Baixe somente os modelos que quiser usar. O áudio permanece neste computador.") }),
        this.table(voiceRows),
        h("p", { class: "caption", attrs: { role: "status" }, text: this.voiceStatus }),
      ),
      h(
        "section",
        { class: "card stack-sm" },
        h("h2", { text: tr("Modelos de resumo de texto") }),
        h("p", { class: "caption", text: tr("Baixe e escolha um modelo compacto para resumir sem enviar conteúdo à nuvem.") }),
        this.table(summaryRows),
        h("p", { class: "caption", attrs: { role: "status" }, text: running?.status ?? this.summaryStatus }),
      ),
      this.leftovers.element,
      this.search.element,
      this.location.element,
    );
  }

  private table(rows: HTMLElement[]): HTMLElement {
    const headers = [tr("Modelo"), tr("Tamanho"), tr("Licença"), tr("Estado"), ""];
    return h(
      "div",
      { class: "table-scroll" },
      h(
        "table",
        { class: "list models" },
        h("thead", {}, h("tr", {}, ...headers.map((label, index) => h("th", { class: index === 1 ? "num" : "", text: label, attrs: { scope: "col" } })))),
        h("tbody", {}, ...rows),
      ),
    );
  }

  /** The model cell wraps instead of truncating; hover discloses the exact model identifier. */
  private row(row: Row): HTMLElement {
    const title = h("span", { class: "model-name" }, h("span", { text: row.label }), row.inUse ? h("span", { class: "tag", text: tr("Em uso") }) : null);
    const model = h("td", { class: "model", attrs: { title: row.tooltip } }, title, h("span", { class: "caption", text: row.description }));
    const downloading = row.progress !== undefined;
    const label = row.progress === undefined
      ? row.installed ? tr("Baixado") : tr("Não baixado")
      : row.progress === null ? tr("Baixando…") : tr("Baixando {percent}%", { percent: row.progress });
    const tone = downloading ? "tone-busy" : row.installed ? "tone-ok" : "tone-muted";
    const state = h(
      "td",
      { class: "state" },
      h("span", { class: `status-chip ${tone}`, text: label }),
      downloading ? h("progress", { attrs: row.progress === null ? { max: 100, "aria-label": label } : { max: 100, value: row.progress, "aria-label": label } }) : null,
    );
    return h("tr", {}, model, h("td", { class: "num", text: row.size }), h("td", { text: row.license }), state, h("td", { class: "num" }, row.action));
  }

  private async confirmed(method: string, id: string, kind: "voice" | "summary"): Promise<void> {
    try {
      const first = await this.bridge.call<Confirmable>(method, { id });
      if (first.status === "confirm") {
        const remove = method === "settings.deleteVoiceModel" || method === "settings.deleteSummaryModel";
        if (!(await confirmDialog({ title: first.title, body: first.message, confirm: remove ? tr("Remover") : tr("Baixar"), danger: remove, selectable: false }))) return;
        const done = await this.bridge.call<Confirmable>(method, { id, confirmed: true });
        if (done.status === "removed" && done.message) {
          if (kind === "voice") this.voiceStatus = done.message;
          else this.summaryStatus = done.message;
        }
      }
    } catch (error) {
      this.messages.fail(tr("Não foi possível concluir a ação."), "", { error });
    }
    void this.load();
  }

  private async call(method: string, params: Record<string, string>): Promise<void> {
    try {
      await this.bridge.call(method, params);
    } catch (error) {
      this.messages.fail(tr("Não foi possível concluir a ação."), "", { error });
    }
  }
}
