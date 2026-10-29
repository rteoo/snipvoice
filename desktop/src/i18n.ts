// Same contract as source/i18n.py: pt-BR literals are the source text and the
// catalog keys. The engine sends the active catalog at startup, so Python's
// i18n_en_us.py stays the single translation file.
let catalog: Record<string, string> = {};
let language = "pt-BR";

export function setCatalog(code: string, entries: Record<string, string>): void {
  language = code;
  catalog = entries;
  document.documentElement.lang = code;
}

export const currentLanguage = (): string => language;

export function tr(text: string, values?: Record<string, string | number>): string {
  let out = catalog[text] ?? text;
  if (values) out = out.replace(/\{(\w+)\}/g, (match, key: string) => (key in values ? String(values[key]) : match));
  return out;
}
