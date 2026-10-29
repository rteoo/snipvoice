// The Gravação readiness strip: what stands between this computer and a good
// recording, each with the action that resolves it. Same rows as the Tk strip.
import type { Bridge } from "../bridge";
import { button, h, replace } from "../dom";
import { tr } from "../i18n";
import type { Destination, Readiness, ReadinessRow } from "../types";
import { toneText, type MessageSlot } from "../ui";

/** Where a row's action leads; the page wires these to navigation. */
export interface ReadinessTargets {
  navigate: (destination: Destination, detail?: { settings?: "models"; recordingId?: string }) => void;
}

const REFRESH_GAP_MS = 1000;

export class ReadinessStrip {
  readonly element = h("section", { class: "card stack-sm readiness", attrs: { "aria-labelledby": "readiness-heading" } });
  private last = 0;
  private timer: number | undefined;
  private attentionId: string | null = null;

  constructor(
    private readonly bridge: Bridge,
    private readonly messages: MessageSlot,
    private readonly targets: ReadinessTargets,
  ) {
    replace(this.element, h("h2", { text: tr("Prontidão"), attrs: { id: "readiness-heading" } }), h("p", { class: "caption", text: tr("Verificando modelos locais…") }));
  }

  /** Refresh at most once a second; events (state pushes) call this, nothing polls. */
  refresh(): void {
    const wait = this.last + REFRESH_GAP_MS - Date.now();
    if (wait > 0) {
      window.clearTimeout(this.timer);
      this.timer = window.setTimeout(() => this.refresh(), wait);
      return;
    }
    this.last = Date.now();
    void this.bridge.call<Readiness>("recording.readiness").then(
      (readiness) => this.render(readiness),
      (error: unknown) => this.render({ rows: [{ key: "inventory", tone: "warning", text: error instanceof Error ? error.message : tr("Não foi possível verificar os modelos locais."), action: "" }], attentionId: null }),
    );
  }

  private render(readiness: Readiness): void {
    this.attentionId = readiness.attentionId;
    replace(
      this.element,
      h("h2", { text: tr("Prontidão"), attrs: { id: "readiness-heading" } }),
      ...readiness.rows.map((row) =>
        h(
          "div",
          { class: "readiness-row" },
          h("p", { class: toneText(row.tone) || "caption", text: row.text }),
          row.action ? button(row.action, () => void this.act(row), "quiet") : null,
        ),
      ),
    );
  }

  private async act(row: ReadinessRow): Promise<void> {
    if (row.key === "startup") {
      this.messages.fail(tr("Recuperação local pendente."), row.text, { tone: "warning" });
    } else if (row.key === "inventory" || row.key === "transcription" || row.key === "summary") {
      this.targets.navigate("settings", { settings: "models" });
    } else if (row.key === "attention") {
      this.targets.navigate("library", this.attentionId ? { recordingId: this.attentionId } : {});
    } else if (row.key === "dictation") {
      this.targets.navigate("voice");
    } else if (row.key === "job") {
      try {
        await this.bridge.call("recording.cancelProcessing");
      } catch (error) {
        this.messages.fail(tr("Não foi possível concluir a ação."), "", { error });
      }
      this.refresh();
    }
  }
}
