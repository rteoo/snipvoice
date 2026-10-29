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
  download: { done: number; total: number } | null;
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
