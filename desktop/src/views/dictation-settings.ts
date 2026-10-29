// Ditado settings: model, language, shortcuts and mode (saved together), plus
// the commands, corrections and licenses editors and model removal.
import type { Bridge } from "../bridge";
import { button, h, replace } from "../dom";
import { tr } from "../i18n";
import type { Confirmable, DictationSettingsView, DictationValues } from "../types";
import { confirmDialog, type MessageSlot } from "../ui";
import { commandsDialog, correctionsDialog, licensesDialog } from "./dictation-dialogs";
import { saveBar } from "./settings-recording";

/** A chord as the engine stores it ("ctrl+alt+space"), from one key press; null for a lone modifier. */
export function chordFromEvent(event: KeyboardEvent): string | null {
  const code = event.code;
  let key: string;
  if (/^Key[A-Z]$/.test(code)) key = code.slice(3).toLowerCase();
  else if (/^Digit\d$/.test(code)) key = code.slice(5);
  else if (/^F([1-9]|1\d|2[0-4])$/.test(code)) key = code.toLowerCase();
  else if (code === "Space") key = "space";
  else if (code === "Enter" || code === "NumpadEnter") key = "enter";
  else if (["Control", "Alt", "Shift", "Meta", "OS"].some((name) => event.key.startsWith(name))) return null;
  else if (event.key.length === 1) key = event.key.toLowerCase();
  else return null;
  const modifiers = [event.ctrlKey && "ctrl", event.altKey && "alt", event.shiftKey && "shift", event.metaKey && "win"].filter(Boolean);
  return [...modifiers, key].join("+");
}

export const chordLabel = (spec: string): string => spec.split("+").filter(Boolean).map((part) => part[0]?.toUpperCase() + part.slice(1)).join(" + ");

export class DictationSettings {
  readonly element = h("div", { class: "stack" });
  private view: DictationSettingsView | null = null;
  private draft: DictationValues | null = null;
  private saving = false;
  private receipt = "";
  private shortcutNote = "";

  constructor(
    private readonly bridge: Bridge,
    private readonly messages: MessageSlot,
  ) {}

  get dirty(): boolean {
    return this.view !== null && this.draft !== null && JSON.stringify(this.draft) !== JSON.stringify(this.view.values);
  }

  async load(): Promise<void> {
    if (this.dirty) return this.render();
    try {
      this.accept(await this.bridge.call<DictationSettingsView>("dictation.settings"));
    } catch (error) {
      this.messages.fail(tr("Não foi possível carregar as configurações do ditado"), "", { error });
    }
    this.render();
  }

  private accept(view: DictationSettingsView): void {
    this.view = view;
    this.draft = { ...view.values };
  }

  private render(): void {
    const view = this.view;
    const draft = this.draft;
    if (!view || !draft) {
      replace(this.element, h("p", { class: "caption", text: tr("Carregando…") }));
      return;
    }
    const focused = this.element.contains(document.activeElement) ? document.activeElement?.id : "";
    const profile = h("select", { attrs: { id: "dictation-profile" } });
    for (const choice of view.profiles) profile.append(h("option", { text: choice.installed ? choice.label : `${choice.label} · ${tr("Não baixado")}`, attrs: { value: choice.id } }));
    profile.value = draft.profile;
    profile.addEventListener("change", () => {
      draft.profile = profile.value;
      const allowed = view.languages[draft.profile] ?? [];
      if (!allowed.some((choice) => choice.id === draft.language)) draft.language = allowed[0]?.id ?? "auto";
      this.render();
    });
    const language = h("select", { attrs: { id: "dictation-language" } });
    for (const choice of view.languages[draft.profile] ?? []) language.append(h("option", { text: choice.label, attrs: { value: choice.id } }));
    language.value = draft.language;
    language.addEventListener("change", () => {
      draft.language = language.value;
      this.renderBar();
    });
    const modes = h("div", { class: "segmented", attrs: { role: "radiogroup", "aria-label": tr("Modo de gravação:") } });
    for (const [value, label] of [["hold", tr("Segure para falar")], ["toggle", tr("Iniciar / Parar (mãos livres)")]] as const) {
      const node = button(label, () => {
        draft.activationMode = value;
        this.render();
      });
      node.setAttribute("role", "radio");
      node.setAttribute("aria-checked", String(draft.activationMode === value));
      node.setAttribute("aria-pressed", String(draft.activationMode === value));
      modes.append(node);
    }
    const remove = button(tr("Remover modelo"), () => void this.removeModel(), "quiet");
    remove.disabled = !view.profiles.find((choice) => choice.id === view.values.profile)?.installed;
    replace(
      this.element,
      h(
        "section",
        { class: "card stack-sm" },
        h("h2", { text: tr("Modelo e idioma") }),
        h("label", { class: "field" }, h("span", { text: tr("Modelo de transcrição local") }), profile),
        h("label", { class: "field" }, h("span", { text: tr("Idioma") }), language),
      ),
      h(
        "section",
        { class: "card stack-sm" },
        h("h2", { text: tr("Atalhos") }),
        this.shortcutField("dictation-hotkey", tr("Ditado:"), "hotkey"),
        this.shortcutField("dictation-command-hotkey", tr("Comando por voz:"), "commandHotkey"),
        h("p", { class: "caption", attrs: { role: "status" }, text: this.shortcutNote || tr("Selecione o campo e pressione o atalho. Tab avança; Esc sai do campo.") }),
        h("div", { class: "field" }, h("span", { text: tr("Modo de gravação:") }), modes),
        h("p", { class: "caption", text: tr("Mãos livres: pressione o atalho para iniciar e novamente para parar. Vale para os dois atalhos. Esc cancela.") }),
      ),
      h(
        "section",
        { class: "card stack-sm" },
        h("h2", { text: tr("Comandos por voz") }),
        h("p", { class: "caption", text: `${view.commands === 1 ? tr("1 comando") : tr("{count} comandos", { count: view.commands })} · ${view.corrections === 1 ? tr("1 correção") : tr("{count} correções", { count: view.corrections })}` }),
        h(
          "div",
          { class: "btn-row" },
          button(tr("Configurar comandos…"), () => void commandsDialog(this.bridge).then((saved) => { if (saved) void this.refreshCounts(); })),
          button(tr("Correções…"), () => void correctionsDialog(this.bridge).then((saved) => { if (saved) void this.refreshCounts(); })),
          button(tr("Recarregar comandos"), () => void this.reloadCommands(), "quiet"),
          button(tr("Licenças e atribuições…"), () => void licensesDialog(this.bridge), "quiet"),
          remove,
        ),
      ),
      h("div", { class: "save-slot" }),
    );
    if (focused) this.element.querySelector<HTMLElement>(`#${focused}`)?.focus();
    this.renderBar();
  }

  /** A field that records the next chord pressed; global hotkeys pause while it has focus. */
  private shortcutField(id: string, label: string, field: "hotkey" | "commandHotkey"): HTMLElement {
    const draft = this.draft;
    const input = h("input", { attrs: { id, type: "text", readonly: "", autocomplete: "off", "aria-describedby": `${id}-label` } });
    input.value = draft ? chordLabel(draft[field]) : "";
    let ready = false;
    input.addEventListener("focus", () => {
      void this.bridge.call<{ ready: boolean; message: string }>("dictation.beginShortcutEdit").then((result) => {
        ready = result.ready;
        this.note(result.message);
      }, (error: unknown) => this.messages.fail(tr("Não foi possível concluir a ação."), "", { error }));
    });
    input.addEventListener("blur", () => {
      ready = false;
      this.note("");
      // A failure here would leave the global shortcuts paused, so it must show.
      void this.bridge.call("dictation.endShortcutEdit").catch((error: unknown) =>
        this.messages.fail(tr("Os atalhos globais podem continuar pausados"), tr("Feche e abra a janela para retomá-los."), { error }));
    });
    input.addEventListener("keydown", (event) => {
      if (event.key === "Tab") return;
      event.preventDefault();
      if (event.key === "Escape") return input.blur();
      if (!ready || !this.draft) return;
      const chord = chordFromEvent(event);
      if (!chord) return;
      if (!chord.includes("+")) return this.note(tr("Pressione um modificador e uma tecla para definir o atalho."));
      this.draft[field] = chord;
      input.value = chordLabel(chord);
      this.renderBar();
    });
    return h("label", { class: "field" }, h("span", { text: label, attrs: { id: `${id}-label` } }), input);
  }

  private note(text: string): void {
    this.shortcutNote = text;
    const status = this.element.querySelector<HTMLElement>(".card [role=status]");
    if (status) status.textContent = text || tr("Selecione o campo e pressione o atalho. Tab avança; Esc sai do campo.");
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

  private async save(confirmed = false): Promise<void> {
    if (!this.draft || this.saving) return;
    this.saving = true;
    this.renderBar();
    try {
      const result = await this.bridge.call<{ status: "saved"; settings: DictationSettingsView } | { status: "confirm"; title: string; message: string }>("dictation.save", { values: this.draft, confirmed });
      if (result.status === "confirm") {
        this.saving = false;
        if (await confirmDialog({ title: result.title, body: result.message, confirm: tr("Baixar e salvar") })) return this.save(true);
        return this.renderBar();
      }
      this.accept(result.settings);
      this.receipt = tr("Configurações do ditado salvas.");
      this.messages.clear();
    } catch (error) {
      this.messages.fail(tr("Não foi possível salvar. Tente novamente."), "", { error });
    }
    this.saving = false;
    this.render();
  }

  private async refreshCounts(): Promise<void> {
    try {
      const fresh = await this.bridge.call<DictationSettingsView>("dictation.settings");
      if (this.view) Object.assign(this.view, { commands: fresh.commands, corrections: fresh.corrections });
    } catch (error) {
      this.messages.fail(tr("Não foi possível carregar as configurações do ditado"), "", { error });
    }
    this.render();
  }

  private async reloadCommands(): Promise<void> {
    try {
      await this.bridge.call("dictation.reloadCommands");
      this.messages.say(tr("Comandos recarregados."));
      await this.refreshCounts();
    } catch (error) {
      this.messages.fail(tr("Não foi possível concluir a ação."), "", { error });
    }
  }

  private async removeModel(): Promise<void> {
    try {
      const first = await this.bridge.call<Confirmable>("dictation.removeModel");
      if (first.status !== "confirm" || !(await confirmDialog({ title: first.title, body: first.message, confirm: tr("Remover"), danger: true }))) return;
      await this.bridge.call("dictation.removeModel", { confirmed: true });
      this.messages.say(tr("Modelo removido. A entrada por voz foi desligada."));
      this.view = null;
      await this.load();
    } catch (error) {
      this.messages.fail(tr("Não foi possível remover o modelo."), "", { error });
    }
  }
}
