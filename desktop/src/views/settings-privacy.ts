// Configurações › Privacidade: the recording notice, chat answers and retention.
import type { Bridge } from "../bridge";
import { h, replace } from "../dom";
import { tr } from "../i18n";
import type { PrivacyView } from "../types";
import type { MessageSlot } from "../ui";
import { saveBar } from "./settings-recording";

type Draft = Omit<PrivacyView, "generation">;

export class PrivacySettings {
  readonly element = h("div", { class: "stack" });
  private view: PrivacyView | null = null;
  private draft: Draft | null = null;
  private saving = false;
  private receipt = "";

  constructor(
    private readonly bridge: Bridge,
    private readonly messages: MessageSlot,
  ) {}

  get dirty(): boolean {
    if (!this.view || !this.draft) return false;
    const { generation: _generation, ...saved } = this.view;
    return JSON.stringify(this.draft) !== JSON.stringify(saved);
  }

  async load(): Promise<void> {
    if (this.dirty) return this.render();
    try {
      this.accept(await this.bridge.call<PrivacyView>("settings.privacy"));
    } catch (error) {
      this.messages.fail(tr("Privacidade local indisponível"), "", { error });
    }
    this.render();
  }

  private accept(view: PrivacyView): void {
    this.view = view;
    const { generation: _generation, ...draft } = view;
    this.draft = draft;
  }

  private render(): void {
    const draft = this.draft;
    if (!draft) {
      replace(this.element, h("p", { class: "caption", text: tr("Configurações de privacidade carregando…") }));
      return;
    }
    const check = (id: string, value: boolean, onChange: (checked: boolean) => void) => {
      const node = h("input", { attrs: { type: "checkbox", id } });
      node.checked = value;
      node.addEventListener("change", () => {
        onChange(node.checked);
        this.render();
      });
      return node;
    };
    const days = (id: string, value: string, enabled: boolean, onInput: (text: string) => void) => {
      const node = h("input", { class: "days", attrs: { type: "text", inputmode: "decimal", id, maxlength: 8 } });
      node.value = value;
      node.disabled = !enabled;
      node.addEventListener("input", () => {
        onInput(node.value);
        this.renderBar();
      });
      return node;
    };
    const noticeLanguage = h("select", { attrs: { id: "privacy-notice-language", "aria-label": tr("Idioma do aviso") } });
    for (const [code, label] of [["pt-BR", tr("Português (Brasil)")], ["en-US", tr("Inglês (Estados Unidos)")]] as const) {
      noticeLanguage.append(h("option", { text: label, attrs: { value: code } }));
    }
    noticeLanguage.value = draft.noticeLanguage;
    noticeLanguage.disabled = !draft.noticeEnabled;
    noticeLanguage.addEventListener("change", () => {
      draft.noticeLanguage = noticeLanguage.value as Draft["noticeLanguage"];
      this.renderBar();
    });
    const focused = this.element.contains(document.activeElement) ? document.activeElement?.id : "";
    replace(
      this.element,
      h(
        "section",
        { class: "card stack-sm" },
        h("h2", { text: tr("Privacidade e retenção local") }),
        h("p", { class: "caption", text: tr("O SnipVoice só grava quando você inicia. Estas opções ficam neste computador.") }),
        h("div", { class: "check-row" }, h("label", { class: "check" }, check("privacy-notice", draft.noticeEnabled, (value) => (draft.noticeEnabled = value)), h("span", { text: tr("Mostrar um aviso antes de cada gravação, em") })), noticeLanguage),
        h("p", { class: "caption", text: tr("Um texto para ler ou colar na chamada avisando que a reunião será gravada.") }),
        h("label", { class: "check" }, check("privacy-qa", draft.qaMode === "memory_only", (value) => (draft.qaMode = value ? "memory_only" : "explicit_save")), h("span", { text: tr("Nunca salvar respostas do chat") })),
        h("p", { class: "caption", text: tr("As respostas somem ao fechar a gravação, e o botão Salvar fica oculto.") }),
        h(
          "div",
          { class: "check-row" },
          h("label", { class: "check" }, check("privacy-auto", draft.autoTrash, (value) => (draft.autoTrash = value)), h("span", { text: tr("Mover gravações para a lixeira automaticamente após") })),
          days("privacy-auto-days", draft.autoTrashDays, draft.autoTrash, (text) => (draft.autoTrashDays = text)),
          h("span", { text: tr("dias") }),
        ),
        h("p", { class: "caption", text: tr("A gravação inteira vai para a lixeira, de onde ainda pode ser restaurada.") }),
        h(
          "div",
          { class: "check-row" },
          h("label", { attrs: { for: "privacy-trash-days" }, text: tr("Manter itens na lixeira por") }),
          days("privacy-trash-days", draft.trashDays, true, (text) => (draft.trashDays = text)),
          h("span", { text: tr("dias") }),
        ),
        h("p", { class: "caption", text: tr("Depois disso, Esvaziar expirados, na lixeira da Biblioteca, apaga esses itens de vez.") }),
        h("p", { class: "caption", text: tr("Apagar não garante eliminação forense em SSDs, e cópias que você exportou para outras pastas não são afetadas.") }),
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

  private discard(): void {
    if (!this.view) return;
    this.accept(this.view);
    this.render();
  }

  private async save(): Promise<void> {
    if (!this.view || !this.draft || this.saving) return;
    this.saving = true;
    this.renderBar();
    try {
      this.accept(await this.bridge.call<PrivacyView>("settings.savePrivacy", { ...this.draft, generation: this.view.generation }));
      this.receipt = tr("Privacidade e retenção salvas localmente.");
      this.messages.clear();
    } catch (error) {
      this.messages.fail(tr("Não foi possível salvar a privacidade"), "", { error });
    }
    this.saving = false;
    this.render();
  }
}
