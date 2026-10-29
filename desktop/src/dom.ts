// Minimal DOM construction. Text is always set as textContent; no view builds
// markup from strings.
import type { SessionState } from "./types";

type Child = Node | string | null | undefined | false;
type Props = {
  class?: string;
  text?: string;
  attrs?: Record<string, string | number | boolean | undefined>;
  on?: { [K in keyof HTMLElementEventMap]?: (event: HTMLElementEventMap[K]) => void };
};

export function h<K extends keyof HTMLElementTagNameMap>(tag: K, props: Props = {}, ...children: Child[]): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (props.class) node.className = props.class;
  if (props.text !== undefined) node.textContent = props.text;
  for (const [name, value] of Object.entries(props.attrs ?? {})) {
    if (value === undefined || value === false) continue;
    node.setAttribute(name, value === true ? "" : String(value));
  }
  for (const [name, listener] of Object.entries(props.on ?? {})) {
    node.addEventListener(name, listener as EventListener);
  }
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child);
  }
  return node;
}

export function replace(parent: Element, ...children: Child[]): void {
  parent.replaceChildren(...children.filter((child): child is Node | string => Boolean(child)));
}

export function button(text: string, onClick: () => void, variant: "primary" | "neutral" | "quiet" | "danger" = "neutral"): HTMLButtonElement {
  return h("button", { class: variant === "neutral" ? "btn" : `btn ${variant}`, text, attrs: { type: "button" }, on: { click: () => onClick() } });
}

const SVG_NS = "http://www.w3.org/2000/svg";

/** Stroke icons on the 20px grid (DS asset geometry, 1.5 stroke). */
export function icon(paths: readonly string[]): SVGSVGElement {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 20 20");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.5");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  for (const d of paths) {
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", d);
    svg.append(path);
  }
  return svg;
}

export const ICONS = {
  record: ["M10 3.5a6.5 6.5 0 1 0 0 13a6.5 6.5 0 1 0 0-13Z", "M10 7.5a2.5 2.5 0 1 0 0 5a2.5 2.5 0 1 0 0-5Z"],
  library: ["M4 3.5v13", "M7.5 3.5v13", "M11 4l4 12.5", "M3 16.5h14"],
  microphone: ["M10 2.5a2.5 2.5 0 0 0-2.5 2.5v5a2.5 2.5 0 0 0 5 0V5A2.5 2.5 0 0 0 10 2.5Z", "M5 9.5a5 5 0 0 0 10 0", "M10 14.5v3"],
  settings: [
    "M10 7.25a2.75 2.75 0 1 0 0 5.5a2.75 2.75 0 1 0 0-5.5Z",
    "M10 2.5v2M10 15.5v2M2.5 10h2M15.5 10h2M4.7 4.7l1.4 1.4M13.9 13.9l1.4 1.4M4.7 15.3l1.4-1.4M13.9 6.1l1.4-1.4",
  ],
  search: ["M8.5 3.5a5 5 0 1 0 0 10a5 5 0 1 0 0-10Z", "M12.25 12.25l4.25 4.25"],
} as const;

const CIRCLE = "M10 2.75a7.25 7.25 0 1 0 0 14.5a7.25 7.25 0 1 0 0-14.5Z";

/** One glyph per recording state, so a list row reads at a glance. */
export const STATE_ICONS: Record<SessionState, readonly string[]> = {
  completed: [CIRCLE, "M6.75 10.25l2.25 2.25l4.25-4.5"],
  partial: [CIRCLE, "M10 2.75v14.5"],
  interrupted: [CIRCLE, "M10 6.25v4.5", "M10 13.75h.01"],
  failed: [CIRCLE, "M7.5 7.5l5 5", "M12.5 7.5l-5 5"],
  cancelled: [CIRCLE, "M4.9 15.1L15.1 4.9"],
  recording: [CIRCLE, "M10 7a3 3 0 1 0 0 6a3 3 0 1 0 0-6Z"],
  processing: [CIRCLE, "M10 6v4.25l2.75 1.75"],
  unknown: [CIRCLE, "M8 8.25a2 2 0 1 1 2.6 1.9c-.35.12-.6.45-.6.85v.5", "M10 13.75h.01"],
};

/** The state glyph with its written label for screen readers and a hover tooltip. */
export function stateIcon(state: SessionState, label: string): HTMLElement {
  return h("span", { class: "state-icon", attrs: { "data-state": state, title: label } }, icon(STATE_ICONS[state] ?? STATE_ICONS.unknown), h("span", { class: "sr-only", text: label }));
}
