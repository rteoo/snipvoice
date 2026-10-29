import type { Appearance } from "./types";

const query = window.matchMedia("(prefers-color-scheme: light)");
let preference: Appearance = "dark";

function resolve(): void {
  const light = preference === "light" || (preference === "system" && query.matches);
  document.documentElement.dataset.theme = light ? "light" : "dark";
}

query.addEventListener("change", () => {
  if (preference === "system") resolve();
});

/** Dark by default; System follows live OS changes. */
export function applyAppearance(value: Appearance): void {
  preference = value;
  resolve();
}
