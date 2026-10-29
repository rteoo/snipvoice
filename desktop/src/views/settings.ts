// Configurações › Geral: preferences that apply immediately.
import type { Bridge } from "../bridge";
import { button, h, replace } from "../dom";
import { tr } from "../i18n";
import { applyAppearance } from "../theme";
import type { Appearance, Hello } from "../types";
import { MessageSlot, pageHeader } from "../ui";

const APPEARANCES: [Appearance, () => string, () => string][] = [
  ["dark", () => tr("Escuro"), () => tr("Tema escuro fixo.")],
  ["light", () => tr("Claro"), () => tr("Tema claro fixo.")],
  ["system", () => tr("Sistema"), () => tr("Segue o tema do sistema.")],
];

export class SettingsPage {
  readonly element: HTMLElement;
  readonly messages = new MessageSlot();
  private readonly appearanceRow = h("div", { class: "segmented", attrs: { role: "radiogroup", "aria-labelledby": "appearance-heading" } });
  private readonly appearanceHint = h("p", { class: "caption" });

  constructor(
    private readonly bridge: Bridge,
    private readonly hello: Hello,
    private readonly onLanguage: (language: string, catalog: Record<string, string>) => void,
  ) {
    const language = h("select", { attrs: { id: "language" } });
    for (const [code, name] of Object.entries(hello.languages)) {
      const option = h("option", { text: name, attrs: { value: code } });
      option.selected = code === hello.language;
      language.append(option);
    }
    language.addEventListener("change", () => void this.setLanguage(language));
    this.element = h(
      "section",
      { class: "page" },
      pageHeader(tr("Configurações")),
      this.messages.element,
      h(
        "div",
        { class: "page-body stack settings" },
        h(
          "div",
          { class: "card stack-sm" },
          h("h2", { text: tr("Idioma") }),
          h("label", { class: "field", attrs: { for: "language" } }, h("span", { text: tr("Idioma da interface") }), language),
        ),
        h("div", { class: "card stack-sm" }, h("h2", { text: tr("Aparência"), attrs: { id: "appearance-heading" } }), this.appearanceRow, this.appearanceHint),
      ),
    );
    this.renderAppearance();
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
