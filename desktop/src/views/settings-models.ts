// Configurações › Modelos: download, cancel and remove local models, and their folder.
import type { Bridge } from "../bridge";
import { button, h, replace } from "../dom";
import { tr } from "../i18n";
import type { Confirmable, LocationView, ModelsView, SummaryProgress, VoiceView } from "../types";
import { confirmDialog, type MessageSlot } from "../ui";
import { LocationCard } from "./location";
import { SearchCard } from "./search-card";

interface Finished {
  id: string;
  ok: boolean;
  cancelled?: boolean;
  message: string;
  detail?: string;
}

export class ModelsSettings {
  readonly element = h("div", { class: "stack" });
  private view: ModelsView | null = null;
  private voiceProgress: { profile: string | null; done: number; total: number } | null = null;
  private summaryStatus = "";
  private readonly location: LocationCard;
  private readonly search: SearchCard;
  private token = 0;

  constructor(
    private readonly bridge: Bridge,
    private readonly messages: MessageSlot,
    enginePid: number,
  ) {
    this.location = new LocationCard(bridge, messages, "models", enginePid);
    this.search = new SearchCard(bridge, messages);
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
      replace(this.element, h("p", { class: "caption", text: tr("Carregando…") }), this.search.element, this.location.element);
      return;
    }
    const voiceRows = view.voice.map((model) => {
      const active = model.downloading || this.voiceProgress?.profile === model.id;
      const progress = active && this.voiceProgress?.total ? Math.min(100, Math.floor((100 * this.voiceProgress.done) / this.voiceProgress.total)) : null;
      const state = model.installed ? tr("Baixado") : active ? (progress === null ? tr("Baixando…") : tr("Baixando {percent}%", { percent: progress })) : tr("Não baixado");
      // Removing a transcription model belongs to Ditado: it turns dictation off.
      const action = active
        ? button(tr("Cancelar"), () => void this.call("settings.cancelVoiceModel", { id: model.id }), "quiet")
        : model.installed
          ? null
          : button(tr("Baixar"), () => void this.confirmed("settings.downloadVoiceModel", model.id));
      if (action && !active) action.disabled = this.voiceProgress?.profile != null;
      return this.row(model.label, model.name, model.purpose, model.size, model.license, state, action);
    });
    const running = view.summaryProgress;
    const summaryRows = view.summary.map((model) => {
      const active = running?.id === model.id;
      const percent = active && running.total ? Math.min(100, Math.floor((100 * running.done) / running.total)) : null;
      const state = model.installed ? tr("Baixado") : active ? (percent === null ? tr("Baixando…") : tr("Baixando {percent}%", { percent })) : tr("Não baixado");
      const action = active
        ? button(tr("Cancelar"), () => void this.call("settings.cancelSummaryModel", {}), "quiet")
        : model.installed
          ? button(tr("Remover"), () => void this.confirmed("settings.deleteSummaryModel", model.id), "quiet")
          : button(tr("Baixar"), () => void this.confirmed("settings.downloadSummaryModel", model.id));
      action.disabled = !active && running !== null;
      return this.row(model.label, model.description, model.label, model.size, model.license, state, action);
    });
    replace(
      this.element,
      h(
        "section",
        { class: "card stack-sm" },
        h("h2", { text: tr("Modelos de transcrição") }),
        h("p", { class: "caption", text: tr("Baixe somente os modelos que quiser usar. O áudio permanece neste computador.") }),
        this.table(voiceRows),
      ),
      h(
        "section",
        { class: "card stack-sm" },
        h("h2", { text: tr("Modelos de resumo de texto") }),
        h("p", { class: "caption", text: tr("Baixe e escolha um modelo compacto para resumir sem enviar conteúdo à nuvem.") }),
        this.table(summaryRows),
        running ? h("progress", { attrs: { max: running.total || 1, value: running.done, "aria-label": running.status } }) : null,
        h("p", { class: "caption", attrs: { role: "status" }, text: running?.status ?? this.summaryStatus }),
      ),
      this.search.element,
      this.location.element,
    );
  }

  private table(rows: HTMLElement[]): HTMLElement {
    return h(
      "div",
      { class: "table-scroll" },
      h(
        "table",
        { class: "list models" },
        h("thead", {}, h("tr", {}, ...[tr("Modelo"), tr("Tamanho"), tr("Licença"), tr("Estado"), ""].map((label) => h("th", { text: label, attrs: { scope: "col" } })))),
        h("tbody", {}, ...rows),
      ),
    );
  }

  /** The model cell wraps instead of truncating: a name cut short cannot be told apart. */
  private row(label: string, detail: string, tooltip: string, size: string, license: string, state: string, action: HTMLElement | null): HTMLElement {
    const model = h("td", { class: "model", attrs: { title: tooltip } }, h("span", { class: "model-name", text: label }), h("span", { class: "caption", text: detail }));
    return h("tr", {}, model, h("td", { text: size }), h("td", { text: license }), h("td", { class: "caption", text: state }), h("td", { class: "num" }, action));
  }

  private async confirmed(method: string, id: string): Promise<void> {
    try {
      const first = await this.bridge.call<Confirmable>(method, { id });
      if (first.status === "confirm") {
        const remove = method === "settings.deleteSummaryModel";
        if (!(await confirmDialog({ title: first.title, body: first.message, confirm: remove ? tr("Remover") : tr("Baixar"), danger: remove, selectable: false }))) return;
        const done = await this.bridge.call<Confirmable>(method, { id, confirmed: true });
        if (done.status === "removed" && done.message) this.summaryStatus = done.message;
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
