// A folder the engine owns (data or models): where it is, and move-and-restart.
import { allowEngineForeground, type Bridge } from "../bridge";
import { button, h, replace } from "../dom";
import { tr } from "../i18n";
import type { LocationView, MovePlan } from "../types";
import { confirmDialog, type MessageSlot } from "../ui";

export type LocationKind = "data" | "models";

const TEXT: Record<LocationKind, { title: () => string; description: () => string; lock: () => string }> = {
  data: {
    title: () => tr("Pasta de dados"),
    description: () =>
      tr("Configurações, histórico de ditado, gravações e a biblioteca de reuniões ficam nesta pasta. Ao escolher outra, o SnipVoice move tudo para lá e reinicia. Os modelos baixados têm uma pasta própria, na seção Modelos."),
    lock: () => tr("Definida pela variável SNIPVOICE_HOME; altere-a fora do aplicativo."),
  },
  models: {
    title: () => tr("Pasta dos modelos"),
    description: () =>
      tr("Modelos de transcrição e de resumo baixados. Os arquivos GGUF ficam em voice-models e summary-models, prontos para outros aplicativos compatíveis. Pode ser uma pasta compartilhada: outros arquivos dela não são alterados."),
    lock: () => tr("Definida por SNIPVOICE_VOICE_CACHE, SNIPVOICE_SUMMARY_CACHE ou voice_cache_dir em settings.json."),
  },
};

export class LocationCard {
  readonly element = h("section", { class: "card stack-sm" });
  private busy = false;
  private view: LocationView | undefined;

  constructor(
    private readonly bridge: Bridge,
    private readonly messages: MessageSlot,
    private readonly kind: LocationKind,
    private readonly enginePid: number,
  ) {}

  show(view: LocationView | undefined): void {
    this.view = view;
    const text = TEXT[this.kind];
    if (!view) {
      replace(this.element, h("h2", { text: text.title() }), h("p", { class: "caption", text: tr("Carregando…") }));
      return;
    }
    const path = h("input", { attrs: { type: "text", readonly: "", "aria-label": text.title() } });
    path.value = view.path;
    const move = button(tr("Mover para…"), () => void this.plan(false));
    move.disabled = this.busy || view.locked;
    const restore = button(tr("Restaurar padrão"), () => void this.plan(true), "quiet");
    restore.disabled = this.busy || view.locked || view.atDefault;
    replace(
      this.element,
      h("h2", { text: text.title() }),
      h("p", { class: "caption", text: text.description() }),
      h("div", { class: "path-row" }, path, move, restore),
      view.locked ? h("p", { class: "caption", text: text.lock() }) : null,
    );
  }

  private async plan(restoreDefault: boolean): Promise<void> {
    if (this.busy) return;
    this.busy = true;
    this.show(this.view);
    try {
      if (!restoreDefault) await allowEngineForeground(this.enginePid);
      const plan = await this.bridge.call<MovePlan>("settings.planMove", { kind: this.kind, restoreDefault });
      if (plan.target !== null && (await confirmDialog({ title: plan.title, body: plan.question, confirm: tr("Mover e reiniciar") }))) {
        await this.bridge.call("settings.move", { kind: this.kind, target: plan.target });
        this.messages.say(tr("Movendo e reiniciando o SnipVoice…"));
        return;
      }
    } catch (error) {
      this.messages.fail(TEXT[this.kind].title(), "", { error });
    }
    this.busy = false;
    this.show(this.view);
  }
}
