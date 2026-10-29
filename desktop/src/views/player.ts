// Playback of a recording's saved final audio. The engine plays it; its
// progress arrives with every recording.state push.
import type { Bridge } from "../bridge";
import { button, h, replace } from "../dom";
import { tr } from "../i18n";
import type { RecordingView, SessionDetail } from "../types";
import type { MessageSlot } from "../ui";

const clock = (seconds: number): string => new Date(Math.max(0, Math.floor(seconds)) * 1000).toISOString().slice(11, 19);

/** "00:01:02 Microfone: text" (meeting_text._timestamped) → 62; null without a leading time. */
export function lineStart(line: string): number | null {
  const match = /^(\d{2,}):([0-5]\d):([0-5]\d)(?=[\s:])/.exec(line.trim());
  return match ? Number(match[1]) * 3600 + Number(match[2]) * 60 + Number(match[3]) : null;
}

export class Player {
  readonly element = h("div", { class: "player" });
  private detail: SessionDetail | null = null;
  private playback: RecordingView["playback"] | null = null;
  private generation = -1;
  private dragging = false;
  private failedGeneration = -1;
  private readonly toggle = button(tr("Reproduzir"), () => void this.togglePlayback(), "primary");
  private readonly slider = h("input", { attrs: { type: "range", min: 0, step: 1, "aria-label": tr("Posição do áudio") } });
  private readonly time = h("span", { class: "caption num" });
  private readonly status = h("p", { class: "caption", attrs: { role: "status" } });

  constructor(
    private readonly bridge: Bridge,
    private readonly messages: MessageSlot,
  ) {
    this.slider.addEventListener("pointerdown", () => (this.dragging = true));
    this.slider.addEventListener("input", () => (this.time.textContent = `${clock(Number(this.slider.value))} / ${clock(this.duration)}`));
    this.slider.addEventListener("change", () => {
      this.dragging = false;
      if (this.playing) void this.play(Number(this.slider.value));
    });
    replace(this.element, this.toggle, this.slider, this.time, this.status);
  }

  private get duration(): number {
    return this.detail?.durationSeconds ?? 0;
  }

  private get playing(): boolean {
    return Boolean(this.playback?.active && this.playback.sessionId === this.detail?.id);
  }

  show(detail: SessionDetail): void {
    if (detail.id !== this.detail?.id) {
      this.slider.value = "0";
      this.status.textContent = detail.playable ? tr("Pronto para ouvir.") : tr("Esta gravação ainda não tem áudio final salvo.");
    }
    this.detail = detail;
    this.slider.max = String(Math.max(1, Math.round(this.duration)));
    this.element.hidden = false;
    this.sync();
  }

  /** Apply the engine's playback state, ignoring generations older than one already applied. */
  update(playback: RecordingView["playback"]): void {
    if (playback.generation < this.generation) return;
    this.generation = playback.generation;
    this.playback = playback;
    if (playback.failed && playback.sessionId === this.detail?.id && playback.generation !== this.failedGeneration) {
      this.failedGeneration = playback.generation;
      this.messages.fail(tr("A reprodução falhou"), tr("Tente de novo. O áudio final continua salvo."));
    }
    this.sync();
  }

  /** Play from a transcript line's timestamp. */
  async playFrom(seconds: number): Promise<void> {
    this.slider.value = String(Math.round(seconds));
    await this.play(seconds);
  }

  private sync(): void {
    const detail = this.detail;
    if (!detail) return;
    const playing = this.playing;
    this.toggle.textContent = playing ? tr("Parar") : tr("Reproduzir");
    this.toggle.disabled = !detail.playable;
    this.slider.disabled = !detail.playable;
    const position = playing ? (this.playback?.position ?? 0) : Number(this.slider.value);
    if (playing && !this.dragging) this.slider.value = String(Math.round(position));
    this.time.textContent = `${clock(position)} / ${clock(this.duration)}`;
    if (playing) this.status.textContent = tr("Reproduzindo {track} · {time}", { track: tr("Áudio final"), time: clock(position) });
  }

  private async togglePlayback(): Promise<void> {
    if (this.playing) {
      try {
        await this.bridge.call("library.stopPlayback");
        this.status.textContent = tr("Reprodução parada.");
      } catch (error) {
        this.messages.fail(tr("Não foi possível concluir a ação."), "", { error });
      }
      return;
    }
    const start = Number(this.slider.value);
    await this.play(this.duration && start >= this.duration ? 0 : start);
  }

  private async play(start: number): Promise<void> {
    const detail = this.detail;
    if (!detail?.playable) return;
    this.status.textContent = tr("Iniciando reprodução…");
    try {
      await this.bridge.call("library.play", { id: detail.id, start });
    } catch (error) {
      this.status.textContent = tr("Não foi possível reproduzir o áudio.");
      this.messages.fail(tr("Não foi possível reproduzir o áudio"), "", { error });
    }
  }
}
