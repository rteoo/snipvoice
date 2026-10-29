// Biblioteca: search and select a recording, then read, summarize, ask about,
// export or delete it.
import { allowEngineForeground, type Bridge } from "../bridge";
import { button, h, ICONS, icon, replace } from "../dom";
import { tr } from "../i18n";
import type { LibraryItem, LibraryPage, SessionDetail, SummaryView, Transcript, TrashPreview } from "../types";
import { confirmDialog, copyText, MessageSlot, pageHeader, toneText } from "../ui";
import { ChatPanel } from "./chat";
import { FilesPanel } from "./files";
import { SummaryPanel } from "./summary";
import { openTrash } from "./trash";

const SEARCH_DELAY_MS = 250;
const MAX_BACK_STACK = 32;
type Style = "full_text" | "timestamped";
type Section = "transcript" | "summary" | "chat" | "files";
const SECTIONS: Section[] = ["transcript", "summary", "chat", "files"];

export class LibraryView {
  readonly element: HTMLElement;
  readonly messages = new MessageSlot();
  private items: LibraryItem[] = [];
  private cursor: unknown = null;
  private nextCursor: unknown = null;
  private backStack: unknown[] = [];
  private selected: string | null = null;
  private detail: SessionDetail | null = null;
  private transcript: Transcript | null = null;
  private style: Style = "full_text";
  private section: Section = "transcript";
  private summaryView: SummaryView | null = null;
  private readonly summary: SummaryPanel;
  private readonly chat: ChatPanel;
  private readonly files: FilesPanel;
  private listToken = 0;
  private detailToken = 0;
  private searchTimer: number | undefined;
  private readonly search = h("input", { attrs: { type: "search", id: "library-search", autocomplete: "off" } });
  private readonly tbody = h("tbody");
  private readonly count = h("p", { class: "caption" });
  private readonly pager = h("div", { class: "btn-row" });
  private readonly detailPane = h("div", { class: "detail-pane" });

  constructor(
    private readonly bridge: Bridge,
    private readonly enginePid: number,
  ) {
    this.summary = new SummaryPanel(bridge, this.messages);
    this.chat = new ChatPanel(bridge, this.messages);
    this.files = new FilesPanel(bridge, this.messages, enginePid, () => void this.deleteSelected());
    this.search.placeholder = tr("Buscar em títulos e transcrições");
    this.search.addEventListener("input", () => {
      window.clearTimeout(this.searchTimer);
      this.listToken++; // typing invalidates an earlier read immediately
      this.searchTimer = window.setTimeout(() => void this.refresh(true), SEARCH_DELAY_MS);
    });
    this.search.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        window.clearTimeout(this.searchTimer);
        void this.refresh(true);
      } else if (event.key === "Escape" && this.search.value) {
        event.stopPropagation();
        this.search.value = "";
        void this.refresh(true);
      }
    });
    this.element = h(
      "section",
      { class: "page" },
      pageHeader(
        tr("Biblioteca"),
        button(tr("Importar áudio…"), () => void this.importAudio(), "quiet"),
        button(tr("Lixeira…"), () => openTrash(this.bridge, () => void this.refresh()), "quiet"),
      ),
      h(
        "div",
        { class: "library-toolbar" },
        h("label", { class: "sr-only", text: tr("Buscar na biblioteca"), attrs: { for: "library-search" } }),
        h("span", { class: "search-icon" }, icon(ICONS.search)),
        this.search,
      ),
      this.messages.element,
      h(
        "div",
        { class: "library-split" },
        h(
          "div",
          { class: "library-list" },
          h(
            "div",
            { class: "table-scroll" },
            h(
              "table",
              { class: "list" },
              h(
                "thead",
                {},
                h(
                  "tr",
                  {},
                  h("th", { class: "col-state", text: tr("Estado"), attrs: { scope: "col" } }),
                  h("th", { text: tr("Gravação"), attrs: { scope: "col" } }),
                  h("th", { class: "col-date", text: tr("Data"), attrs: { scope: "col" } }),
                  h("th", { class: "col-duration num", text: tr("Duração"), attrs: { scope: "col" } }),
                ),
              ),
              this.tbody,
            ),
          ),
          h("div", { class: "list-footer" }, this.count, this.pager),
        ),
        this.detailPane,
      ),
    );
    this.renderDetail();
    void this.refresh(true);
  }

  focusSearch(): void {
    this.search.focus();
    this.search.select();
  }

  async open(id: string): Promise<void> {
    this.selected = id;
    // Rows stay in place so the focused row keeps focus while arrows move the selection.
    this.tbody.querySelectorAll("tr[data-id]").forEach((row) =>
      row.setAttribute("aria-selected", String(row.getAttribute("data-id") === id)));
    await this.loadDetail(id);
  }

  async refresh(reset = false): Promise<void> {
    if (reset) {
      this.cursor = null;
      this.backStack = [];
    }
    const token = ++this.listToken;
    try {
      const page = await this.bridge.call<LibraryPage>("library.list", { query: this.search.value, cursor: this.cursor });
      if (token !== this.listToken) return;
      this.items = page.items;
      this.nextCursor = page.nextCursor ?? null;
    } catch (error) {
      if (token !== this.listToken) return;
      this.messages.fail(tr("Não foi possível carregar a biblioteca"), "", { error, action: { label: tr("Tentar novamente"), run: () => void this.refresh() } });
      return;
    }
    this.renderRows();
  }

  private page(direction: 1 | -1): void {
    if (direction === 1 && this.nextCursor !== null) {
      this.backStack = [...this.backStack, this.cursor].slice(-MAX_BACK_STACK);
      this.cursor = this.nextCursor;
    } else if (direction === -1 && this.backStack.length) {
      this.cursor = this.backStack[this.backStack.length - 1];
      this.backStack = this.backStack.slice(0, -1);
    } else {
      return;
    }
    void this.refresh();
  }

  private renderRows(): void {
    const rows = this.items.map((item) => {
      const row = h(
        "tr",
        { attrs: { tabindex: 0, "aria-selected": item.id === this.selected ? "true" : "false", "data-id": item.id } },
        h("td", { class: `col-state ${toneText(item.tone)}`, text: item.status }),
        h("td", { text: item.title, attrs: { title: item.title } }),
        h("td", { class: "col-date", text: item.date }),
        h("td", { class: "col-duration num", text: item.duration }),
      );
      row.addEventListener("click", () => void this.open(item.id));
      row.addEventListener("keydown", (event) => {
        if (event.key === "Enter") void this.open(item.id);
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
          event.preventDefault();
          const sibling = event.key === "ArrowDown" ? row.nextElementSibling : row.previousElementSibling;
          if (sibling instanceof HTMLElement && sibling.dataset.id) {
            sibling.focus();
            void this.open(sibling.dataset.id);
          }
        }
      });
      return row;
    });
    const query = this.search.value.trim();
    replace(
      this.tbody,
      ...(rows.length
        ? rows
        : [
            h(
              "tr",
              {},
              h(
                "td",
                { class: "empty", attrs: { colspan: 4 } },
                h("p", { text: query ? tr("Nada encontrado para essa busca.") : tr("Nenhuma gravação ainda.") }),
                query ? button(tr("Limpar busca"), () => ((this.search.value = ""), void this.refresh(true)), "quiet") : null,
              ),
            ),
          ]),
    );
    const count = this.items.length;
    const paged = this.backStack.length > 0 || this.nextCursor !== null;
    const words = count === 1 ? tr("1 gravação") : tr("{count} gravações", { count });
    this.count.textContent = paged ? tr("{count} · página {page}", { count: words, page: this.backStack.length + 1 }) : words;
    const previous = button(tr("‹ Anterior"), () => this.page(-1), "quiet");
    previous.disabled = !this.backStack.length;
    const next = button(tr("Próxima ›"), () => this.page(1), "quiet");
    next.disabled = this.nextCursor === null;
    replace(this.pager, ...(paged ? [previous, next] : []));
  }

  private async loadDetail(id: string): Promise<void> {
    const token = ++this.detailToken;
    this.messages.clear();
    try {
      const detail = await this.bridge.call<SessionDetail>("library.get", { id });
      if (token !== this.detailToken) return;
      this.detail = detail;
      this.transcript = null;
      this.summaryView = null;
      this.renderDetail();
      await Promise.all([this.loadTranscript(token), this.loadSummary(token)]);
    } catch (error) {
      if (token !== this.detailToken) return;
      this.detail = null;
      this.renderDetail();
      this.messages.fail(tr("Não foi possível abrir a gravação"), "", { error });
    }
  }

  private async loadTranscript(token = this.detailToken): Promise<void> {
    const detail = this.detail;
    if (!detail) return;
    try {
      const transcript = await this.bridge.call<Transcript>("library.transcript", { id: detail.id, revision: detail.revision, style: this.style });
      if (token !== this.detailToken) return;
      this.transcript = transcript;
    } catch (error) {
      if (token !== this.detailToken) return;
      this.transcript = null;
      this.messages.fail(tr("Não foi possível ler a transcrição"), "", { error });
    }
    this.renderDetail();
  }

  private async loadSummary(token = this.detailToken): Promise<void> {
    const detail = this.detail;
    if (!detail) return;
    try {
      const view = await this.bridge.call<SummaryView>("library.summary", { id: detail.id });
      if (token !== this.detailToken) return;
      this.summaryView = view;
    } catch (error) {
      if (token !== this.detailToken) return;
      this.messages.fail(tr("Não foi possível carregar as versões do resumo"), "", { error });
    }
    this.renderSection();
  }

  private renderDetail(): void {
    const detail = this.detail;
    if (!detail) {
      replace(this.detailPane, h("p", { class: "empty", text: tr("Selecione uma gravação para ler a transcrição.") }));
      return;
    }
    const title = h("h2", { class: "selectable", text: detail.title, attrs: { tabindex: -1 } });
    const rename = button(tr("Renomear"), () => this.editTitle(title), "quiet");
    rename.title = "F2";
    const labels: Record<Section, string> = { transcript: tr("Transcrição"), summary: tr("Resumo"), chat: tr("Chat"), files: tr("Arquivos") };
    const tabs = h("div", { class: "tabs", attrs: { role: "tablist", "aria-label": tr("Seções da gravação") } });
    for (const section of SECTIONS) {
      const tab = h("button", {
        class: "tab",
        text: labels[section],
        attrs: { type: "button", role: "tab", id: `tab-${section}`, "aria-controls": "section-panel", "aria-selected": String(section === this.section), tabindex: section === this.section ? 0 : -1 },
      });
      tab.addEventListener("click", () => this.showSection(section));
      tab.addEventListener("keydown", (event) => {
        if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
        event.preventDefault();
        const step = event.key === "ArrowRight" ? 1 : SECTIONS.length - 1;
        const next = SECTIONS[(SECTIONS.indexOf(section) + step) % SECTIONS.length] ?? "transcript";
        this.showSection(next);
        tabs.querySelector<HTMLElement>(`#tab-${next}`)?.focus();
      });
      tabs.append(tab);
    }
    replace(
      this.detailPane,
      h(
        "div",
        { class: "detail-header" },
        h("div", { class: "title-row" }, title, rename),
        h("p", { class: "caption" }, h("span", { class: toneText(detail.tone), text: detail.status }), ` · ${detail.date} · ${detail.duration}`),
      ),
      tabs,
      h("div", { class: "section-panel", attrs: { id: "section-panel", role: "tabpanel", "aria-labelledby": `tab-${this.section}` } }),
    );
    this.renderSection();
  }

  private showSection(section: Section): void {
    this.section = section;
    this.detailPane.querySelectorAll<HTMLElement>("[role=tab]").forEach((tab) => {
      const current = tab.id === `tab-${section}`;
      tab.setAttribute("aria-selected", String(current));
      tab.tabIndex = current ? 0 : -1;
    });
    this.detailPane.querySelector("[role=tabpanel]")?.setAttribute("aria-labelledby", `tab-${section}`);
    this.renderSection();
  }

  private renderSection(): void {
    const detail = this.detail;
    const panel = this.detailPane.querySelector<HTMLElement>("[role=tabpanel]");
    if (!detail || !panel) return;
    if (this.section === "summary") {
      this.summary.show(detail.id, this.summaryView);
      replace(panel, this.summary.element);
    } else if (this.section === "chat") {
      this.chat.show(detail.id, this.summaryView?.modelReady ?? false, this.summaryView?.canSaveAnswers ?? false);
      replace(panel, this.chat.element);
    } else if (this.section === "files") {
      void this.files.show(detail.id);
      replace(panel, this.files.element);
    } else {
      replace(panel, ...this.transcriptSection());
    }
  }

  private transcriptSection(): (HTMLElement | null)[] {
    const styles = h(
      "div",
      { class: "segmented", attrs: { role: "group", "aria-label": tr("Formato da transcrição") } },
      ...([
        ["full_text", tr("Texto completo")],
        ["timestamped", tr("Com horários")],
      ] as const).map(([style, label]) =>
        button(label, () => {
          if (this.style === style) return;
          this.style = style;
          void this.loadTranscript();
        }),
      ),
    );
    styles.querySelectorAll("button").forEach((node, index) => node.setAttribute("aria-pressed", String((index === 0) === (this.style === "full_text"))));
    const transcript = this.transcript;
    const copy = button(tr("Copiar transcrição"), () => void this.copyTranscript(), "primary");
    // A partial copy would pass for the whole transcript.
    copy.disabled = !transcript || transcript.empty || transcript.truncated;
    const body = !transcript
      ? h("p", { class: "caption", text: tr("Carregando transcrição…") })
      : transcript.empty
        ? h("p", { class: "empty", text: tr("Esta gravação ainda não tem transcrição.") })
        : h("article", { class: "transcript selectable", text: transcript.text, attrs: { tabindex: 0 } });
    return [
      h("div", { class: "detail-commands" }, styles, copy),
      transcript?.truncated ? h("p", { class: "caption text-warning", text: tr("Transcrição extensa: prévia limitada.") }) : null,
      body,
    ];
  }

  private async importAudio(): Promise<void> {
    try {
      await allowEngineForeground(this.enginePid);
      const result = await this.bridge.call<{ id: string | null }>("library.importAudio");
      if (!result.id) return;
      this.search.value = "";
      await this.refresh(true);
      await this.open(result.id);
      this.messages.say(tr("Áudio importado. A transcrição começa sozinha quando houver um modelo instalado."));
    } catch (error) {
      void this.refresh();
      this.messages.fail(tr("Não foi possível importar o áudio"), "", { error });
    }
  }

  private async deleteSelected(): Promise<void> {
    const detail = this.detail;
    if (!detail) return;
    let preview: TrashPreview;
    try {
      preview = await this.bridge.call<TrashPreview>("library.trashPreview", { id: detail.id });
    } catch (error) {
      this.messages.fail(tr("A prévia de retenção falhou"), "", { error });
      return;
    }
    if (!preview.eligible) {
      this.messages.fail(tr("A retenção foi bloqueada"), preview.reasons, { tone: "warning" });
      return;
    }
    const confirmed = await confirmDialog({ title: tr("Mover para a lixeira"), body: preview.question, confirm: tr("Mover para a lixeira"), danger: true, details: preview.details });
    if (!confirmed) return this.messages.say(tr("Nenhuma exclusão foi aplicada."));
    try {
      await this.bridge.call("library.trash", { token: preview.token });
    } catch (error) {
      this.messages.fail(tr("A retenção não foi aplicada"), "", { error });
      return;
    }
    this.detailToken++;
    this.selected = null;
    this.detail = null;
    this.renderDetail();
    void this.refresh();
    this.messages.fail(tr("Movida para a lixeira"), "", { tone: "success", action: { label: tr("Desfazer"), run: () => void this.undoTrash(detail.id) } });
  }

  private async undoTrash(id: string): Promise<void> {
    try {
      await this.bridge.call("library.restore", { id });
    } catch (error) {
      this.messages.fail(tr("Não foi possível restaurar a gravação"), tr("Abra a lixeira para tentar novamente."), { error });
      return;
    }
    await this.refresh();
    this.messages.say(tr("Gravação restaurada na biblioteca."));
  }

  editTitle(title = this.detailPane.querySelector("h2")): void {
    const detail = this.detail;
    if (!detail || !title) return;
    const input = h("input", { attrs: { type: "text", maxlength: 400, "aria-label": tr("Título da gravação") } });
    input.value = detail.title;
    let done = false;
    const finish = async (save: boolean) => {
      if (done) return;
      done = true;
      const value = input.value.trim();
      if (!save || !value || value === detail.title) return this.renderDetail();
      try {
        const result = await this.bridge.call<{ title: string }>("library.rename", { id: detail.id, title: value });
        detail.title = result.title;
        const row = this.items.find((item) => item.id === detail.id);
        if (row) row.title = result.title;
        this.renderRows();
        this.messages.say(tr("Título salvo"));
      } catch (error) {
        this.messages.fail(tr("Não foi possível salvar o título. Tente de novo."), "", { error });
      }
      this.renderDetail();
    };
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || (event.key === "s" && event.ctrlKey)) {
        event.preventDefault();
        void finish(true);
      } else if (event.key === "Escape") {
        event.stopPropagation();
        void finish(false);
      }
    });
    input.addEventListener("blur", () => void finish(true));
    title.replaceWith(input);
    input.focus();
    input.select();
  }

  private async copyTranscript(): Promise<void> {
    if (!this.transcript || this.transcript.empty) return;
    if (await copyText(this.transcript.text)) this.messages.say(tr("Transcrição copiada."));
    else this.messages.fail(tr("Não foi possível copiar a transcrição"), tr("Selecione o texto e pressione Ctrl+C"), { tone: "warning" });
  }

  hasSelection(): boolean {
    return this.detail !== null;
  }
}
