// A synthetic engine for `npm run dev` in a plain browser. It answers the same
// methods as source/manager_bridge.py with made-up data and stores nothing.
import type { Transport } from "./bridge";

type Params = Record<string, unknown>;

const DEVICES = [
  { id: "mic-usb", name: "USB Microphone", kind: "microphone", default: true, communicationsDefault: false },
  { id: "mic-headset", name: "Headset Microphone", kind: "microphone", default: false, communicationsDefault: true },
  { id: "out-speakers", name: "Speakers", kind: "system", default: true, communicationsDefault: true },
];

const SESSIONS = Array.from({ length: 7 }, (_, index) => ({
  id: `s${index + 1}`,
  title: ["Weekly planning", "Customer interview — onboarding", "1:1 check-in", "Design review", "Sales call", "Retro", "Team sync"][index] ?? "",
  status: index === 2 ? "Falhou" : index === 4 ? "Parcial" : "Concluída",
  tone: index === 2 ? "danger" : index === 4 ? "warning" : "default",
  date: index === 0 ? "Hoje 14:02" : index === 1 ? "Ontem 09:10" : `${20 - index}/10 10:${10 + index}`,
  duration: `00:${String(12 + index * 7).padStart(2, "0")}:0${index}`,
}));

export class MockTransport implements Transport {
  private receive: (line: string) => void = () => {};
  private recording: "idle" | "recording" | "paused" = "idle";
  private started = 0;
  private voiceEnabled = true;
  private timer: number | undefined;

  async start(receive: (line: string) => void): Promise<void> {
    this.receive = receive;
    this.push("recording.state", this.recordingView());
  }

  async send(line: string): Promise<void> {
    const message = JSON.parse(line) as { id: number; method: string; params: Params };
    window.setTimeout(() => {
      try {
        this.reply(message.id, this.handle(message.method, message.params));
      } catch (error) {
        this.receive(JSON.stringify({ jsonrpc: "2.0", id: message.id, error: { code: -32000, message: String(error) } }));
      }
    }, 60);
  }

  private reply(id: number, result: unknown): void {
    this.receive(JSON.stringify({ jsonrpc: "2.0", id, result }));
  }

  private push(method: string, params: unknown): void {
    this.receive(JSON.stringify({ jsonrpc: "2.0", method, params }));
  }

  private handle(method: string, params: Params): unknown {
    switch (method) {
      case "app.hello":
        return {
          protocol: 1,
          displayName: "SnipVoice v1.3.0 (preview)",
          version: "1.3.0",
          language: "pt-BR",
          languages: { "pt-BR": "Português (Brasil)", "en-US": "English (US)" },
          catalog: {},
          appearance: "dark",
          startup: { ready: true, message: "" },
        };
      case "app.setAppearance":
        return { appearance: params.appearance };
      case "app.setLanguage":
        return { language: "pt-BR", catalog: {} };
      case "recording.state":
        return this.recordingView();
      case "recording.devices":
        return { devices: DEVICES };
      case "recording.start":
        if (params.consent !== true) return { status: "consent", notice: "Esta reunião está sendo gravada localmente pelo SnipVoice. Confirme que todas as pessoas foram informadas e consentiram antes de gravar." };
        this.recording = "recording";
        this.started = Date.now();
        this.timer = window.setInterval(() => this.push("recording.state", this.recordingView()), 200);
        return { status: "started" };
      case "recording.stop":
        this.recording = "idle";
        window.clearInterval(this.timer);
        this.push("recording.state", this.recordingView());
        return { accepted: true };
      case "recording.pause":
      case "recording.resume":
        this.recording = method === "recording.pause" ? "paused" : "recording";
        return { accepted: true };
      case "recording.preview":
        return { errors: [] };
      case "recording.cancelProcessing":
        return { accepted: true };
      case "voice.state":
        return this.voiceView();
      case "voice.setEnabled":
        this.voiceEnabled = params.enabled === true;
        window.setTimeout(() => this.push("voice.state", this.voiceView()), 100);
        return { status: "accepted" };
      case "voice.history":
        return {
          rows: [
            { id: "v1", date: "Hoje 14:31", status: "Concluído", tone: "default", preview: "Enviar o resumo da reunião para o time até amanhã de manhã.", retryable: false, copyable: true },
            { id: "v2", date: "Hoje 11:05", status: "Falhou", tone: "danger", preview: "O modelo não respondeu a tempo.", retryable: true, copyable: false },
            { id: "v3", date: "Ontem 18:40", status: "Concluído", tone: "default", preview: "Lembrete: revisar a proposta.", retryable: false, copyable: true },
          ],
        };
      case "voice.retry":
      case "voice.copy":
        return { accepted: true };
      case "library.list": {
        const query = String(params.query ?? "").toLowerCase();
        return { items: SESSIONS.filter((item) => item.title.toLowerCase().includes(query)), nextCursor: null };
      }
      case "library.get": {
        const item = SESSIONS.find((session) => session.id === params.id);
        if (!item) throw new Error("Gravação não encontrada.");
        return { ...item, revision: item.status === "Falhou" ? null : "r1" };
      }
      case "library.transcript":
        if (params.revision === null) return { text: "", truncated: false, empty: true };
        return {
          text:
            params.style === "timestamped"
              ? "[00:00:04] Bom dia a todos, vamos começar.\n[00:00:11] Primeiro item: o cronograma da entrega.\n[00:01:02] Fechamos a revisão na sexta."
              : "Bom dia a todos, vamos começar. Primeiro item: o cronograma da entrega.\n\nFechamos a revisão na sexta.",
          truncated: false,
          empty: false,
        };
      case "library.rename":
        return { title: params.title };
      default:
        throw new Error(`unknown method ${method}`);
    }
  }

  private recordingView(): unknown {
    const live = this.recording !== "idle";
    const seconds = live ? Math.floor((Date.now() - this.started) / 1000) : 0;
    const elapsed = new Date(seconds * 1000).toISOString().slice(11, 19);
    const wave = (phase: number) => (live ? Array.from({ length: 360 }, (_, i) => Math.abs(Math.sin((i + Date.now() / 80) / (9 + phase))) * 0.8) : []);
    const level = () => (this.recording === "recording" ? 0.45 + Math.random() * 0.4 : 0);
    const word = this.recording === "recording" ? "Gravando" : this.recording === "paused" ? "Pausado" : "Pronto";
    return {
      state: this.recording,
      status: `${word} · ${elapsed}`,
      tone: this.recording === "recording" ? "live" : this.recording === "paused" ? "muted" : "ok",
      elapsed,
      capturing: live,
      previewing: false,
      processing: false,
      processingJob: "",
      processingSession: null,
      postprocess: "",
      canStart: !live,
      startBlocker: "",
      hold: "",
      canInterrupt: false,
      canStop: live,
      canPause: live,
      paused: this.recording === "paused",
      error: "",
      tracks: {
        microphone: { label: "Microfone", level: level(), reading: live ? "-12.4 dBFS" : "sem sinal", health: live ? "Recebendo sinal" : "", tone: "muted", waveform: wave(0) },
        system: { label: "Áudio do sistema", level: level() * 0.6, reading: live ? "-21.0 dBFS" : "sem sinal", health: live ? "Conectado · sem som no momento" : "", tone: "muted", waveform: wave(4) },
      },
      lost: [],
      export: null,
      hotkey: "Ctrl+Alt+R",
    };
  }

  private voiceView(): unknown {
    return {
      enabled: this.voiceEnabled,
      state: "idle",
      status: this.voiceEnabled ? "Ditado pronto" : "Ditado desligado",
      detail: "Equilibrado · Parakeet · auto · Ctrl+Alt+Space · segurar",
      tone: this.voiceEnabled ? "ok" : "muted",
      download: null,
    };
  }
}
