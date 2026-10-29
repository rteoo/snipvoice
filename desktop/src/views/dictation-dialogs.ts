// Ditado editors as modal dialogs: spoken commands, transcript corrections and
// licenses. Each edits a private draft; nothing is stored until Save.
import type { Bridge } from "../bridge";
import { button, h, replace } from "../dom";
import { tr } from "../i18n";
import type { Correction, VoiceCommand } from "../types";
import { confirmDialog } from "../ui";

function modal(title: string, wide = false): { dialog: HTMLDialogElement; close: () => void } {
  const opener = document.activeElement as HTMLElement | null;
  const dialog = h("dialog", { class: wide ? "editor-dialog" : "", attrs: { "aria-labelledby": "editor-title" } });
  dialog.append(h("h2", { text: title, attrs: { id: "editor-title" } }));
  const close = () => {
    dialog.close();
    dialog.remove();
    opener?.focus();
  };
  document.body.append(dialog);
  return { dialog, close };
}

/** A list of pairs with a form under it: select a row to edit it, or start a new one. */
function pairEditor<T>(options: {
  title: string;
  intro: string;
  columns: [string, string];
  load: () => Promise<T[]>;
  save: (items: T[], baseline: T[]) => Promise<void>;
  read: (item: T) => [string, string];
  make: (first: string, second: string) => T;
  check: (item: T, items: T[], editing: number | null) => string;
  multiline: boolean;
  empty: string;
}): Promise<boolean> {
  return new Promise((resolve) => {
    const { dialog, close } = modal(options.title, true);
    let baseline: T[] = [];
    let items: T[] = [];
    let editing: number | null = null;
    let saved = false;
    const status = h("p", { class: "caption", attrs: { role: "status", "aria-live": "polite" } });
    const tbody = h("tbody");
    const first = h("input", { attrs: { type: "text", id: "editor-first", maxlength: 160 } });
    const second = options.multiline ? h("textarea", { attrs: { id: "editor-second", rows: 4 } }) : h("input", { attrs: { type: "text", id: "editor-second" } });
    const apply = button(tr("Aplicar"), () => commit());
    const remove = button(tr("Remover"), () => void removeSelected(), "quiet");
    const save = button(tr("Salvar"), () => void persist(), "primary");
    const dirty = () => JSON.stringify(items) !== JSON.stringify(baseline);
    const finish = () => {
      close();
      resolve(saved);
    };
    const leave = async () => {
      if (dirty() && !(await confirmDialog({ title: tr("Alterações não salvas"), body: tr("Descartar as alterações deste editor?"), confirm: tr("Descartar"), danger: true }))) return;
      finish();
    };
    const fill = (index: number | null) => {
      editing = index;
      const [one, two] = index === null ? ["", ""] : options.read(items[index] as T);
      first.value = one;
      second.value = two;
      tbody.querySelectorAll("tr[data-index]").forEach((row) => row.setAttribute("aria-selected", String(Number(row.getAttribute("data-index")) === index)));
      remove.disabled = index === null;
    };
    const render = () => {
      replace(
        tbody,
        ...(items.length
          ? items.map((item, index) => {
              const [one, two] = options.read(item);
              const row = h("tr", { attrs: { tabindex: 0, "data-index": index, "aria-selected": String(index === editing) } }, h("td", { text: one, attrs: { title: one } }), h("td", { text: two.split(/\s+/).join(" ").slice(0, 120), attrs: { title: two } }));
              row.addEventListener("click", () => fill(index));
              row.addEventListener("keydown", (event) => {
                if (event.key === "Enter") fill(index);
              });
              return row;
            })
          : [h("tr", {}, h("td", { class: "empty", text: options.empty, attrs: { colspan: 2 } }))]),
      );
      save.disabled = !dirty();
    };
    const commit = (): boolean => {
      const item = options.make(first.value, second.value);
      const problem = options.check(item, items, editing);
      if (problem) {
        status.textContent = problem;
        return false;
      }
      if (editing === null) items = [...items, item];
      else items = items.map((current, index) => (index === editing ? item : current));
      status.textContent = "";
      render();
      fill(null);
      first.focus();
      return true;
    };
    const removeSelected = async () => {
      if (editing === null) return;
      items = items.filter((_item, index) => index !== editing);
      render();
      fill(null);
    };
    const persist = async () => {
      // An unapplied form is part of what the person meant to save.
      if ((first.value.trim() || second.value.trim()) && !commit()) return;
      save.disabled = true;
      status.textContent = tr("Salvando…");
      try {
        await options.save(items, baseline);
        saved = true;
        finish();
      } catch (error) {
        status.textContent = error instanceof Error ? error.message : String(error);
        save.disabled = false;
      }
    };
    dialog.append(
      h("p", { class: "caption", text: options.intro }),
      h("div", { class: "table-scroll" }, h("table", { class: "list" }, h("thead", {}, h("tr", {}, ...options.columns.map((label) => h("th", { text: label, attrs: { scope: "col" } })))), tbody)),
      h("label", { class: "field" }, h("span", { text: options.columns[0] }), first),
      h("label", { class: "field" }, h("span", { text: options.columns[1] }), second),
      h("div", { class: "btn-row" }, button(tr("Novo"), () => fill(null), "quiet"), apply, remove),
      status,
      h("div", { class: "dialog-actions" }, button(tr("Cancelar"), () => void leave()), save),
    );
    dialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      void leave();
    });
    dialog.showModal();
    status.textContent = tr("Carregando…");
    options.load().then(
      (loaded) => {
        baseline = loaded;
        items = [...loaded];
        status.textContent = "";
        render();
        fill(null);
        first.focus();
      },
      (error: unknown) => {
        status.textContent = error instanceof Error ? error.message : String(error);
      },
    );
  });
}

export function commandsDialog(bridge: Bridge): Promise<boolean> {
  return pairEditor<VoiceCommand>({
    title: tr("Comandos por voz"),
    intro: tr("Associe uma frase falada ao texto que deseja inserir. O comando deve corresponder à frase inteira."),
    columns: [tr("Frase falada"), tr("Texto a inserir")],
    load: async () => (await bridge.call<{ commands: VoiceCommand[] }>("dictation.commands")).commands,
    save: async (commands, baseline) => void (await bridge.call("dictation.saveCommands", { commands, baseline })),
    read: (item) => [item.phrase, item.text],
    make: (phrase, text) => ({ phrase: phrase.split(/\s+/).filter(Boolean).join(" ").toLowerCase(), text }),
    check: (item, items, editing) => {
      if (!item.phrase || item.phrase.startsWith("_") || item.phrase.length > 160) return tr("Use uma frase de até 160 caracteres, sem começar com sublinhado.");
      if (!item.text.trim() || item.text.length > 100_000) return tr("Informe um texto de até 100.000 caracteres para inserir.");
      if (items.some((other, index) => index !== editing && other.phrase.toLowerCase() === item.phrase)) return tr("Já existe um comando com essa frase.");
      return "";
    },
    multiline: true,
    empty: tr("Nenhum comando. Adicione uma frase e o texto abaixo."),
  });
}

export function correctionsDialog(bridge: Bridge): Promise<boolean> {
  return pairEditor<Correction>({
    title: tr("Correções da transcrição"),
    intro: tr("Corrija termos recorrentes reconhecidos incorretamente."),
    columns: [tr("Texto reconhecido incorretamente:"), tr("Substituir por:")],
    load: async () => (await bridge.call<{ items: Correction[] }>("dictation.corrections")).items,
    save: async (items) => void (await bridge.call("dictation.saveCorrections", { items })),
    read: (item) => [item.source, item.replacement],
    make: (source, replacement) => ({ source: source.trim(), replacement }),
    check: (item, items, editing) => {
      if (!item.source || !item.replacement.trim()) return tr("O texto não pode ser vazio ou muito longo.");
      if (items.some((other, index) => index !== editing && other.source.toLowerCase() === item.source.toLowerCase())) return tr("Revise os termos informados.");
      return "";
    },
    multiline: false,
    empty: tr("Nenhuma correção ainda."),
  });
}

export async function licensesDialog(bridge: Bridge): Promise<void> {
  const { dialog, close } = modal(tr("Licenças e atribuições"), true);
  const body = h("pre", { class: "licenses selectable", text: tr("Carregando…") });
  dialog.append(body, h("div", { class: "dialog-actions" }, button(tr("Fechar"), close, "primary")));
  dialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    close();
  });
  dialog.showModal();
  try {
    body.textContent = (await bridge.call<{ lines: string[] }>("dictation.licenses")).lines.join("\n");
  } catch (error) {
    body.textContent = error instanceof Error ? error.message : String(error);
  }
}
