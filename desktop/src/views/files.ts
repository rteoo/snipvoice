// Arquivos: export text, download or prepare the final audio, and the danger zone.
import { allowEngineForeground, type Bridge } from "../bridge";
import { button, h, replace } from "../dom";
import { tr } from "../i18n";
import type { FilesView, SaveResult } from "../types";
import type { MessageSlot } from "../ui";

export class FilesPanel {
  readonly element = h("div", { class: "section-body stack" });
  private sessionId: string | null = null;
  private view: FilesView | null = null;
  private busy = false;
  private token = 0;

  constructor(
    private readonly bridge: Bridge,
    private readonly messages: MessageSlot,
    private readonly enginePid: number,
    private readonly onDelete: () => void,
  ) {}

  async show(sessionId: string): Promise<void> {
    const token = ++this.token;
    if (sessionId !== this.sessionId) this.view = null;
    this.sessionId = sessionId;
    this.render();
    try {
      const view = await this.bridge.call<FilesView>("library.files", { id: sessionId });
      if (token !== this.token) return;
      this.view = view;
    } catch (error) {
      if (token !== this.token) return;
      this.messages.fail(tr("O áudio desta gravação não está disponível."), "", { error });
    }
    this.render();
  }

  private render(): void {
    const view = this.view;
    const markdown = button(tr("Markdown…"), () => void this.save("library.exportText", { format: "markdown" }, (name) => tr("Transcrição exportada como {name}.", { name })));
    const text = button(tr("Texto…"), () => void this.save("library.exportText", { format: "text" }, (name) => tr("Transcrição exportada como {name}.", { name })));
    const download = button(tr("Baixar áudio final…"), () => void this.save("library.exportAudio", {}, (name) => tr("Áudio final salvo como {name}.", { name })), "primary");
    download.disabled = this.busy || !view?.final;
    const prepare = view?.canPrepare ? button(tr("Preparar áudio final"), () => void this.prepare()) : null;
    if (prepare) prepare.disabled = this.busy;
    for (const control of [markdown, text]) control.disabled = this.busy;
    const remove = button(tr("Excluir gravação…"), () => this.onDelete(), "danger");
    remove.disabled = this.busy;
    const audioFacts = !view
      ? tr("Carregando…")
      : view.final
        ? `${tr("O áudio mixado usado para ouvir e baixar.")} ${view.final.format} · ${view.final.size}.`
        : tr("Esta gravação ainda não tem áudio final salvo.");
    replace(
      this.element,
      h("section", { class: "card stack-sm" }, h("h3", { text: tr("Exportar texto") }), h("div", { class: "btn-row" }, markdown, text)),
      h("section", { class: "card stack-sm" }, h("h3", { text: tr("Áudio final") }), h("p", { class: "caption", text: audioFacts }), h("div", { class: "btn-row" }, download, prepare)),
      h("section", { class: "card stack-sm danger-zone" }, h("h3", { text: tr("Zona de risco") }), h("p", { class: "caption", text: tr("Vai para a lixeira, de onde ainda pode ser restaurada.") }), h("div", { class: "btn-row" }, remove)),
    );
  }

  private async save(method: string, params: Record<string, string>, receipt: (name: string) => string): Promise<void> {
    const sessionId = this.sessionId;
    if (!sessionId || this.busy) return;
    this.busy = true;
    this.render();
    try {
      await allowEngineForeground(this.enginePid);
      const result = await this.bridge.call<SaveResult>(method, { id: sessionId, ...params });
      if (result.saved) this.messages.say(receipt(result.name));
    } catch (error) {
      this.messages.fail(tr("Não foi possível concluir a operação"), "", { error });
    }
    this.busy = false;
    this.render();
  }

  private async prepare(): Promise<void> {
    const sessionId = this.sessionId;
    if (!sessionId || this.busy) return;
    try {
      await this.bridge.call("library.prepareAudio", { id: sessionId });
      this.messages.say(tr("Processamento local iniciado. O resultado aparecerá quando terminar."));
    } catch (error) {
      this.messages.fail(tr("O processamento não começou"), "", { error });
    }
  }
}
