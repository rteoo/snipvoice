"""Busca for the Tauri manager: semantic search, its model, reindexing and repair.

``SearchActions`` adds these JSON-RPC methods to ``ManagerApi``. Word search
always works; semantic search needs the embedding model and a compatible
local runtime. Downloads and the index repair run on their own threads with
throttled progress pushes; while the semantic index rebuilds in the library's
worker, a watcher polls its state, as the Tk window did, and pushes changes.
"""

import threading
import time

from i18n import tr
from manager_bridge import INVALID_PARAMS, RpcFailure, technical_detail
from meeting_gui import INDEX_STATE_LABELS
from summary_catalog import format_model_size

PROGRESS_INTERVAL = 0.25
# How often the watcher reads the semantic state while it rebuilds, and for how long at most.
WATCH_INTERVAL = 1.0
WATCH_LIMIT = 3600.0


class SearchActions:
    """``meetings`` (MeetingController), ``embedding_models`` (module with
    ``MODEL`` and ``download_embedding_model``), ``notify(method, params)`` and
    ``logger`` (``warning(message)``)."""

    def __init__(self, *, meetings, embedding_models, notify, logger):
        self.meetings = meetings
        self.embedding_models = embedding_models
        self.notify = notify
        self.logger = logger
        self._lock = threading.Lock()
        self._download = None
        self._repair = None
        self._watching = False

    def methods(self):
        return {
            "search.state": self.state,
            "search.setSemantic": self.set_semantic,
            "search.downloadModel": self.download_model,
            "search.reindex": self.reindex,
            "search.cancel": self.cancel,
            "search.repair": self.repair,
            "search.cancelRepair": self.cancel_repair,
        }

    @staticmethod
    def _label(state):
        return tr(INDEX_STATE_LABELS.get(state, state)) if isinstance(state, str) else ""

    def state(self, _params):
        try:
            info = self.meetings.semantic_info() or {}
            index = self.meetings.index_state()
        except Exception as exc:
            raise RpcFailure(tr("Índice de busca: estado desconhecido"), detail=technical_detail(exc)) from exc
        model = self.embedding_models.MODEL
        semantic = info.get("state", "disabled")
        with self._lock:
            download, repair = self._download, self._repair
        return {
            "enabled": bool(info.get("enabled")),
            "installed": bool(info.get("installed")),
            "model": {"name": model["name"], "size": format_model_size(model["size_bytes"]),
                      "license": model["license_id"]},
            "semantic": semantic,
            "status": (tr("Busca semântica: {state}", state=self._label(semantic)) if info.get("enabled")
                       else tr("Busca por palavras ativa.")),
            "index": tr("Índice de busca: {state}", state=self._label(index)) if index else
            tr("Índice de busca: estado desconhecido"),
            "download": self._progress(download) if download else None,
            "repair": self._progress(repair) if repair else None,
        }

    @staticmethod
    def _progress(job):
        return {"done": job["done"], "total": job["total"]}

    def set_semantic(self, params):
        enabled = params.get("enabled")
        if not isinstance(enabled, bool):
            raise RpcFailure(tr("Pedido inválido."), code=INVALID_PARAMS)
        try:
            self.meetings.set_semantic_search(enabled)
        except Exception as exc:
            raise RpcFailure(tr("Não foi possível salvar a opção de busca semântica."),
                             detail=technical_detail(exc)) from exc
        self._watch()
        return self.state({})

    def _run(self, kind, work):
        """Run ``work(job)`` on its own thread; push progress, then ``search.finished``."""
        job = {"done": 0, "total": None, "cancel": threading.Event(), "pushed": 0.0}
        with self._lock:
            if getattr(self, f"_{kind}") is not None:
                raise RpcFailure(tr("Aguarde a operação atual e tente novamente."))
            setattr(self, f"_{kind}", job)

        def progress(done, total):
            job["done"], job["total"] = done, total
            now = time.monotonic()
            if now - job["pushed"] >= PROGRESS_INTERVAL:
                job["pushed"] = now
                self.notify("search.progress", {"kind": kind, **self._progress(job)})

        def run():
            outcome = {"kind": kind, "ok": True}
            try:
                work(job, progress)
            except Exception as exc:  # noqa: BLE001 - every outcome must reach the window
                outcome = {"kind": kind, "ok": False, "cancelled": job["cancel"].is_set(),
                           "detail": technical_detail(exc)}
            finally:
                with self._lock:
                    setattr(self, f"_{kind}", None)
            self.notify("search.finished", outcome)

        threading.Thread(target=run, name=f"manager-search-{kind}", daemon=True).start()

    def download_model(self, params):
        model = self.embedding_models.MODEL
        if params.get("confirmed") is not True:
            return {"status": "confirm", "title": tr("Baixar modelo local"), "message": tr(
                "Baixar {name} ({size})?\nLicença: {license}\nO arquivo será verificado por SHA-256. "
                "A busca semântica exige um runtime local compatível.", name=model["name"],
                size=format_model_size(model["size_bytes"]), license=model["license_id"])}
        self._run("download", lambda job, progress: self.embedding_models.download_embedding_model(
            cancel_event=job["cancel"], progress=progress))
        return {"status": "started"}

    def reindex(self, _params):
        info = self.meetings.semantic_info() or {}
        if not info.get("enabled"):
            raise RpcFailure(tr("Ative a busca semântica antes de reindexar."))
        try:
            self.meetings.rebuild_semantic_index()
        except Exception as exc:
            raise RpcFailure(tr("Operação semântica cancelada ou indisponível. Busca por palavras disponível."),
                             detail=technical_detail(exc)) from exc
        self._watch()
        return self.state({})

    def cancel(self, _params):
        with self._lock:
            download = self._download
        if download is not None:
            download["cancel"].set()
        else:
            self.meetings.cancel_semantic_index()
        return {"cancelled": True}

    def repair(self, _params):
        self._run("repair", lambda job, progress: self.meetings.rebuild_index(cancel_event=job["cancel"],
                                                                             progress=progress))
        return {"status": "started"}

    def cancel_repair(self, _params):
        with self._lock:
            job = self._repair
        if job is None:
            raise RpcFailure(tr("Pedido inválido."), code=INVALID_PARAMS)
        job["cancel"].set()
        return {"cancelled": True}

    def _watch(self):
        """Push semantic state changes while the library's worker rebuilds the index."""
        with self._lock:
            if self._watching:
                return
            self._watching = True

        def watch():
            previous, started = None, time.monotonic()
            try:
                while time.monotonic() - started < WATCH_LIMIT:
                    state = self.meetings.semantic_state()
                    if state != previous:
                        previous = state
                        self.notify("search.changed", self.state({}))
                    if state != "rebuilding" and time.monotonic() - started > 2 * WATCH_INTERVAL:
                        return
                    time.sleep(WATCH_INTERVAL)
            except Exception as exc:  # noqa: BLE001 - a lost watcher only delays the next refresh
                self.logger.warning(f"semantic state watch stopped: {technical_detail(exc)}")
            finally:
                with self._lock:
                    self._watching = False

        threading.Thread(target=watch, name="manager-search-watch", daemon=True).start()
