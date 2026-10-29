// JSON-RPC 2.0 over newline-delimited JSON. The engine answers requests and
// pushes notifications (state snapshots); the page never polls.
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";

export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };

export interface Transport {
  /** Attach the receiver; resolves once delivery is live. */
  start(receive: (line: string) => void): Promise<void>;
  send(line: string): Promise<void>;
}

export class RpcError extends Error {
  constructor(
    readonly code: number,
    message: string,
    readonly data?: Json,
  ) {
    super(message);
    this.name = "RpcError";
  }
}

type Pending = { resolve: (value: unknown) => void; reject: (error: Error) => void };
type Handler = (params: never) => void;

export class Bridge {
  private nextId = 1;
  private readonly pending = new Map<number, Pending>();
  private readonly handlers = new Map<string, Set<Handler>>();
  // Engine notifications wait here until every page has subscribed, so a
  // request sent while the window starts (tray Start) is never dropped.
  private held: { method: string; params: unknown }[] | null = [];

  constructor(private readonly transport: Transport) {}

  release(): void {
    const held = this.held ?? [];
    this.held = null;
    for (const { method, params } of held) this.dispatch(method, params);
  }

  async start(): Promise<void> {
    await this.transport.start((line) => this.receive(line));
  }

  call<T>(method: string, params: Record<string, unknown> = {}): Promise<T> {
    const id = this.nextId++;
    const line = JSON.stringify({ jsonrpc: "2.0", id, method, params });
    return new Promise<T>((resolve, reject) => {
      this.pending.set(id, { resolve: resolve as (value: unknown) => void, reject });
      this.transport.send(line).catch((error: unknown) => {
        this.pending.delete(id);
        reject(error instanceof Error ? error : new Error(String(error)));
      });
    });
  }

  on<P>(method: string, handler: (params: P) => void): () => void {
    let set = this.handlers.get(method);
    if (!set) this.handlers.set(method, (set = new Set()));
    set.add(handler as Handler);
    return () => set.delete(handler as Handler);
  }

  private receive(line: string): void {
    let message: { id?: number; method?: string; params?: unknown; result?: unknown; error?: { code: number; message: string; data?: Json } };
    try {
      message = JSON.parse(line);
    } catch {
      console.error("bridge: dropped a non-JSON line", line.slice(0, 200));
      return;
    }
    if (typeof message.id === "number" && !message.method) {
      const waiter = this.pending.get(message.id);
      if (!waiter) return;
      this.pending.delete(message.id);
      if (message.error) waiter.reject(new RpcError(message.error.code, message.error.message, message.error.data));
      else waiter.resolve(message.result);
      return;
    }
    if (typeof message.method === "string") {
      if (this.held) this.held.push({ method: message.method, params: message.params ?? {} });
      else this.dispatch(message.method, message.params ?? {});
    }
  }

  private dispatch(method: string, params: unknown): void {
    for (const handler of this.handlers.get(method) ?? []) {
      try {
        (handler as (params: unknown) => void)(params);
      } catch (error) {
        console.error(`bridge: handler for ${method} failed`, error);
      }
    }
  }
}

export class TauriTransport implements Transport {
  async start(receive: (line: string) => void): Promise<void> {
    await listen<string>("bridge://message", (event) => receive(event.payload));
    for (const line of await invoke<string[]>("bridge_ready")) receive(line);
  }

  send(line: string): Promise<void> {
    return invoke("bridge_send", { line });
  }
}

export const inTauri = (): boolean => "__TAURI_INTERNALS__" in window;

/** The engine shows save/open dialogs itself; grant it the foreground first or they open behind this window. */
export async function allowEngineForeground(pid: number): Promise<void> {
  if (!inTauri() || !pid) return;
  try {
    await invoke<boolean>("allow_engine_foreground", { pid });
  } catch (error) {
    console.warn("the engine dialog may open behind this window", error);
  }
}
