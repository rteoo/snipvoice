"""Busca for the Tauri manager: the word-search index state and its repair.

``SearchActions`` adds these JSON-RPC methods to ``ManagerApi``. The repair
runs on its own thread with throttled ``search.progress`` pushes and ends with
``search.finished``.
"""

import threading
import time

from i18n import tr
from manager_bridge import INVALID_PARAMS, RpcFailure, technical_detail
from meeting_gui import INDEX_STATE_LABELS

PROGRESS_INTERVAL = 0.25


class SearchActions:
    """``meetings`` (MeetingController) and ``notify(method, params)``."""

    def __init__(self, *, meetings, notify):
        self.meetings = meetings
        self.notify = notify
        self._lock = threading.Lock()
        self._repair = None

    def methods(self):
        return {
            "search.state": self.state,
            "search.repair": self.repair,
            "search.cancelRepair": self.cancel_repair,
        }

    def state(self, _params):
        try:
            index = self.meetings.index_state()
        except Exception as exc:
            raise RpcFailure(tr("Índice de busca: estado desconhecido"), detail=technical_detail(exc)) from exc
        label = tr(INDEX_STATE_LABELS.get(index, index)) if isinstance(index, str) and index else ""
        with self._lock:
            repair = self._repair
        return {
            "index": tr("Índice de busca: {state}", state=label) if label else
            tr("Índice de busca: estado desconhecido"),
            "repair": self._progress(repair) if repair else None,
        }

    @staticmethod
    def _progress(job):
        return {"done": job["done"], "total": job["total"]}

    def repair(self, _params):
        """Rebuild the word-search index on its own thread; push progress, then ``search.finished``."""
        job = {"done": 0, "total": None, "cancel": threading.Event(), "pushed": 0.0}
        with self._lock:
            if self._repair is not None:
                raise RpcFailure(tr("Aguarde a operação atual e tente novamente."))
            self._repair = job

        def progress(done, total):
            job["done"], job["total"] = done, total
            now = time.monotonic()
            if now - job["pushed"] >= PROGRESS_INTERVAL:
                job["pushed"] = now
                self.notify("search.progress", self._progress(job))

        def run():
            outcome = {"ok": True}
            try:
                self.meetings.rebuild_index(cancel_event=job["cancel"], progress=progress)
            except Exception as exc:  # noqa: BLE001 - every outcome must reach the window
                outcome = {"ok": False, "cancelled": job["cancel"].is_set(), "detail": technical_detail(exc)}
            finally:
                with self._lock:
                    self._repair = None
            self.notify("search.finished", outcome)

        threading.Thread(target=run, name="manager-search-repair", daemon=True).start()
        return {"status": "started"}

    def cancel_repair(self, _params):
        with self._lock:
            job = self._repair
        if job is None:
            raise RpcFailure(tr("Pedido inválido."), code=INVALID_PARAMS)
        job["cancel"].set()
        return {"cancelled": True}
