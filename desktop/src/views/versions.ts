// Versões salvas: every summary version and saved answer of one recording;
// a section can be reviewed (stored beside the generated text) or the report exported.
import { allowEngineForeground, type Bridge } from "../bridge";
import { button, h, replace } from "../dom";
import { tr } from "../i18n";
import type { SaveResult, VersionItem, VersionView } from "../types";
import { copyText, type MessageSlot } from "../ui";
import { summaryBody } from "./summary";

export class VersionsPanel {
  readonly element = h("details", { class: "card stack-sm versions" });
  private sessionId: string | null = null;
  private items: VersionItem[] | null = null;
  private selected: VersionView | null = null;
  private section = "";
  private review = "";
  private busy = false;
  private token = 0;

  constructor(
    private readonly bridge: Bridge,
    private readonly messages: MessageSlot,
    private readonly enginePid: number,
  ) {
    // The list loads when the panel opens; most visits only read the latest summary.
    this.element.addEventListener("toggle", () => {
      if ((this.element as HTMLDetailsElement).open && this.items === null) void this.load();
    });
  }

  show(sessionId: string): void {
    if (sessionId !== this.sessionId) {
      this.sessionId = sessionId;
      this.items = null;
      this.selected = null;
      this.token++;
      if ((this.element as HTMLDetailsElement).open) void this.load();
    }
    this.render();
  }

  /** A new version was saved elsewhere (generate, save answer): reload when open. */
  invalidate(): void {
    this.items = null;
    this.selected = null;
    if ((this.element as HTMLDetailsElement).open) void this.load();
    else this.render();
  }

  private async load(): Promise<void> {
    const sessionId = this.sessionId;
    if (!sessionId) return;
    const token = ++this.token;
    this.render();
    try {
      const { items } = await this.bridge.call<{ items: VersionItem[] }>("library.versions", { id: sessionId });
      if (token !== this.token) return;
      this.items = items;
      const first = items.find((item) => !item.answer) ?? items[0];
      if (first) await this.open(first.id);
    } catch (error) {
      if (token !== this.token) return;
      this.items = [];
      this.messages.fail(tr("Não foi possível carregar as versões do resumo"), "", { error });
    }
    this.render();
  }

  private async open(reportId: string): Promise<void> {
    const sessionId = this.sessionId;
    if (!sessionId) return;
    const token = this.token;
    try {
      const view = await this.bridge.call<VersionView>("library.version", { id: sessionId, reportId });
      if (token !== this.token) return;
      this.selected = view;
      this.section = view.sections[0]?.name ?? "";
      this.review = view.sections[0]?.text ?? "";
    } catch (error) {
      this.messages.fail(tr("Não foi possível abrir este relatório"), "", { error });
    }
    this.render();
  }

  private render(): void {
    const header = h("summary", { text: tr("Versões salvas") });
    if (!(this.element as HTMLDetailsElement).open) {
      replace(this.element, header);
      return;
    }
    if (this.items === null) {
      replace(this.element, header, h("p", { class: "caption", text: tr("Carregando…") }));
      return;
    }
    if (!this.items.length) {
      replace(this.element, header, h("p", { class: "caption", text: tr("Nenhum relatório selecionado.") }));
      return;
    }
    const versions = h("select", { attrs: { id: "version-select", "aria-label": tr("Histórico") } });
    for (const item of this.items) versions.append(h("option", { text: item.reviewed ? `${item.label} · ${tr("revisada")}` : item.label, attrs: { value: item.id } }));
    versions.value = this.selected?.id ?? "";
    versions.addEventListener("change", () => void this.open(versions.value));
    const view = this.selected;
    const nodes: (HTMLElement | null)[] = [header, h("label", { class: "field" }, h("span", { text: tr("Histórico") }), versions)];
    if (view) {
      const sections = h("select", { attrs: { id: "version-section", "aria-label": tr("Seção") } });
      for (const section of view.sections) sections.append(h("option", { text: section.name, attrs: { value: section.name } }));
      sections.value = this.section;
      sections.addEventListener("change", () => {
        this.section = sections.value;
        this.review = view.sections.find((section) => section.name === this.section)?.text ?? "";
        this.render();
      });
      const editor = h("textarea", { attrs: { id: "version-review", rows: 8, "aria-label": tr("Seção") } });
      editor.value = this.review;
      editor.readOnly = !view.reviewable;
      editor.addEventListener("input", () => {
        this.review = editor.value;
        save.disabled = this.busy || !this.changed();
      });
      const save = button(tr("Salvar revisão"), () => void this.saveReview());
      save.disabled = this.busy || !view.reviewable || !this.changed();
      nodes.push(
        summaryBody(view.text),
        view.sections.length ? h("label", { class: "field" }, h("span", { text: tr("Seção") }), sections) : null,
        view.sections.length ? editor : null,
        view.reviewable ? null : h("p", { class: "caption", text: tr("O resumo legado não aceita revisão por seção; gere um relatório novo.") }),
        h(
          "div",
          { class: "btn-row" },
          view.reviewable ? save : null,
          button(tr("Copiar seção"), () => void this.copySection(), "quiet"),
          button(tr("Exportar relatório…"), () => void this.exportReport(), "quiet"),
        ),
      );
    }
    replace(this.element, ...nodes);
  }

  private changed(): boolean {
    const original = this.selected?.sections.find((section) => section.name === this.section)?.text ?? "";
    return this.review !== original;
  }

  private async saveReview(): Promise<void> {
    const view = this.selected;
    const sessionId = this.sessionId;
    if (!view || !sessionId || this.busy) return;
    this.busy = true;
    this.render();
    try {
      this.selected = await this.bridge.call<VersionView>("library.reviewSection", { id: sessionId, reportId: view.id, section: this.section, text: this.review, generation: view.generation });
      this.messages.say(tr("Revisão salva separadamente do relatório gerado."));
    } catch (error) {
      this.messages.fail(tr("A revisão não foi salva"), "", { error });
    }
    this.busy = false;
    this.render();
  }

  private async copySection(): Promise<void> {
    const text = this.selected?.sections.find((section) => section.name === this.section)?.text ?? this.selected?.text ?? "";
    if (await copyText(text)) this.messages.say(tr("Seção copiada sem alterar o relatório gerado."));
    else this.messages.fail(tr("Não foi possível copiar a seção"), tr("Selecione o texto e pressione Ctrl+C"), { tone: "warning" });
  }

  private async exportReport(): Promise<void> {
    const view = this.selected;
    const sessionId = this.sessionId;
    if (!view || !sessionId) return;
    try {
      await allowEngineForeground(this.enginePid);
      const result = await this.bridge.call<SaveResult>("library.exportReport", { id: sessionId, reportId: view.id });
      if (result.saved) this.messages.say(tr("Relatório exportado atomicamente sem caminhos locais."));
    } catch (error) {
      this.messages.fail(tr("Não foi possível exportar o relatório"), "", { error });
    }
  }
}
