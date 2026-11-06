// Arquivos antigos: files of models and features SnipVoice no longer ships, with Remover.
import type { Bridge } from "../bridge";
import { button, h, replace } from "../dom";
import { tr } from "../i18n";
import type { Confirmable, LeftoversView } from "../types";
import { confirmDialog, type MessageSlot } from "../ui";

export class LeftoversCard {
  readonly element = h("section", { class: "card stack-sm" });
  private view: LeftoversView | null = null;
  private status = "";

  constructor(
    private readonly bridge: Bridge,
    private readonly messages: MessageSlot,
  ) {}

  async load(): Promise<void> {
    try {
      this.view = await this.bridge.call<LeftoversView>("settings.leftovers");
    } catch (error) {
      this.view = null;
      this.messages.fail(tr("Não foi possível procurar arquivos antigos."), "", { error });
    }
    this.render();
  }

  private render(): void {
    const items = this.view?.items ?? [];
    // Nothing left behind: the card stays out of the way until there is.
    this.element.hidden = items.length === 0 && !this.status;
    const rows = items.map((item) =>
      h(
        "tr",
        {},
        h("td", { class: "model" }, h("span", { class: "model-name", text: item.name }), h("span", { class: "caption", text: item.detail })),
        h("td", { class: "num", text: item.size }),
        h("td", { class: "num" }, button(tr("Remover"), () => void this.remove(item.id), "quiet")),
      ),
    );
    replace(
      this.element,
      h("h2", { text: tr("Arquivos antigos") }),
      h("p", { class: "caption", text: tr("O SnipVoice não usa mais estes arquivos. Remova-os para liberar espaço.") }),
      rows.length
        ? h(
            "div",
            { class: "table-scroll" },
            h(
              "table",
              { class: "list models leftovers" },
              h("thead", {}, h("tr", {}, h("th", { text: tr("Arquivo"), attrs: { scope: "col" } }), h("th", { class: "num", text: tr("Tamanho"), attrs: { scope: "col" } }), h("th", { attrs: { scope: "col" } }))),
              h("tbody", {}, ...rows),
            ),
          )
        : null,
      h("p", { class: "caption", attrs: { role: "status" }, text: this.status }),
    );
  }

  private async remove(id: string): Promise<void> {
    this.status = "";
    try {
      const first = await this.bridge.call<Confirmable>("settings.removeLeftover", { id });
      if (first.status !== "confirm") return;
      if (!(await confirmDialog({ title: first.title, body: first.message, confirm: tr("Remover"), danger: true, selectable: false }))) return;
      const done = await this.bridge.call<Confirmable>("settings.removeLeftover", { id, confirmed: true });
      if (done.status === "removed" && done.message) this.status = done.message;
    } catch (error) {
      this.messages.fail(tr("Não foi possível remover os arquivos."), "", { error });
    }
    void this.load();
  }
}
