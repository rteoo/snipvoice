// Engine payloads (source/manager_bridge.py). Text fields arrive translated.
export type Tone = "live" | "ok" | "busy" | "warning" | "danger" | "muted" | "default";
export type Appearance = "dark" | "light" | "system";
export type Destination = "recording" | "library" | "voice" | "settings";

export interface Hello {
  protocol: number;
  displayName: string;
  version: string;
  language: string;
  languages: Record<string, string>;
  catalog: Record<string, string>;
  appearance: Appearance;
  startup: { ready: boolean; message: string };
  enginePid: number;
}

export interface TrackView {
  label: string;
  level: number;
  reading: string;
  health: string;
  tone: Tone;
  waveform: number[];
}

export interface RecordingView {
  state: string;
  status: string;
  tone: Tone;
  elapsed: string;
  capturing: boolean;
  previewing: boolean;
  processing: boolean;
  processingJob: string;
  processingSession: string | null;
  postprocess: string;
  canStart: boolean;
  startBlocker: string;
  hold: string;
  canInterrupt: boolean;
  canStop: boolean;
  canPause: boolean;
  paused: boolean;
  error: string;
  tracks: { microphone: TrackView; system: TrackView };
  lost: { track: "microphone" | "system"; label: string; silentFor: number }[];
  export: { percent: number | null; label: string; stage: string; sessionId: string | null } | null;
  hotkey: string;
}

export interface Device {
  id: string;
  name: string;
  kind: "microphone" | "system";
  default: boolean | null;
  communicationsDefault: boolean | null;
}

export type EndpointSelection =
  | { mode: "default"; default_role: "multimedia" | "communications" }
  | { mode: "manual"; endpoint_id: string };

export interface SourceChoice {
  enabled: boolean;
  selection: EndpointSelection;
}

export type StartResult = { status: "started" } | { status: "consent"; notice: string };

export interface VoiceView {
  enabled: boolean;
  state: string | null;
  status: string;
  detail: string;
  tone: Tone;
  download: { done: number; total: number; profile: string | null } | null;
}

export type VoiceToggleResult =
  | { status: "accepted" }
  | { status: "blocked" }
  | { status: "confirm"; title: string; message: string };

export interface HistoryRow {
  id: string;
  date: string;
  status: string;
  tone: Tone;
  preview: string;
  retryable: boolean;
  copyable: boolean;
}

export interface LibraryItem {
  id: string;
  title: string;
  status: string;
  tone: Tone;
  date: string;
  duration: string;
}

export interface LibraryPage {
  items: LibraryItem[];
  nextCursor: unknown;
}

export interface SessionDetail {
  id: string;
  title: string;
  status: string;
  tone: Tone;
  date: string;
  duration: string;
  revision: string | number | null;
}

export interface Transcript {
  text: string;
  truncated: boolean;
  empty: boolean;
}

export interface SummaryProfile {
  id: string;
  name: string;
  description: string;
  disabled: boolean;
  custom: boolean;
  baseName: string;
  instructions: string;
}

export interface SummaryView {
  text: string;
  name: string;
  profiles: SummaryProfile[];
  profileId: string;
  modelReady: boolean;
  canSaveAnswers: boolean;
  hint: string;
}

export interface ChatAnswer {
  answerId: string;
  answer: string;
  sources: number;
  uncertainty: string;
  uncertaintyTone: Tone;
}

export interface FilesView {
  final: { format: string; size: string } | null;
  canPrepare: boolean;
}

export type SaveResult = { saved: false } | { saved: true; name: string };

export type TrashPreview =
  | { eligible: false; reasons: string; details: string }
  | { eligible: true; token: string; question: string; details: string };

export interface TrashItem {
  id: string;
  name: string;
  moved: string;
  expires: string;
  size: string;
  expired: boolean;
}

export interface TrashList {
  items: TrashItem[];
  summary: string;
  expired: number;
}

export interface Choice {
  id: string;
  label: string;
}

export interface RecordingValues {
  profile: string;
  language: string;
  hotkey: string;
  summaryModel: string;
  destination: string;
  voiceBoost: boolean;
}

export interface RecordingSettingsView {
  profiles: Choice[];
  languages: Record<string, Choice[]>;
  summaryModels: (Choice & { installed: boolean })[];
  values: RecordingValues;
  destinationLabel: string;
  microphoneEnabled: boolean;
}

export interface PrivacyView {
  generation: number;
  noticeEnabled: boolean;
  noticeLanguage: "pt-BR" | "en-US";
  qaMode: "memory_only" | "explicit_save";
  autoTrash: boolean;
  autoTrashDays: string;
  trashDays: string;
}

export interface VoiceModelRow {
  id: string;
  name: string;
  purpose: string;
  size: string;
  license: string;
  installed: boolean;
  downloading: boolean;
}

export interface SummaryModelRow {
  id: string;
  name: string;
  label: string;
  size: string;
  license: string;
  installed: boolean;
  downloading: boolean;
}

export interface SummaryProgress {
  id: string;
  done: number;
  total: number;
  status: string;
}

export interface ModelsView {
  voice: VoiceModelRow[];
  summary: SummaryModelRow[];
  summaryProgress: SummaryProgress | null;
}

export type Confirmable = { status: "confirm"; title: string; message: string } | { status: "started" | "removed"; message?: string };

export interface LocationView {
  path: string;
  locked: boolean;
  atDefault: boolean;
}

export type MovePlan = { target: null } | { target: string; title: string; question: string };

export interface DictationValues {
  profile: string;
  language: string;
  hotkey: string;
  commandHotkey: string;
  activationMode: "hold" | "toggle";
}

export interface DictationSettingsView {
  profiles: (Choice & { installed: boolean })[];
  languages: Record<string, Choice[]>;
  values: DictationValues;
  commands: number;
  corrections: number;
}

export interface VoiceCommand {
  phrase: string;
  text: string;
}

export interface Correction {
  source: string;
  replacement: string;
}

export interface SearchState {
  enabled: boolean;
  installed: boolean;
  model: { name: string; size: string; license: string };
  semantic: string;
  status: string;
  index: string;
  download: { done: number; total: number | null } | null;
  repair: { done: number; total: number | null } | null;
}

export interface VersionItem {
  id: string;
  label: string;
  answer: boolean;
  reviewed: boolean;
}

export interface VersionView {
  id: string;
  text: string;
  sections: { name: string; text: string }[];
  reviewable: boolean;
  generation: number;
}
