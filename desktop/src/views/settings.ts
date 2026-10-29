// Configurações: Geral applies immediately; Gravação and Privacidade are saved
// drafts that survive switching sections; Modelos downloads and removes models.
import type { Bridge } from "../bridge";
import { button, h, replace } from "../dom";
import { tr } from "../i18n";
import { applyAppearance } from "../theme";
import type { Appearance, Hello, LocationView } from "../types";
import { MessageSlot, pageHeader } from "../ui";
import { LocationCard } from "./location";
import { ModelsSettings } from "./settings-models";
import { PrivacySettings } from "./settings-privacy";
import { RecordingSettings } from "./settings-recording";

const APPEARANCES: [Appearance, () => string, () => string][] = [
  ["dark", () => tr("Escuro"), () => tr("Tema escuro fixo.")],
  ["light", () => tr("Claro"), () => tr("Tema claro fixo.")],
  ["system", () => tr("Sistema"), () => tr("Segue o tema do sistema.")],
];

type Section = "general" | "recording" | "privacy" | "models";
const SECTIONS: Section[] = ["general", "recording", "privacy", "models"];

export class SettingsPage {
  readonly element: HTMLElement;
  readonly messages = new MessageSlot();
  private section: Section = "general";
  private readonly appearanceRow = h("div", { class: "segmented", attrs: { role: "radiogroup", "aria-labelledby": "appearance-heading" } });
  private readonly appearanceHint = h("p", { class: "caption" });
  private readonly tabs = h("div", { class: "tabs settings-tabs", attrs: { role: "tablist", "aria-label": tr("Seções das configurações") } });
  private readonly panel = h("div", { class: "page-body settings", attrs: { role: "tabpanel", id: "settings-panel" } });
  private readonly general: HTMLElement;
  private readonly dataLocation: LocationCard;
  private readonly recording: RecordingSettings;
  private readonly privacy: PrivacySettings;
  private readonly models: ModelsSettings;

  constructor(
    private readonly bridge: Bridge,
    private readonly hello: Hello,
    private readonly onLanguage: (language: string, catalog: Record<string, string>) => void,
  ) {
    this.dataLocation = new LocationCard(bridge, this.messages, "data", hello.enginePid);
    this.recording = new RecordingSettings(bridge, this.messages, hello.enginePid);
    this.privacy = new PrivacySettings(bridge, this.messages);
    this.models = new ModelsSettings(bridge, this.messages, hello.enginePid);
    const language = h("select", { attrs: { id: "language" } });
    for (const [code, name] of Object.entries(hello.languages)) {
      const option = h("option", { text: name, attrs: { value: code } });
      option.selected = code === hello.language;
      language.append(option);
    }
    language.addEventListener("change", () => void this.setLanguage(language));
    this.general = h(
      "div",
      { class: "stack" },
      h(
        "div",
        { class: "card stack-sm" },
        h("h2", { text: tr("Idioma") }),
        h("label", { class: "field", attrs: { for: "language" } }, h("span", { text: tr("Idioma da interface") }), language),
      ),
      h("div", { class: "card stack-sm" }, h("h2", { text: tr("Aparência"), attrs: { id: "appearance-heading" } }), this.appearanceRow, this.appearanceHint),
      this.dataLocation.element,
    );
    const labels: Record<Section, string> = { general: tr("Geral"), recording: tr("Gravação"), privacy: tr("Privacidade"), models: tr("Modelos") };
    for (const section of SECTIONS) {
      const tab = h("button", {
        class: "tab",
        text: labels[section],
        attrs: { type: "button", role: "tab", id: `settings-tab-${section}`, "aria-controls": "settings-panel" },
      });
      tab.addEventListener("click", () => this.show(section));
      tab.addEventListener("keydown", (event) => {
        if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
        event.preventDefault();
        const step = event.key === "ArrowRight" ? 1 : SECTIONS.length - 1;
        const next = SECTIONS[(SECTIONS.indexOf(section) + step) % SECTIONS.length] ?? "general";
        this.show(next);
        this.tabs.querySelector<HTMLElement>(`#settings-tab-${next}`)?.focus();
      });
      this.tabs.append(tab);
    }
    this.element = h("section", { class: "page" }, pageHeader(tr("Configurações")), h("div", { class: "settings-head" }, this.tabs), this.messages.element, this.panel);
    this.renderAppearance();
    this.show("general");
  }

  /** Opens one section; drafts keep their unsaved values while another is shown. */
  show(section: Section): void {
    this.section = section;
    this.tabs.querySelectorAll<HTMLElement>("[role=tab]").forEach((tab) => {
      const current = tab.id === `settings-tab-${section}`;
      tab.setAttribute("aria-selected", String(current));
      tab.tabIndex = current ? 0 : -1;
    });
    this.panel.setAttribute("aria-labelledby", `settings-tab-${section}`);
    this.messages.clear();
    if (section === "general") {
      replace(this.panel, this.general);
      void this.loadLocations();
    } else if (section === "recording") {
      replace(this.panel, this.recording.element);
      void this.recording.load();
    } else if (section === "privacy") {
      replace(this.panel, this.privacy.element);
      void this.privacy.load();
    } else {
      replace(this.panel, this.models.element);
      void this.loadLocations();
    }
  }

  private async loadLocations(): Promise<void> {
    try {
      const view = await this.bridge.call<Record<"data" | "models", LocationView>>("settings.locations");
      if (this.section === "general") this.dataLocation.show(view.data);
      else if (this.section === "models") await this.models.load(view.models);
    } catch (error) {
      this.messages.fail(tr("Não foi possível carregar as pastas"), "", { error });
      if (this.section === "models") await this.models.load();
    }
  }

  private renderAppearance(): void {
    const current = this.hello.appearance;
    replace(
      this.appearanceRow,
      ...APPEARANCES.map(([value, label]) => {
        const node = button(label(), () => void this.setAppearance(value));
        node.setAttribute("role", "radio");
        node.setAttribute("aria-checked", String(value === current));
        node.setAttribute("aria-pressed", String(value === current));
        return node;
      }),
    );
    this.appearanceHint.textContent = APPEARANCES.find(([value]) => value === current)?.[2]() ?? "";
  }

  private async setAppearance(value: Appearance): Promise<void> {
    const previous = this.hello.appearance;
    if (value === previous) return;
    this.hello.appearance = value;
    applyAppearance(value);
    this.renderAppearance();
    try {
      await this.bridge.call("app.setAppearance", { appearance: value });
    } catch (error) {
      // An immediate preference rolls back with its reason.
      this.hello.appearance = previous;
      applyAppearance(previous);
      this.renderAppearance();
      this.messages.fail(tr("Não foi possível salvar a aparência. Tente de novo."), "", { error });
    }
  }

  private async setLanguage(select: HTMLSelectElement): Promise<void> {
    if (this.recording.dirty || this.privacy.dirty) {
      select.value = this.hello.language;
      this.messages.fail(tr("Alterações não salvas"), tr("Salve ou descarte Gravação e Privacidade antes de trocar o idioma; a janela é reconstruída."), { tone: "warning" });
      return;
    }
    select.disabled = true;
    try {
      const result = await this.bridge.call<{ language: string; catalog: Record<string, string> }>("app.setLanguage", { language: select.value });
      this.onLanguage(result.language, result.catalog);
    } catch (error) {
      select.value = this.hello.language;
      select.disabled = false;
      this.messages.fail(tr("Não foi possível salvar o idioma"), "", { error });
    }
  }
}
