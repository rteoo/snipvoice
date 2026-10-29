import "./styles/tokens.css";
import "./styles/app.css";
import { Bridge, inTauri, TauriTransport, type Transport } from "./bridge";
import { h, ICONS, icon, replace } from "./dom";
import { setCatalog, tr } from "./i18n";
import { applyAppearance } from "./theme";
import type { Destination, Hello } from "./types";
import { LibraryView } from "./views/library";
import { RecordingPage } from "./views/recording";
import { SettingsPage } from "./views/settings";
import { VoicePage } from "./views/voice";

const DESTINATION_KEY = "snipvoice.destination";
const DESTINATIONS: Destination[] = ["recording", "library", "voice", "settings"];

async function transport(): Promise<Transport> {
  if (inTauri()) return new TauriTransport();
  // Browser preview only: a synthetic engine, never bundled into the Tauri path's work.
  const { MockTransport } = await import("./mock");
  return new MockTransport();
}

async function showWindow(): Promise<void> {
  if (!inTauri()) return;
  const { getCurrentWindow } = await import("@tauri-apps/api/window");
  const window_ = getCurrentWindow();
  await window_.unminimize();
  await window_.show();
  await window_.setFocus();
}

async function main(): Promise<void> {
  const root = document.getElementById("app");
  if (!root) throw new Error("missing #app");
  const bridge = new Bridge(await transport());
  await bridge.start();
  const hello = await bridge.call<Hello>("app.hello");
  setCatalog(hello.language, hello.catalog);
  applyAppearance(hello.appearance);
  document.title = hello.displayName;

  const recording = new RecordingPage(bridge);
  const library = new LibraryView(bridge);
  const voice = new VoicePage(bridge);
  const settings = new SettingsPage(bridge, hello, (language, catalog) => {
    setCatalog(language, catalog);
    // Every page is rebuilt in the new language, as the Tk manager does.
    window.location.reload();
  });
  const pages: Record<Destination, { element: HTMLElement; navState?: string }> = { recording, library, voice, settings };
  const labels: Record<Destination, string> = {
    recording: tr("Gravação"),
    library: tr("Biblioteca"),
    voice: tr("Ditado"),
    settings: tr("Configurações"),
  };
  const icons = { recording: ICONS.record, library: ICONS.library, voice: ICONS.microphone, settings: ICONS.settings };

  const navStates = {} as Record<Destination, HTMLElement>;
  const navItems = {} as Record<Destination, HTMLButtonElement>;
  for (const destination of DESTINATIONS) {
    navStates[destination] = h("span", { class: "nav-state" });
    navItems[destination] = h(
      "button",
      { class: destination === "settings" ? "nav-item settings" : "nav-item", attrs: { type: "button" }, on: { click: () => go(destination) } },
      icon(icons[destination]),
      h("span", { class: "nav-label", text: labels[destination] }),
      navStates[destination],
    );
  }
  const nav = h(
    "nav",
    { class: "nav", attrs: { "aria-label": tr("Destinos") } },
    h("div", { class: "nav-brand" }, h("img", { attrs: { src: "/snipvoice-icon.svg", alt: "" } }), h("span", { text: `SnipVoice ${hello.version}` })),
    ...DESTINATIONS.map((destination) => navItems[destination]),
    h("p", { class: "nav-footer caption", text: `${tr("100% local")} · ${tr("sem upload automático")}` }),
  );
  const main_ = h("main", { class: "content" });
  replace(root, nav, main_);
  root.removeAttribute("aria-busy");

  let current: Destination = "recording";
  function go(destination: Destination): void {
    current = destination;
    for (const item of DESTINATIONS) {
      if (item === destination) navItems[item].setAttribute("aria-current", "page");
      else navItems[item].removeAttribute("aria-current");
    }
    replace(main_, pages[destination].element);
    try {
      sessionStorage.setItem(DESTINATION_KEY, destination);
    } catch {
      // Preview contexts may block storage; the destination then resets on reload.
    }
  }
  function syncNav(): void {
    for (const destination of DESTINATIONS) navStates[destination].textContent = pages[destination].navState ?? "";
  }
  recording.onChange = syncNav;
  voice.onChange = syncNav;

  bridge.on<{ destination?: Destination }>("window.show", (params) => {
    if (params.destination && DESTINATIONS.includes(params.destination)) go(params.destination);
    void showWindow();
  });
  bridge.on<{ id: string }>("library.open", (params) => {
    go("library");
    void library.open(params.id);
  });

  document.addEventListener("keydown", (event) => {
    if (event.ctrlKey && !event.shiftKey && !event.altKey && /^[1-4]$/.test(event.key)) {
      event.preventDefault();
      go(DESTINATIONS[Number(event.key) - 1] ?? "recording");
    } else if (event.ctrlKey && event.key.toLowerCase() === "f") {
      event.preventDefault();
      go("library");
      library.focusSearch();
    } else if (event.key === "F2" && current === "library" && library.hasSelection()) {
      event.preventDefault();
      library.editTitle();
    } else if (event.key === "F5" && current === "library") {
      event.preventDefault();
      void library.refresh();
    }
  });

  let initial: string | null = null;
  try {
    initial = sessionStorage.getItem(DESTINATION_KEY);
  } catch {
    initial = null;
  }
  go(DESTINATIONS.includes(initial as Destination) ? (initial as Destination) : "recording");
  syncNav();
  bridge.release();
  await showWindow();
}

main().catch((error: unknown) => {
  console.error(error);
  const root = document.getElementById("app");
  if (root) replace(root, h("p", { class: "empty", text: tr("Não foi possível abrir o SnipVoice. Feche esta janela e abra de novo pela bandeja.") }));
  void showWindow();
});
