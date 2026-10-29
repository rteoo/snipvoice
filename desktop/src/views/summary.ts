// Resumo: the newest summary version, and generating another with a format and focus.
import { RpcError, type Bridge } from "../bridge";
import { button, h, replace } from "../dom";
import { tr } from "../i18n";
import type { Confirmable, SummaryProfile, SummaryView } from "../types";
import { confirmDialog, copyText, type MessageSlot } from "../ui";
import { VersionsPanel } from "./versions";

const MAX_FOCUS = 600;
export const CANCELLED = -32001;

/** Headings end with ":" and bullets start with "• " in the engine's summary text. */
export function summaryBody(text: string): HTMLElement {
  const article = h("article", { class: "summary selectable", attrs: { tabindex: 0 } });
  for (const line of text.split("\n")) {
    if (!line.trim()) continue;
    if (line.startsWith("• ")) article.append(h("p", { class: "bullet", text: line.slice(2) }));
    else if (line.endsWith(":")) article.append(h("h3", { text: line.slice(0, -1) }));
    else article.append(h("p", { text: line }));
  }
  return article;
}

export class SummaryPanel {
  readonly element = h("div", { class: "section-body stack" });
  private sessionId: string | null = null;
  private view: SummaryView | null = null;
  private profileId = "";
  private focusText = "";
  private pending: string | null = null;
  private setupOpen = false;
  private token = 0;
  private readonly versions: VersionsPanel;

  constructor(
    private readonly bridge: Bridge,
    private readonly messages: MessageSlot,
    enginePid: number,
  ) {
    this.versions = new VersionsPanel(bridge, messages, enginePid);
  }

  show(sessionId: string, view: SummaryView | null): void {
    if (sessionId !== this.sessionId) {
      this.focusText = "";
      this.profileId = "";
    }
    this.sessionId = sessionId;
    this.view = view;
    if (view && !view.profiles.some((profile) => profile.id === this.profileId)) this.profileId = view.profileId;
    this.versions.show(sessionId);
    this.render();
  }

  /** Reload formats and the latest summary after a format changed. */
  private async refreshView(selectId?: string): Promise<void> {
    const sessionId = this.sessionId;
    if (!sessionId) return;
    try {
      const view = await this.bridge.call<SummaryView>("library.summary", { id: sessionId });
      if (this.sessionId !== sessionId) return;
      this.view = view;
      if (selectId && view.profiles.some((profile) => profile.id === selectId)) this.profileId = selectId;
      else if (!view.profiles.some((profile) => profile.id === this.profileId)) this.profileId = view.profileId;
    } catch (error) {
      this.messages.fail(tr("Não foi possível carregar os formatos de resumo"), "", { error });
    }
    this.render();
  }

  private render(): void {
    const view = this.view;
    if (!view) {
      replace(this.element, h("p", { class: "caption", text: tr("Carregando resumo…") }));
      return;
    }
    const busy = this.pending !== null;
    const here = this.pending === this.sessionId;
    const copy = button(tr("Copiar resumo"), () => void this.copy(), view.text ? "primary" : "neutral");
    copy.disabled = !view.text;

    const select = h("select", { attrs: { id: "summary-format" } });
    for (const profile of view.profiles) {
      const option = h("option", { text: profile.disabled ? `${profile.name} · ${tr("desativado")}` : profile.name, attrs: { value: profile.id } });
      select.append(option);
    }
    select.value = this.profileId;
    const description = h("p", { class: "caption" });
    const describe = () => {
      description.textContent = view.profiles.find((profile) => profile.id === this.profileId)?.description ?? "";
    };
    describe();
    select.addEventListener("change", () => {
      this.profileId = select.value;
      this.render();
    });
    const current = view.profiles.find((profile) => profile.id === this.profileId);
    const formats = h(
      "div",
      { class: "btn-row" },
      button(tr("Criar…"), () => void this.editProfile("create", current), "quiet"),
      current ? button(tr("Duplicar…"), () => void this.editProfile("duplicate", current), "quiet") : null,
      current?.custom ? button(tr("Editar…"), () => void this.editProfile("edit", current), "quiet") : null,
      current?.custom ? button(current.disabled ? tr("Ativar") : tr("Desativar"), () => void this.setEnabled(current), "quiet") : null,
      current?.custom ? button(tr("Excluir…"), () => void this.deleteProfile(current), "quiet") : null,
    );
    const focus = h("textarea", { attrs: { id: "summary-focus", rows: 3, maxlength: MAX_FOCUS } });
    focus.placeholder = tr("Ex.: priorize próximos passos e dúvidas que precisam de decisão.");
    focus.value = this.focusText;
    focus.addEventListener("input", () => (this.focusText = focus.value));
    const generate = button(here ? tr("Gerando…") : view.text ? tr("Gerar outra versão") : tr("Gerar resumo"), () => void this.generate(), view.text ? "neutral" : "primary");
    generate.disabled = busy || !view.modelReady || !view.profiles.length;
    const cancel = here ? button(tr("Cancelar"), () => void this.cancel(), "quiet") : null;

    // A running generation keeps its Cancel in view; otherwise the panel stays as the person left it.
    const toggle = h("summary", { text: view.text ? tr("Gerar outra versão") : tr("Gerar resumo") });
    const setup = h(
      "details",
      { class: "card stack-sm", attrs: !view.text || here || this.setupOpen ? { open: "" } : {} },
      toggle,
      h("p", { class: "caption", text: tr("Escolha um formato. Cada resumo é salvo como uma nova versão nesta gravação.") }),
      h("label", { class: "field" }, h("span", { text: tr("Formato") }), select),
      description,
      h("p", { class: "caption", text: tr("Formatos personalizados") }),
      formats,
      h("label", { class: "field" }, h("span", { text: tr("Foco adicional (opcional, até 600 caracteres)") }), focus),
      view.modelReady ? null : h("p", { class: "caption text-warning", text: tr("Instale um modelo de resumo em Configurações → Modelos para continuar.") }),
      h("div", { class: "btn-row" }, generate, cancel),
    );
    toggle.addEventListener("click", () => (this.setupOpen = !setup.open));
    replace(
      this.element,
      h(
        "div",
        { class: "detail-commands" },
        h("p", { class: "caption", text: here ? tr("Gerando {name} localmente… O resumo atual será preservado.", { name: select.selectedOptions[0]?.text ?? "" }) : view.name ? tr("{name} · Versão salva nesta gravação.", { name: view.name }) : view.hint }),
        copy,
      ),
      view.text ? summaryBody(view.text) : h("p", { class: "empty", text: tr("O resumo aparecerá aqui após o processamento local.") }),
      setup,
      this.versions.element,
    );
  }

  private async editProfile(mode: "create" | "duplicate" | "edit", base: SummaryProfile | undefined): Promise<void> {
    const values = await profileDialog(
      mode === "duplicate" && base ? tr("{name} (cópia)", { name: base.baseName || base.name }) : mode === "edit" && base ? base.baseName : "",
      base && mode !== "create" ? base.instructions : "",
    );
    if (!values) return;
    try {
      const saved = await this.bridge.call<{ profileId: string }>("library.saveProfile", { mode, baseId: base?.id ?? "", ...values });
      this.messages.say(tr("Perfil de relatório salvo."));
      await this.refreshView(saved.profileId);
    } catch (error) {
      this.messages.fail(tr("Não foi possível salvar o perfil"), "", { error });
    }
  }

  private async setEnabled(profile: SummaryProfile): Promise<void> {
    try {
      await this.bridge.call("library.setProfileEnabled", { profileId: profile.id, enabled: profile.disabled });
      await this.refreshView(profile.id);
    } catch (error) {
      this.messages.fail(tr("Não foi possível salvar o perfil"), "", { error });
    }
  }

  private async deleteProfile(profile: SummaryProfile): Promise<void> {
    try {
      const first = await this.bridge.call<Confirmable>("library.deleteProfile", { profileId: profile.id });
      if (first.status !== "confirm" || !(await confirmDialog({ title: first.title, body: first.message, confirm: tr("Excluir"), danger: true }))) return;
      await this.bridge.call("library.deleteProfile", { profileId: profile.id, confirmed: true });
      await this.refreshView();
    } catch (error) {
      this.messages.fail(tr("Não foi possível salvar o perfil"), "", { error });
    }
  }

  private async generate(): Promise<void> {
    const sessionId = this.sessionId;
    if (!sessionId || this.pending) return;
    const token = ++this.token;
    this.pending = sessionId;
    this.messages.clear();
    this.render();
    try {
      const view = await this.bridge.call<SummaryView>("library.generateSummary", { id: sessionId, profileId: this.profileId, focus: this.focusText });
      if (token !== this.token) return;
      this.pending = null;
      if (this.sessionId === sessionId) {
        this.view = view;
        this.focusText = "";
        this.versions.invalidate();
        this.messages.say(tr("Resumo salvo nesta gravação. Você pode copiar ou gerar outra versão."));
      }
    } catch (error) {
      if (token !== this.token) return;
      this.pending = null;
      if (error instanceof RpcError && error.code === CANCELLED) {
        this.messages.fail(tr("O resumo foi cancelado"), tr("O resumo anterior foi preservado. Gere de novo quando quiser."), { tone: "warning" });
      } else {
        this.messages.fail(tr("Não foi possível gerar o resumo"), "", { error, action: { label: tr("Gerar resumo"), run: () => void this.generate() } });
      }
    }
    this.render();
  }

  private async cancel(): Promise<void> {
    try {
      await this.bridge.call("library.cancelJob", { kind: "summary" });
    } catch (error) {
      this.messages.fail(tr("Não foi possível concluir a ação."), "", { error });
    }
  }

  private async copy(): Promise<void> {
    if (!this.view?.text) return;
    if (await copyText(this.view.text)) this.messages.say(tr("Resumo copiado."));
    else this.messages.fail(tr("Não foi possível copiar o resumo"), tr("Selecione o texto e pressione Ctrl+C"), { tone: "warning" });
  }
}

/** The custom-format form: a name and optional extra instructions, with the Tk limits. */
function profileDialog(name: string, instructions: string): Promise<{ name: string; instructions: string } | null> {
  return new Promise((resolve) => {
    const opener = document.activeElement as HTMLElement | null;
    const dialog = h("dialog", { attrs: { "aria-labelledby": "profile-title" } });
    const nameInput = h("input", { attrs: { type: "text", maxlength: 128 } });
    nameInput.value = name;
    const instructionsInput = h("textarea", { attrs: { rows: 5, maxlength: 2000 } });
    instructionsInput.value = instructions;
    const status = h("p", { class: "caption text-warning", attrs: { role: "status" } });
    const finish = (value: { name: string; instructions: string } | null) => {
      dialog.close();
      dialog.remove();
      opener?.focus();
      resolve(value);
    };
    const save = button(tr("Salvar"), () => {
      if (!nameInput.value.trim() || nameInput.value.length > 128) {
        status.textContent = tr("Nome: use de 1 a 128 caracteres.");
        return;
      }
      finish({ name: nameInput.value.trim(), instructions: instructionsInput.value });
    }, "primary");
    dialog.append(
      h("h2", { text: tr("Formato personalizado"), attrs: { id: "profile-title" } }),
      h("label", { class: "field" }, h("span", { text: tr("Nome") }), nameInput),
      h("label", { class: "field" }, h("span", { text: tr("Instruções adicionais") }), instructionsInput),
      status,
      h("div", { class: "dialog-actions" }, button(tr("Cancelar"), () => finish(null)), save),
    );
    dialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      finish(null);
    });
    document.body.append(dialog);
    dialog.showModal();
    nameInput.focus();
  });
}
