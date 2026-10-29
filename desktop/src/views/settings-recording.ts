// Configurações › Gravação: defaults for the next recording, saved together.
import { allowEngineForeground, type Bridge } from "../bridge";
import { button, h, replace } from "../dom";
import { tr } from "../i18n";
import type { RecordingSettingsView, RecordingValues } from "../types";
import type { MessageSlot } from "../ui";

/** The bar every draft form ends with: what state the draft is in, Discard, Save. */
export function saveBar(state: { dirty: boolean; saving: boolean; receipt: string }, onDiscard: () => void, onSave: () => void): HTMLElement {
  const save = button(state.saving ? tr("Salvando…") : tr("Salvar"), onSave, "primary");
  save.disabled = !state.dirty || state.saving;
  const discard = button(tr("Descartar"), onDiscard, "quiet");
  discard.disabled = !state.dirty || state.saving;
  const status = state.saving ? tr("Salvando…") : state.dirty ? tr("Alterações não salvas") : state.receipt;
  return h("div", { class: "save-bar" }, h("p", { class: "caption", text: status, attrs: { role: "status" } }), discard, save);
}

export class RecordingSettings {
  readonly element = h("div", { class: "stack" });
  private view: RecordingSettingsView | null = null;
  private draft: RecordingValues | null = null;
  private destinationLabel = "";
  private saving = false;
  private receipt = "";

  constructor(
    private readonly bridge: Bridge,
    private readonly messages: MessageSlot,
    private readonly enginePid: number,
  ) {}

  get dirty(): boolean {
    return this.view !== null && this.draft !== null && JSON.stringify(this.draft) !== JSON.stringify(this.view.values);
  }

  async load(): Promise<void> {
    if (this.dirty) return this.render();
    try {
      this.accept(await this.bridge.call<RecordingSettingsView>("settings.recording"));
    } catch (error) {
      this.messages.fail(tr("Não foi possível carregar as configurações"), "", { error });
    }
    this.render();
  }

  private accept(view: RecordingSettingsView): void {
    this.view = view;
    this.draft = { ...view.values };
    this.destinationLabel = view.destinationLabel;
  }

  private render(): void {
    const view = this.view;
    const draft = this.draft;
    if (!view || !draft) {
      replace(this.element, h("p", { class: "caption", text: tr("Carregando…") }));
      return;
    }
    // A redraw keeps keyboard focus on the control that changed.
    const focused = this.element.contains(document.activeElement) ? document.activeElement?.id : "";
    const select =(id: string, choices: { id: string; label: string }[], value: string, onChange: (value: string) => void) => {
      const node = h("select", { attrs: { id } });
      for (const choice of choices) node.append(h("option", { text: choice.label, attrs: { value: choice.id } }));
      node.value = value;
      node.addEventListener("change", () => onChange(node.value));
      return node;
    };
    const languages = view.languages[draft.profile] ?? [];
    const profile = select("recording-profile", view.profiles, draft.profile, (value) => {
      draft.profile = value;
      const allowed = view.languages[value] ?? [];
      if (!allowed.some((choice) => choice.id === draft.language)) draft.language = allowed[0]?.id ?? "auto";
      this.render();
    });
    const language = select("recording-language", languages, draft.language, (value) => {
      draft.language = value;
      this.render();
    });
    const summaries = view.summaryModels.map((model) => ({ id: model.id, label: model.installed ? model.label : `${model.label} · ${tr("Não baixado")}` }));
    const summary = select("recording-summary", summaries, draft.summaryModel, (value) => {
      draft.summaryModel = value;
      this.render();
    });
    const hotkey = h("input", { attrs: { id: "recording-hotkey", type: "text", maxlength: 80, autocomplete: "off", spellcheck: "false" } });
    hotkey.placeholder = "ctrl+alt+r";
    hotkey.value = draft.hotkey;
    hotkey.addEventListener("input", () => {
      draft.hotkey = hotkey.value;
      this.renderBar();
    });
    const destination = h("input", { attrs: { type: "text", readonly: "", "aria-label": tr("Pasta dos arquivos finais") } });
    destination.value = this.destinationLabel;
    const boost = h("input", { attrs: { type: "checkbox", id: "recording-boost" } });
    boost.checked = draft.voiceBoost;
    boost.disabled = !view.microphoneEnabled;
    boost.addEventListener("change", () => {
      draft.voiceBoost = boost.checked;
      this.renderBar();
    });
    replace(
      this.element,
      h(
        "section",
        { class: "card stack-sm" },
        h("h2", { text: tr("Padrões de gravação") }),
        h("p", { class: "caption", text: tr("Escolha os modelos, o idioma e o atalho usados em novas gravações.") }),
        h("label", { class: "field" }, h("span", { text: tr("Modelo de transcrição local") }), profile),
        h("label", { class: "field" }, h("span", { text: tr("Idioma") }), language),
        h("label", { class: "field" }, h("span", { text: tr("Atalho de gravação (opcional)") }), hotkey),
        h("label", { class: "field" }, h("span", { text: tr("Modelo de resumo local") }), summary),
      ),
      h(
        "section",
        { class: "card stack-sm" },
        h("h2", { text: tr("Detalhes e automação") }),
        h(
          "div",
          { class: "field" },
          h("span", { text: tr("Pasta dos arquivos finais") }),
          h("div", { class: "path-row" }, destination, button(tr("Escolher…"), () => void this.chooseDestination()), button(tr("Usar padrão"), () => this.useDefaultDestination(), "quiet")),
        ),
        h("p", { class: "caption", text: tr("Transcrição e resumo são gerados automaticamente com os modelos instalados.") }),
        h("label", { class: "check" }, boost, h("span", { text: tr("Ajustar volume do microfone no áudio final") })),
        h("p", { class: "caption", text: tr("Configurações valem para a próxima gravação. A pasta local padrão fica ao lado da biblioteca.\nO sistema inclui todos os sons do dispositivo escolhido. Use fones para reduzir duplicação acústica.") }),
      ),
      h("div", { class: "save-slot" }),
    );
    if (focused) this.element.querySelector<HTMLElement>(`#${focused}`)?.focus();
    this.renderBar();
  }

  private renderBar(): void {
    const slot = this.element.querySelector(".save-slot");
    if (slot) replace(slot as HTMLElement, saveBar({ dirty: this.dirty, saving: this.saving, receipt: this.receipt }, () => this.discard(), () => void this.save()));
  }

  private async chooseDestination(): Promise<void> {
    if (!this.draft) return;
    try {
      await allowEngineForeground(this.enginePid);
      const chosen = await this.bridge.call<{ path: string; label: string }>("settings.chooseDestination", { initial: this.draft.destination });
      if (!chosen.path || !this.draft) return;
      this.draft.destination = chosen.path;
      this.destinationLabel = chosen.label;
      this.render();
    } catch (error) {
      this.messages.fail(tr("Pasta padrão das gravações"), "", { error });
    }
  }

  private useDefaultDestination(): void {
    if (!this.draft) return;
    this.draft.destination = "";
    this.destinationLabel = tr("Pasta local padrão (recordings)");
    this.render();
  }

  private discard(): void {
    if (!this.view) return;
    this.accept(this.view);
    this.render();
  }

  private async save(): Promise<void> {
    if (!this.draft || this.saving) return;
    this.saving = true;
    this.renderBar();
    try {
      this.accept(await this.bridge.call<RecordingSettingsView>("settings.saveRecording", { values: this.draft }));
      this.receipt = tr("Configurações salvas para a próxima gravação.");
      this.messages.clear();
    } catch (error) {
      this.messages.fail(tr("Não foi possível salvar as configurações"), "", { error });
    }
    this.saving = false;
    this.render();
  }
}
