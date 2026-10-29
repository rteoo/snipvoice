// Gravação: check sources, record, and read the recorder's state.
import type { Bridge } from "../bridge";
import { button, h, replace } from "../dom";
import { tr } from "../i18n";
import type { Destination, Device, EndpointSelection, RecordingView, SourceChoice, StartResult, TrackView } from "../types";
import { confirmDialog, MessageSlot, pageHeader, toneClass, toneText } from "../ui";
import { ReadinessStrip } from "./readiness";

type Track = "microphone" | "system";
const TRACKS: Track[] = ["microphone", "system"];

interface Option {
  label: string;
  selection: EndpointSelection;
}

function deviceName(device: Device, track: Track): string {
  return device.name && device.name !== device.id ? device.name : track === "microphone" ? tr("Dispositivo de entrada") : tr("Dispositivo de saída");
}

function sameSelection(a: EndpointSelection, b: EndpointSelection): boolean {
  return a.mode === "manual" ? b.mode === "manual" && a.endpoint_id === b.endpoint_id : b.mode === "default" && a.default_role === b.default_role;
}

/** Mirrors meeting_gui.endpoint_options: defaults first, then devices; a missing pinned device stays visible. */
function endpointOptions(devices: Device[], track: Track, current: EndpointSelection): Option[] {
  const mine = devices.filter((device) => device.kind === track && device.id);
  const options: Option[] = [];
  for (const [label, flag, role] of [
    [tr("Padrão do sistema"), "default", "multimedia"],
    [tr("Padrão para chamadas"), "communicationsDefault", "communications"],
  ] as const) {
    const device = mine.find((item) => item[flag] === true);
    options.push({ label: device ? `${label} · ${deviceName(device, track)}` : label, selection: { mode: "default", default_role: role } });
  }
  const used = new Set(options.map((option) => option.label));
  for (const device of mine) {
    let label = deviceName(device, track);
    for (let suffix = 2; used.has(label); suffix++) label = `${deviceName(device, track)} (${suffix})`;
    used.add(label);
    options.push({ label, selection: { mode: "manual", endpoint_id: device.id } });
  }
  if (current.mode === "manual" && !mine.some((device) => device.id === current.endpoint_id)) {
    options.push({ label: tr("Dispositivo selecionado indisponível"), selection: current });
  }
  return options;
}

/** Mirrors meeting_gui.endpoint_hint. Returns [text, isWarning]. */
function endpointHint(devices: Device[] | null, track: Track, choice: SourceChoice): [string, boolean] {
  if (!choice.enabled) return [tr("Desativado · não será incluído na próxima gravação."), false];
  if (devices === null) return [tr("Buscando dispositivos…"), false];
  const mine = devices.filter((device) => device.kind === track && device.id);
  const selection = choice.selection;
  if (selection.mode === "manual") {
    if (!mine.some((device) => device.id === selection.endpoint_id)) return [tr("Dispositivo desconectado. Conecte-o ou escolha outro."), true];
    return [tr("Usa sempre este dispositivo, mesmo se o padrão do sistema mudar."), false];
  }
  const flag = selection.default_role === "communications" ? "communicationsDefault" : "default";
  if (!mine.length || (mine.some((device) => device[flag] !== null) && !mine.some((device) => device[flag] === true))) {
    return [tr("Nenhum dispositivo padrão disponível. Conecte um ou escolha outro."), true];
  }
  return selection.default_role === "communications"
    ? [tr("Automático · acompanha o dispositivo padrão para chamadas."), false]
    : [tr("Automático · acompanha o dispositivo padrão do sistema."), false];
}

export class RecordingPage {
  readonly element: HTMLElement;
  readonly messages = new MessageSlot();
  private view: RecordingView | null = null;
  private devices: Device[] | null = null;
  private readonly choices: Record<Track, SourceChoice> = {
    microphone: { enabled: true, selection: { mode: "default", default_role: "multimedia" } },
    system: { enabled: true, selection: { mode: "default", default_role: "multimedia" } },
  };
  private readonly title = h("input", { attrs: { type: "text", maxlength: 400, id: "recording-title", autocomplete: "off" } });
  private readonly sourceRows = h("div", { class: "stack" });
  private readonly status = h("span", { class: "status-chip" });
  private readonly transport = h("div", { class: "btn-row" });
  private readonly blocked = h("p", { class: "caption" });
  private readonly lostSlot = h("div");
  private readonly exportProgress = h("progress", { attrs: { max: 100 } });
  private readonly meters = h("div", { class: "stack" });
  private readonly waveform = h("canvas", { class: "waveform", attrs: { height: 120, "aria-hidden": "true" } });
  private readonly previewButton = button(tr("Testar fontes"), () => void this.preview());
  private busy = false;
  private previewRunning = false;
  private startAfterInterrupt = false;
  readonly readiness: ReadinessStrip;

  /** Set by the shell: where readiness actions lead. */
  onNavigate: (destination: Destination, detail?: { settings?: "models"; recordingId?: string }) => void = () => {};

  constructor(private readonly bridge: Bridge) {
    this.readiness = new ReadinessStrip(bridge, this.messages, { navigate: (destination, detail) => this.onNavigate(destination, detail) });
    this.title.placeholder = tr("Opcional");
    this.exportProgress.hidden = true;
    this.element = h(
      "section",
      { class: "page", attrs: { "aria-labelledby": "recording-heading" } },
      pageHeader(tr("Gravação")),
      this.messages.element,
      h(
        "div",
        { class: "page-body recording-grid" },
        h(
          "div",
          { class: "stack" },
          h(
            "div",
            { class: "card stack" },
            h("h2", { text: tr("Fontes") }),
            h("label", { class: "field", attrs: { for: "recording-title" } }, h("span", { text: tr("Título da gravação") }), this.title),
            this.sourceRows,
            h("div", { class: "btn-row" }, button(tr("Atualizar dispositivos"), () => void this.refreshDevices(true)), this.previewButton),
          ),
          this.readiness.element,
        ),
        h(
          "div",
          { class: "card stack monitor" },
          this.lostSlot,
          h("div", { class: "monitor-state" }, this.status, this.transport),
          this.blocked,
          this.exportProgress,
          this.meters,
          this.waveform,
        ),
      ),
    );
    this.element.querySelector("h1")?.setAttribute("id", "recording-heading");
    bridge.on<RecordingView>("recording.state", (view) => this.render(view));
    bridge.on<{ origin: string }>("recording.requestStart", () => void this.start());
    bridge.on("voice.state", () => this.readiness.refresh());
    this.readiness.refresh();
    this.renderSources();
    void this.refreshDevices(false);
    void bridge.call<RecordingView>("recording.state").then((view) => this.render(view));
  }

  get navState(): string {
    if (!this.view) return "";
    return this.view.capturing ? `● ${this.view.status.split(" · ").slice(0, 2).join(" · ")}` : "";
  }

  onChange: () => void = () => {};

  private async refreshDevices(announce: boolean): Promise<void> {
    try {
      const { devices } = await this.bridge.call<{ devices: Device[] }>("recording.devices");
      this.devices = devices;
      if (announce) this.messages.say(tr("Dispositivos atualizados."));
    } catch (error) {
      this.devices = this.devices ?? [];
      this.messages.fail(tr("Não foi possível listar os dispositivos"), tr("Confira as fontes e tente de novo."), { error });
    }
    this.renderSources();
  }

  private renderSources(): void {
    const capturing = this.view?.capturing ?? false;
    replace(
      this.sourceRows,
      ...TRACKS.map((track) => {
        const choice = this.choices[track];
        const id = `source-${track}`;
        const check = h("input", { attrs: { type: "checkbox", id: `${id}-on` } });
        check.checked = choice.enabled;
        check.disabled = capturing;
        check.addEventListener("change", () => {
          choice.enabled = check.checked;
          this.renderSources();
        });
        const select = h("select", { attrs: { id, "aria-describedby": `${id}-hint` } });
        const options = endpointOptions(this.devices ?? [], track, choice.selection);
        options.forEach((option, index) => {
          const node = h("option", { text: option.label, attrs: { value: index } });
          node.selected = sameSelection(choice.selection, option.selection);
          select.append(node);
        });
        select.disabled = capturing || !choice.enabled || this.devices === null;
        select.addEventListener("change", () => {
          const option = options[Number(select.value)];
          if (option) choice.selection = option.selection;
          this.renderSources();
        });
        const [hint, warning] = endpointHint(this.devices, track, choice);
        return h(
          "div",
          { class: "field" },
          h("label", { class: "switch", attrs: { for: `${id}-on` } }, check, h("span", { text: track === "microphone" ? tr("Microfone") : tr("Áudio do sistema") })),
          select,
          h("small", { class: warning ? "text-warning" : "", text: hint, attrs: { id: `${id}-hint` } }),
        );
      }),
    );
  }

  private sources(): Record<Track, SourceChoice> {
    return { microphone: { ...this.choices.microphone }, system: { ...this.choices.system } };
  }

  async start(consent = false): Promise<void> {
    if (this.busy) return;
    if (!this.choices.microphone.enabled && !this.choices.system.enabled) {
      this.messages.fail(tr("Revise as configurações da gravação"), tr("Ative pelo menos uma fonte de áudio."), { tone: "warning" });
      return;
    }
    this.busy = true;
    this.renderTransport();
    try {
      const result = await this.bridge.call<StartResult>("recording.start", { title: this.title.value, sources: this.sources(), consent });
      if (result.status === "consent") {
        this.busy = false;
        const accepted = await confirmDialog({ title: tr("Aviso de gravação"), body: result.notice, confirm: tr("Confirmar e iniciar"), selectable: true });
        if (accepted) return void (await this.start(true));
        this.messages.say(tr("Gravação cancelada no aviso de consentimento."));
        return;
      }
      this.messages.clear();
      this.title.value = "";
    } catch (error) {
      this.messages.fail(tr("Gravação não iniciada"), "", { tone: "warning", error });
    } finally {
      this.busy = false;
      this.renderTransport();
    }
  }

  private async command(method: string, failure: string): Promise<void> {
    try {
      await this.bridge.call(method);
    } catch (error) {
      this.messages.fail(failure, "", { error });
    }
  }

  private async interruptAndRecord(): Promise<void> {
    const view = this.view;
    if (!view?.canInterrupt) return;
    const accepted = await confirmDialog({
      title: tr("Interromper e gravar"),
      body: `${tr("Interromper {job} e começar a gravar?", { job: view.processingJob })}\n\n${tr(
        "Essa tarefa para antes de terminar. Execute-a de novo depois. A gravação começa assim que ela parar.",
      )}`,
      confirm: tr("Interromper e gravar"),
    });
    if (!accepted) return;
    this.startAfterInterrupt = true;
    this.messages.say(tr("Interrompendo {job}; a gravação começa em seguida.", { job: view.processingJob }));
    try {
      await this.bridge.call("recording.cancelProcessing");
    } catch (error) {
      this.startAfterInterrupt = false;
      this.messages.fail(tr("Não foi possível interromper o processamento"), tr("A gravação não começou. Tente de novo."), { error });
    }
  }

  private async preview(): Promise<void> {
    this.previewRunning = true;
    this.previewButton.disabled = true;
    try {
      const { errors } = await this.bridge.call<{ errors: string[] }>("recording.preview", { sources: this.sources() });
      if (errors.length) this.messages.fail(tr("Teste de fontes com falha"), errors.join(", "), { tone: "warning" });
      else this.messages.say(tr("Teste de fontes concluído."));
    } catch (error) {
      this.messages.fail(tr("Não foi possível testar as fontes"), "", { error });
    } finally {
      this.previewRunning = false;
      this.previewButton.disabled = Boolean(this.view?.capturing);
    }
  }

  private render(view: RecordingView): void {
    const wasCapturing = this.view?.capturing;
    // A job starting or ending, or a capture changing state, changes what is ready.
    if (this.view?.processing !== view.processing || this.view?.state !== view.state) this.readiness.refresh();
    this.view = view;
    this.status.className = `status-chip ${toneClass(view.tone)}`;
    this.status.textContent = view.status;
    if (wasCapturing !== view.capturing) this.renderSources();
    this.previewButton.disabled = view.capturing || view.previewing || this.previewRunning;
    this.renderTransport();
    if (this.startAfterInterrupt && view.canStart) {
      this.startAfterInterrupt = false;
      void this.start();
    }
    replace(
      this.lostSlot,
      ...view.lost.map((lost) =>
        h(
          "div",
          { class: "infobar warning", attrs: { role: "alert" } },
          h("span", { class: "glyph", text: "⚠", attrs: { "aria-hidden": "true" } }),
          h(
            "div",
            { class: "stack-sm" },
            h("span", { class: "title-line", text: tr("Sem áudio de {source}", { source: lost.label.toLowerCase() }) }),
            h("p", { text: tr("A gravação continua e retoma sozinha quando o áudio voltar.") }),
          ),
          button(tr("Trocar dispositivo"), () => {
            document.getElementById(`source-${lost.track}`)?.focus();
            this.messages.say(tr("Trocar o dispositivo vale a partir da próxima gravação. Para continuar agora, reconecte o dispositivo."));
          }),
        ),
      ),
    );
    this.exportProgress.hidden = !view.export;
    if (view.export) {
      if (view.export.percent === null) this.exportProgress.removeAttribute("value");
      else this.exportProgress.value = view.export.percent;
    }
    replace(this.meters, ...TRACKS.map((track) => this.meter(view.tracks[track])));
    this.waveform.hidden = !(view.capturing || view.previewing);
    if (!this.waveform.hidden) this.drawWaveform(view);
    this.onChange();
  }

  private renderTransport(): void {
    const view = this.view;
    const buttons: HTMLButtonElement[] = [];
    if (view?.canStop) {
      const stop = button(tr("Parar e salvar"), () => void this.command("recording.stop", tr("Não foi possível parar a gravação")), "primary");
      buttons.push(stop);
      if (view.canPause) {
        buttons.push(
          button(view.paused ? tr("Retomar") : tr("Pausar"), () =>
            void this.command(view.paused ? "recording.resume" : "recording.pause", tr("Não foi possível pausar a gravação")),
          ),
        );
      }
    } else {
      const start = button(tr("Iniciar gravação"), () => void this.start(), "primary");
      if (view?.hotkey) start.append(h("span", { class: "kbd", text: view.hotkey }));
      start.disabled = this.busy || !view?.canStart;
      buttons.push(start);
      if (view?.canInterrupt) buttons.push(button(tr("Interromper e gravar"), () => void this.interruptAndRecord()));
    }
    replace(this.transport, ...buttons);
    const reason = view && !view.capturing && !view.canStart ? view.hold || (view.startBlocker ? tr("Aguardando {job}.", { job: view.startBlocker }) : "") : "";
    this.blocked.textContent = reason;
    this.blocked.hidden = !reason;
  }

  private meter(track: TrackView): HTMLElement {
    const fill = h("div", { class: "meter-fill" });
    fill.style.width = `${Math.round(track.level * 100)}%`;
    return h(
      "div",
      { class: "meter" },
      h("div", { class: "btn-row meter-head" }, h("span", { text: track.label }), h("span", { class: "caption", text: track.reading })),
      h("div", { class: "meter-track", attrs: { role: "meter", "aria-label": track.label, "aria-valuemin": 0, "aria-valuemax": 100, "aria-valuenow": Math.round(track.level * 100) } }, fill),
      track.health ? h("small", { class: `caption ${toneText(track.tone)}`, text: track.health }) : null,
    );
  }

  private drawWaveform(view: RecordingView): void {
    const canvas = this.waveform;
    const width = canvas.clientWidth || 400;
    const ratio = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(120 * ratio);
    const context = canvas.getContext("2d");
    if (!context) return;
    const styles = getComputedStyle(document.documentElement);
    const colors = {
      microphone: styles.getPropertyValue(view.paused ? "--color-chart-neutral" : "--color-chart-primary"),
      system: styles.getPropertyValue(view.paused ? "--color-chart-neutral" : "--color-chart-secondary"),
    };
    context.clearRect(0, 0, canvas.width, canvas.height);
    const lane = canvas.height / 2;
    TRACKS.forEach((track, index) => {
      const points = view.tracks[track].waveform;
      if (!points.length) return;
      const middle = lane * index + lane / 2;
      const step = canvas.width / Math.max(points.length, 360);
      context.fillStyle = colors[track].trim();
      points.forEach((value, i) => {
        const height = Math.max(1 * ratio, Math.min(1, value) * (lane - 8 * ratio));
        context.fillRect(i * step, middle - height / 2, Math.max(1, step - ratio), height);
      });
    });
  }
}
