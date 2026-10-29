// Lixeira: restore a recording or delete it for good; emptying removes only expired ones.
import type { Bridge } from "../bridge";
import { button, h, replace } from "../dom";
import { tr } from "../i18n";
import type { TrashItem, TrashList } from "../types";
import { typedConfirmDialog } from "../ui";

/** Opens the trash as a modal dialog; `onChanged` refreshes the library after a restore or deletion. */
export function openTrash(bridge: Bridge, onChanged: () => void): void {
  const opener = document.activeElement as HTMLElement | null;
  const dialog = h("dialog", { class: "trash-dialog", attrs: { "aria-labelledby": "trash-title" } });
  const status = h("p", { class: "caption", attrs: { role: "status", "aria-live": "polite" } });
  const tbody = h("tbody");
  const restore = button(tr("Restaurar"), () => void act("restore"), "primary");
  const purge = button(tr("Excluir definitivamente…"), () => void act("purge"), "danger");
  const empty = button(tr("Esvaziar expirados…"), () => void act("empty"));
  let list: TrashList = { items: [], summary: "", expired: 0 };
  let selected: string | null = null;
  let busy = false;

  const close = () => {
    dialog.close();
    dialog.remove();
    opener?.focus();
  };
  const sync = () => {
    restore.disabled = purge.disabled = busy || !selected;
    empty.disabled = busy || !list.expired;
  };
  const select = (id: string) => {
    selected = id;
    tbody.querySelectorAll("tr[data-id]").forEach((row) => row.setAttribute("aria-selected", String(row.getAttribute("data-id") === id)));
    sync();
  };
  const render = () => {
    replace(
      tbody,
      ...(list.items.length
        ? list.items.map((item, index) => {
            const row = h(
              "tr",
              { attrs: { tabindex: 0, "data-id": item.id, "aria-selected": String(item.id === selected) } },
              h("td", { text: item.name }),
              h("td", { text: item.moved }),
              h("td", { class: item.expired ? "text-warning" : "", text: item.expires }),
              h("td", { class: "num", text: item.size }),
            );
            row.addEventListener("click", () => select(item.id));
            row.addEventListener("keydown", (event) => {
              if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
              event.preventDefault();
              const next = list.items[index + (event.key === "ArrowDown" ? 1 : -1)];
              const sibling = event.key === "ArrowDown" ? row.nextElementSibling : row.previousElementSibling;
              if (next && sibling instanceof HTMLElement) {
                sibling.focus();
                select(next.id);
              }
            });
            return row;
          })
        : [h("tr", {}, h("td", { class: "empty", text: tr("A lixeira está vazia"), attrs: { colspan: 4 } }))]),
    );
    sync();
  };
  const load = async (receipt = "") => {
    status.textContent = tr("Carregando lixeira…");
    try {
      list = await bridge.call<TrashList>("trash.list");
      // Keep the row the person was on; otherwise start at the top.
      if (!list.items.some((item: TrashItem) => item.id === selected)) selected = list.items[0]?.id ?? null;
      status.textContent = receipt ? `${receipt} · ${list.summary}` : list.summary;
    } catch (error) {
      status.textContent = error instanceof Error && error.message ? error.message : tr("Não foi possível carregar a lixeira. Tente Atualizar.");
    }
    render();
  };
  const act = async (kind: "restore" | "purge" | "empty") => {
    const item = list.items.find((entry) => entry.id === selected);
    if (kind !== "empty" && !item) return void (status.textContent = tr("Selecione uma gravação na lixeira."));
    if (kind === "purge" && item && !(await typedConfirmDialog({ question: tr("Excluir “{name}” definitivamente?", { name: item.name }), facts: tr("Movida para a lixeira: {moved} · {size}", { moved: item.moved, size: item.size }) }))) return;
    if (kind === "empty" && !(await typedConfirmDialog({
      question: list.expired === 1 ? tr("Excluir 1 gravação expirada definitivamente?") : tr("Excluir {count} gravações expiradas definitivamente?", { count: list.expired }),
      facts: tr("Somente as gravações cujo prazo na lixeira já venceu."),
    }))) return;
    busy = true;
    sync();
    const name = item?.name ?? "";
    status.textContent = kind === "restore" ? tr("Restaurando {name}…", { name }) : kind === "purge" ? tr("Excluindo {name} definitivamente…", { name }) : tr("Excluindo as gravações expiradas…");
    let receipt = "";
    try {
      if (kind === "restore") await bridge.call("library.restore", { id: item?.id ?? "" });
      else if (kind === "purge") await bridge.call("trash.purge", { id: item?.id ?? "" });
      else await bridge.call("trash.emptyExpired");
      receipt = kind === "restore" ? tr("Gravação {name} restaurada na Biblioteca", { name }) : kind === "purge" ? tr("Gravação {name} excluída definitivamente", { name }) : tr("Gravações expiradas excluídas definitivamente");
      onChanged();
    } catch (error) {
      receipt = `${tr("A lixeira não confirmou a ação")}: ${error instanceof Error ? error.message : ""}`;
    }
    busy = false;
    await load(receipt);
  };

  dialog.append(
    h("h2", { text: tr("Lixeira"), attrs: { id: "trash-title" } }),
    h("p", { class: "caption", text: tr("Restaurar devolve a gravação à Biblioteca. Excluir definitivamente apaga os arquivos do app e não pode ser desfeito.") }),
    h(
      "div",
      { class: "table-scroll" },
      h(
        "table",
        { class: "list" },
        h("thead", {}, h("tr", {}, ...[tr("Gravação"), tr("Movida em"), tr("Expira"), tr("Tamanho")].map((label, index) => h("th", { class: index === 3 ? "num" : "", text: label, attrs: { scope: "col" } })))),
        tbody,
      ),
    ),
    status,
    h("div", { class: "dialog-actions" }, button(tr("Atualizar"), () => void load(), "quiet"), empty, purge, restore, button(tr("Fechar"), close)),
  );
  dialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    close();
  });
  document.body.append(dialog);
  dialog.showModal();
  void load();
}
