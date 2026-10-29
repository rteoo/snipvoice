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

class Cancelled extends Error {}

const SUMMARY = [
  "Visão geral:",
  "A equipe revisou o cronograma da entrega e fechou a revisão para sexta.",
  "Decisões:",
  "• Manter a data de entrega.",
  "• Revisar a proposta antes do envio.",
  "Próximos passos:",
  "• Ana envia o resumo ao time até amanhã.",
].join("\n");
const PROFILES = [
  { id: "meeting_notes", name: "Notas de reunião", description: "Visão geral, decisões e próximos passos.", disabled: false, custom: false, baseName: "Notas de reunião", instructions: "" },
  { id: "interview", name: "Entrevista", description: "Perguntas, respostas e sinais relevantes.", disabled: false, custom: false, baseName: "Entrevista", instructions: "" },
  { id: "one_on_one", name: "1:1", description: "Acompanhamento, bloqueios e combinados.", disabled: false, custom: false, baseName: "1:1", instructions: "" },
];

export class MockTransport implements Transport {
  private receive: (line: string) => void = () => {};
  private readonly summaries = new Map<string, string>([["s1", SUMMARY]]);
  private readonly trashed: { id: string; name: string; moved: string; expires: string; size: string; expired: boolean }[] = [
    { id: "old1", name: "…a1b2c3d4", moved: "02/10 09:00", expires: "Expirada", size: "18 MB", expired: true },
  ];
  private jobs = new Map<string, () => void>();
  private semantic = false;
  private playing: { id: string; start: number; at: number } | null = null;
  private playbackGeneration = 1;
  private playTimer: number | undefined;
  private dictation = { profile: "balanced", language: "pt-BR", hotkey: "ctrl+alt+space", commandHotkey: "ctrl+alt+shift+space", activationMode: "hold" };
  private commands = [{ phrase: "meu email", text: "nome@example.com" }, { phrase: "assinatura", text: "Atenciosamente,\nEquipe" }];
  private corrections = [{ source: "snip voice", replacement: "SnipVoice" }];
  private recordingSettings = {
    profiles: [
      { id: "dictation", label: "Igual ao ditado" },
      { id: "balanced", label: "Equilibrado · Parakeet" },
      { id: "compact", label: "Preciso · Qwen 0.6B" },
    ],
    languages: {
      dictation: [{ id: "auto", label: "Automático" }, { id: "pt-BR", label: "Português (Brasil)" }, { id: "en-US", label: "Inglês (Estados Unidos)" }],
      balanced: [{ id: "auto", label: "Automático" }, { id: "pt-BR", label: "Português (Brasil)" }, { id: "en-US", label: "Inglês (Estados Unidos)" }],
      compact: [{ id: "auto", label: "Automático" }],
    } as Record<string, { id: string; label: string }[]>,
    summaryModels: [{ id: "gemma", label: "Equilibrado", installed: false }],
    values: { profile: "dictation", language: "pt-BR", hotkey: "", summaryModel: "gemma", destination: "", voiceBoost: true },
    destinationLabel: "Pasta local padrão (recordings)",
    microphoneEnabled: true,
  };
  private privacy = { generation: 3, noticeEnabled: true, noticeLanguage: "pt-BR", qaMode: "explicit_save", autoTrash: false, autoTrashDays: "90", trashDays: "30" };
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
      Promise.resolve()
        .then(() => this.handle(message.method, message.params))
        .then(
          (result) => this.reply(message.id, result),
          (error: unknown) => {
            const code = error instanceof Cancelled ? -32001 : -32000;
            const text = error instanceof Error ? error.message : String(error);
            this.receive(JSON.stringify({ jsonrpc: "2.0", id: message.id, error: { code, message: text } }));
          },
        );
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
          enginePid: 0,
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
        return { ...item, revision: item.status === "Falhou" ? null : "r1", durationSeconds: 720, playable: item.status !== "Falhou" };
      }
      case "library.transcript":
        if (params.revision === null) return { text: "", truncated: false, empty: true };
        return {
          text:
            params.style === "timestamped"
              ? "00:00:04 Microfone: Bom dia a todos, vamos começar.\n00:00:11 Sistema: Primeiro item: o cronograma da entrega.\n00:01:02 Microfone: Fechamos a revisão na sexta."
              : "Bom dia a todos, vamos começar. Primeiro item: o cronograma da entrega.\n\nFechamos a revisão na sexta.",
          truncated: false,
          empty: false,
        };
      case "library.rename":
        return { title: params.title };
      case "library.summary": {
        const text = this.summaries.get(String(params.id)) ?? "";
        return { text, name: text ? "Notas de reunião" : "", profiles: PROFILES, profileId: "meeting_notes", modelReady: true, canSaveAnswers: true, hint: text ? "Resumo salvo. Escolha um formato para gerar outra versão." : "O resumo será gerado automaticamente com os modelos instalados." };
      }
      case "library.generateSummary":
        return this.job("summary", 1800, () => {
          this.summaries.set(String(params.id), SUMMARY);
          return this.handle("library.summary", params);
        });
      case "library.ask":
        return this.job("ask", 1200, () => ({ answerId: `a${Date.now()}`, answer: `Sobre "${String(params.question)}": a equipe decidiu manter a data e revisar a proposta antes do envio.`, sources: 2, uncertainty: "Incerteza baixa", uncertaintyTone: "muted" }));
      case "library.saveAnswer":
        return { saved: true };
      case "library.cancelJob": {
        const cancel = this.jobs.get(String(params.kind));
        cancel?.();
        return { cancelled: Boolean(cancel) };
      }
      case "library.files":
        return params.id === "s3" ? { final: null, canPrepare: true } : { final: { format: "MP3", size: "24,1 MB" }, canPrepare: false };
      case "library.exportText":
      case "library.exportAudio":
        return { saved: true, name: params.format === "text" ? "transcricao.txt" : params.format === "markdown" ? "transcricao.md" : "gravacao.mp3" };
      case "library.prepareAudio":
        return { accepted: true };
      case "library.importAudio":
        return { id: null };
      case "library.trashPreview":
        return {
          eligible: true,
          token: "t1",
          question: [
            `Mover “${SESSIONS.find((item) => item.id === params.id)?.title ?? ""}” para a lixeira?`,
            "",
            "Duração: 00:12:00 · Tamanho: 24,1 MB",
            "Você poderá restaurar esta gravação por 30 dias. Cópias exportadas ficam preservadas.",
          ].join("\n"),
          details: ["Operação: lixeira · elegível", "Inventário: 3 alvo(s) · 25270000 bytes"].join("\n"),
        };
      case "library.trash":
      case "library.restore":
        return { id: params.id ?? "s1" };
      case "trash.list": {
        const expired = this.trashed.filter((item) => item.expired).length;
        return { items: this.trashed, summary: this.trashed.length ? `${this.trashed.length} gravação na lixeira · ${expired} expirada` : "A lixeira está vazia", expired };
      }
      case "trash.purge":
      case "trash.emptyExpired":
        this.trashed.splice(0, this.trashed.length);
        return { emptied: true };
      case "settings.recording":
        return this.recordingSettings;
      case "settings.saveRecording":
        this.recordingSettings = { ...this.recordingSettings, values: params.values as typeof this.recordingSettings.values };
        return this.recordingSettings;
      case "settings.chooseDestination":
        return { path: "", label: "" };
      case "settings.privacy":
        return this.privacy;
      case "settings.savePrivacy":
        this.privacy = { ...(params as unknown as typeof this.privacy), generation: this.privacy.generation + 1 };
        return this.privacy;
      case "settings.models":
        return {
          voice: [
            { id: "balanced", name: "nvidia/parakeet-tdt-0.6b-v3", purpose: "Equilibrado", size: "640 MB", license: "CC-BY-4.0", installed: true, downloading: false },
            { id: "accuracy", name: "Qwen/Qwen3-ASR-1.7B", purpose: "Muito preciso", size: "2.10 GB", license: "Apache-2.0", installed: false, downloading: false },
          ],
          summary: [{ id: "gemma", name: "Gemma 4 E4B", label: "Equilibrado", size: "4.20 GB", license: "Gemma", installed: false, downloading: false }],
          summaryProgress: null,
        };
      case "settings.downloadVoiceModel":
      case "settings.downloadSummaryModel":
      case "settings.deleteSummaryModel":
        if (params.confirmed !== true) return { status: "confirm", title: "Baixar modelo local", message: "Baixar este modelo?\nLicença: Apache-2.0" };
        return { status: "started" };
      case "settings.cancelVoiceModel":
      case "settings.cancelSummaryModel":
        return { cancelled: true };
      case "settings.locations":
        return {
          data: { path: "C:\\Users\\voce\\.snipvoice", locked: false, atDefault: true },
          models: { path: "C:\\Users\\voce\\.snipvoice\\models", locked: false, atDefault: true },
        };
      case "settings.planMove":
        return { target: null };
      case "settings.move":
        return { moving: true };
      case "dictation.settings":
        return {
          profiles: [{ id: "balanced", label: "Equilibrado · Parakeet", installed: true }, { id: "compact", label: "Preciso · Qwen 0.6B", installed: false }],
          languages: {
            balanced: [{ id: "auto", label: "Automático" }, { id: "pt-BR", label: "Português (Brasil)" }],
            compact: [{ id: "auto", label: "Automático" }],
          },
          values: { ...this.dictation },
          commands: this.commands.length,
          corrections: this.corrections.length,
        };
      case "dictation.save": {
        const values = params.values as typeof this.dictation;
        if (values.profile === "compact" && params.confirmed !== true) return { status: "confirm", title: "Baixar modelo de voz", message: "Isso vai baixar 1.20 GB (Apache-2.0)." };
        this.dictation = { ...values };
        return { status: "saved", settings: this.handle("dictation.settings", {}) };
      }
      case "dictation.beginShortcutEdit":
        return { ready: true, message: "Atalhos globais pausados. Pressione o novo atalho." };
      case "dictation.endShortcutEdit":
        return { ended: true };
      case "dictation.commands":
        return { commands: this.commands };
      case "dictation.saveCommands":
        this.commands = params.commands as typeof this.commands;
        return { commands: this.commands };
      case "dictation.reloadCommands":
        return { commands: this.commands.length };
      case "dictation.corrections":
        return { items: this.corrections };
      case "dictation.saveCorrections":
        this.corrections = params.items as typeof this.corrections;
        return { items: this.corrections };
      case "dictation.licenses":
        return { lines: ["transcribe.cpp — MIT", "sounddevice / PortAudio — MIT", "nvidia/parakeet-tdt-0.6b-v3 — CC-BY-4.0."] };
      case "library.versions":
        return { items: [
          { id: "rep2", label: "Relatório · Notas de reunião · Hoje 14:30", answer: false, reviewed: true },
          { id: "qa1", label: "Pergunta · Resposta salva · Hoje 14:20", answer: true, reviewed: false },
          { id: "rep1", label: "Relatório · Entrevista · Ontem 10:00", answer: false, reviewed: false },
        ] };
      case "library.version":
        return { id: params.reportId, text: SUMMARY, sections: [{ name: "summary", text: "A equipe revisou o cronograma." }, { name: "decisions", text: JSON.stringify(["Manter a data"], null, 2) }], reviewable: true, generation: 1 };
      case "library.reviewSection":
        return { id: params.reportId, text: SUMMARY, sections: [{ name: String(params.section), text: String(params.text) }], reviewable: true, generation: 2 };
      case "library.exportReport":
        return { saved: true, name: "relatorio.md" };
      case "library.saveProfile":
        return { profileId: "meu-formato" };
      case "library.setProfileEnabled":
        return { profileId: params.profileId, enabled: params.enabled };
      case "library.deleteProfile":
        return params.confirmed === true ? { status: "removed" } : { status: "confirm", title: "Excluir perfil", message: "Excluir este perfil personalizado?" };
      case "search.state":
        return { enabled: this.semantic, installed: false, model: { name: "EmbeddingGemma 2 (text, BF16)", size: "0.52 GB", license: "Apache-2.0" }, semantic: this.semantic ? "unavailable" : "disabled", status: this.semantic ? "Busca semântica: indisponível" : "Busca por palavras ativa.", index: "Índice de busca: pronto", download: null, repair: null };
      case "search.setSemantic":
        this.semantic = params.enabled === true;
        return this.handle("search.state", {});
      case "search.downloadModel":
        return params.confirmed === true ? { status: "started" } : { status: "confirm", title: "Baixar modelo local", message: "Baixar EmbeddingGemma 2 (0.52 GB)?" };
      case "search.reindex":
        return this.handle("search.state", {});
      case "search.cancel":
      case "search.cancelRepair":
        return { cancelled: true };
      case "search.repair":
        return { status: "started" };
      case "recording.readiness":
        return { rows: [
          { key: "summary", tone: "warning", text: "Sem modelo de resumo instalado.", action: "Ver modelos" },
          { key: "attention", tone: "warning", text: "Gravações com problemas nesta página: 1", action: "Ver" },
        ], attentionId: "s3" };
      case "library.play":
        this.playing = { id: String(params.id), start: Number(params.start), at: Date.now() };
        this.playbackGeneration++;
        window.clearInterval(this.playTimer);
        this.playTimer = window.setInterval(() => this.push("recording.state", this.recordingView()), 200);
        return { accepted: true };
      case "library.stopPlayback":
        this.playing = null;
        this.playbackGeneration++;
        window.clearInterval(this.playTimer);
        this.push("recording.state", this.recordingView());
        return { stopped: true };
      case "dictation.removeModel":
        return params.confirmed === true ? { status: "removed" } : { status: "confirm", title: "Remover modelo", message: "A entrada por voz será desligada e só o modelo deste perfil será apagado." };
      default:
        throw new Error(`unknown method ${method}`);
    }
  }

  private job(kind: string, ms: number, done: () => unknown): Promise<unknown> {
    return new Promise((resolve, reject) => {
      const timer = window.setTimeout(() => {
        this.jobs.delete(kind);
        resolve(done());
      }, ms);
      this.jobs.set(kind, () => {
        window.clearTimeout(timer);
        this.jobs.delete(kind);
        reject(new Cancelled("Resposta cancelada."));
      });
    });
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
      playback: { active: this.playing !== null, sessionId: this.playing?.id ?? null, position: this.playing ? this.playing.start + (Date.now() - this.playing.at) / 1000 : 0, failed: false, generation: this.playbackGeneration },
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
