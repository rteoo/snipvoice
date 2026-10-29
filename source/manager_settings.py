"""Configurações for the Tauri manager: Gravação, Privacidade, Modelos and folders.

``SettingsActions`` adds these JSON-RPC methods to ``ManagerApi`` and keeps the
Tk manager's validation: recording defaults go through ``validated_settings``,
privacy saves carry the workspace generation they were read at, and a folder
move is validated twice, before the confirmation and again before it is
recorded. A summary model download runs on its own thread; its progress
reaches the window as throttled ``models.summaryProgress`` notifications.
"""

import copy
import math
import os
import threading
import time

import data_relocation
import voice_models
from i18n import N_, tr
from manager_bridge import INVALID_PARAMS, RpcFailure, _text, technical_detail
from meeting_gui import LANGUAGE_LABELS, PROFILE_LABELS, SUMMARY_LABELS, destination_display, profile_display_label
from meeting_settings import FOLLOW_DICTATION, dictation_profile, resolve_meeting_settings, validate_hotkey_conflicts
from summary_catalog import format_model_size, summary_catalog, summary_catalog_entry
from voice_catalog import available_languages, format_size, selectable_catalog

MAX_DAYS = 36500
DEFAULT_TRASH_DAYS = 30.0
DEFAULT_AUTO_TRASH_DAYS = 90.0
# A progress push at most this often; the bytes arrive far faster.
PROGRESS_INTERVAL = 0.25
NOTICE_LANGUAGES = ("pt-BR", "en-US")


def _policy_days(value, *, field):
    """Days as the Tk form reads them: blank is None, otherwise 0 to 36 500."""
    text = str(value if value is not None else "").strip()
    if not text:
        return None
    try:
        days = float(text)
    except (TypeError, ValueError) as exc:
        raise RpcFailure(tr("{field}: informe um número de dias válido.", field=field), code=INVALID_PARAMS) from exc
    if not math.isfinite(days) or days < 0 or days > MAX_DAYS:
        raise RpcFailure(tr("{field}: informe um valor entre 0 e 36.500 dias.", field=field), code=INVALID_PARAMS)
    return days


class SettingsActions:
    """Recording defaults, privacy, model downloads and folder moves.

    Dependencies: ``meetings`` (MeetingController), ``voice`` (VoiceController),
    ``settings_getter``/``persist_settings`` (settings.json),
    ``on_settings_changed()`` (rebuilds the recording hotkey),
    ``summary_models`` (module with download/delete/is-installed),
    ``choose_directory(title, initial) -> path or ""``, ``notify(method, params)``,
    ``data_location()``/``models_location()`` (``{path, default, env_locked}``) and
    ``relocate_data(target)``/``relocate_models(target)`` (``""`` or a reason).
    """

    def __init__(self, *, meetings, voice, settings_getter, persist_settings, on_settings_changed, summary_models,
                 choose_directory, notify, data_location, models_location, relocate_data, relocate_models):
        self.meetings = meetings
        self.voice = voice
        self.settings_getter = settings_getter
        self.persist_settings = persist_settings
        self.on_settings_changed = on_settings_changed
        self.summary_models = summary_models
        self.choose_directory = choose_directory
        self.notify = notify
        self.locations = {
            "data": {"info": data_location, "request": relocate_data, "choose": data_relocation.target_for_choice,
                     "validate": data_relocation.validate_target, "size": data_relocation.directory_size,
                     "title": N_("Pasta de dados"), "choose_title": N_("Nova pasta de dados"),
                     "action": N_("Mover os dados do SnipVoice")},
            "models": {"info": models_location, "request": relocate_models, "choose": os.path.abspath,
                       "validate": data_relocation.validate_models_target, "size": data_relocation.models_size,
                       "title": N_("Pasta dos modelos"), "choose_title": N_("Nova pasta dos modelos"),
                       "action": N_("Mover os modelos baixados")},
        }
        self._lock = threading.Lock()
        self._summary_download = None

    def methods(self):
        return {
            "settings.recording": self.recording,
            "settings.saveRecording": self.save_recording,
            "settings.chooseDestination": self.choose_destination,
            "settings.privacy": self.privacy,
            "settings.savePrivacy": self.save_privacy,
            "settings.models": self.models,
            "settings.downloadVoiceModel": self.download_voice_model,
            "settings.cancelVoiceModel": self.cancel_voice_model,
            "settings.downloadSummaryModel": self.download_summary_model,
            "settings.cancelSummaryModel": self.cancel_summary_model,
            "settings.deleteSummaryModel": self.delete_summary_model,
            "settings.locations": self.locations_view,
            "settings.planMove": self.plan_move,
            "settings.move": self.move,
        }

    # -- Gravação -------------------------------------------------------

    def _effective_profile(self, profile, raw):
        return dictation_profile(raw) if profile == FOLLOW_DICTATION else profile

    def recording(self, _params):
        raw = dict(self.settings_getter())
        try:
            settings = resolve_meeting_settings(raw)
        except ValueError as exc:
            raise RpcFailure(tr("As configurações de gravação são inválidas"), detail=str(exc)) from exc
        profiles = [FOLLOW_DICTATION] + [entry["profile"] for entry in selectable_catalog()]
        languages = {profile: [{"id": language, "label": tr(LANGUAGE_LABELS[language])}
                               for language in available_languages(self._effective_profile(profile, raw))]
                     for profile in profiles}
        installed = self._summary_installed()
        return {
            "profiles": [{"id": profile, "label": profile_display_label(profile)} for profile in profiles],
            "languages": languages,
            "summaryModels": [{"id": entry["id"], "label": tr(SUMMARY_LABELS[entry["id"]]),
                               "installed": installed.get(entry["id"], False)} for entry in summary_catalog()],
            "values": {
                "profile": settings.profile_setting,
                "language": settings.language,
                "hotkey": settings.hotkey,
                "summaryModel": settings.summary_model,
                "destination": settings.destination,
                "voiceBoost": bool(settings.voice_boost),
            },
            "destinationLabel": destination_display(settings.destination),
            "microphoneEnabled": bool(settings.input_enabled),
        }

    def save_recording(self, params):
        values = params.get("values")
        if not isinstance(values, dict):
            raise RpcFailure(tr("Pedido inválido."), code=INVALID_PARAMS)
        profile = values.get("profile")
        language = values.get("language")
        summary_model = values.get("summaryModel")
        if profile != FOLLOW_DICTATION and profile not in PROFILE_LABELS:
            raise RpcFailure(tr("Pedido inválido."), code=INVALID_PARAMS)
        if language not in LANGUAGE_LABELS or summary_model not in SUMMARY_LABELS:
            raise RpcFailure(tr("Pedido inválido."), code=INVALID_PARAMS)
        hotkey = _text(values, "hotkey", limit=80).lower()
        destination = _text(values, "destination", limit=4096)
        raw = dict(self.settings_getter())
        if language not in available_languages(self._effective_profile(profile, raw)):
            raise RpcFailure(tr("Este modelo usa detecção automática de idioma. Selecione auto."), code=INVALID_PARAMS)
        if destination and (not os.path.isabs(destination) or not os.path.isdir(destination)):
            raise RpcFailure(tr("Pasta dos arquivos finais: escolha uma pasta existente e absoluta."),
                             code=INVALID_PARAMS)
        payload = {
            "meeting_profile": profile, "meeting_language": language, "meeting_hotkey": hotkey,
            "meeting_summary_model": summary_model, "meeting_destination": destination,
            "meeting_voice_boost": values.get("voiceBoost") is True,
        }
        merged = dict(raw, **payload)
        try:
            validate_hotkey_conflicts(merged)
            resolve_meeting_settings(merged)
        except ValueError as exc:
            raise RpcFailure(str(exc) or tr("Revise as configurações e tente de novo."), code=INVALID_PARAMS) from exc
        if self.persist_settings(payload) is False:
            raise RpcFailure(tr("Não foi possível salvar as configurações. Tente novamente."))
        self.on_settings_changed()
        return self.recording({})

    def choose_destination(self, params):
        initial = params.get("initial") if isinstance(params.get("initial"), str) else ""
        path = self.choose_directory(tr("Pasta padrão das gravações"), initial.strip())
        return {"path": path, "label": destination_display(path) if path else ""}

    # -- Privacidade ----------------------------------------------------

    def _workspace(self):
        try:
            workspace = self.meetings.read_workspace()
        except Exception as exc:
            raise RpcFailure(tr("Privacidade local indisponível"), detail=technical_detail(exc)) from exc
        if not isinstance(workspace, dict):
            raise RpcFailure(tr("Privacidade local indisponível"))
        return workspace

    def privacy(self, _params):
        workspace = self._workspace()
        privacy = workspace.get("privacy_defaults") if isinstance(workspace.get("privacy_defaults"), dict) else {}
        retention = workspace.get("retention_defaults") if isinstance(workspace.get("retention_defaults"), dict) else {}
        notice = privacy.get("recording_notice") if isinstance(privacy.get("recording_notice"), dict) else {}
        whole = retention.get("whole_meeting") if isinstance(retention.get("whole_meeting"), dict) else {}
        days = whole.get("after_days")
        generation = workspace.get("generation", 0)
        return {
            "generation": generation if isinstance(generation, int) and generation >= 0 else 0,
            "noticeEnabled": bool(notice.get("enabled", False)),
            "noticeLanguage": notice.get("language") if notice.get("language") in NOTICE_LANGUAGES else "pt-BR",
            "qaMode": privacy.get("qa_mode") if privacy.get("qa_mode") in {"memory_only", "explicit_save"}
            else "explicit_save",
            "autoTrash": whole.get("mode") == "whole_meeting" and days is not None,
            "autoTrashDays": f"{float(days if days is not None else DEFAULT_AUTO_TRASH_DAYS):g}",
            "trashDays": f"{float(retention.get('trash_days', DEFAULT_TRASH_DAYS)):g}",
        }

    def save_privacy(self, params):
        generation = params.get("generation")
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
            raise RpcFailure(tr("Pedido inválido."), code=INVALID_PARAMS)
        auto = params.get("autoTrash") is True
        days = _policy_days(params.get("autoTrashDays"), field=tr("Reunião"))
        if auto and not days:
            raise RpcFailure(tr("Informe após quantos dias mover gravações para a lixeira."), code=INVALID_PARAMS)
        trash = _policy_days(params.get("trashDays"), field=tr("Lixeira"))
        qa_mode = params.get("qaMode")
        if qa_mode not in {"memory_only", "explicit_save"}:
            raise RpcFailure(tr("Escolha um modo de Q&A válido."), code=INVALID_PARAMS)
        language = params.get("noticeLanguage")
        if language not in NOTICE_LANGUAGES:
            raise RpcFailure(tr("Escolha um idioma válido."), code=INVALID_PARAMS)
        workspace = self._workspace()
        privacy = copy.deepcopy(workspace.get("privacy_defaults") or {})
        notice = privacy.get("recording_notice")
        if not isinstance(notice, dict):
            notice = privacy["recording_notice"] = {}
        notice.update(enabled=params.get("noticeEnabled") is True, language=language)
        privacy["qa_mode"] = qa_mode
        retention = copy.deepcopy(workspace.get("retention_defaults") or {})
        whole = retention.get("whole_meeting")
        whole = copy.deepcopy(whole) if isinstance(whole, dict) else {}
        whole["mode"] = "whole_meeting" if auto else "keep"
        if days is None:
            whole.pop("after_days", None)
        else:
            whole["after_days"] = days
        retention["whole_meeting"] = whole
        # Raw-track retention is no longer user-facing; keep its legacy key inert.
        retention["raw_audio"] = {"mode": "keep", "tracks": []}
        retention["trash_days"] = DEFAULT_TRASH_DAYS if trash is None else trash
        try:
            self.meetings.update_workspace({"privacy_defaults": privacy, "retention_defaults": retention},
                                           expected_generation=generation)
        except Exception as exc:
            raise RpcFailure(tr("As opções anteriores continuam valendo. Salve de novo ou reabra a janela."),
                             detail=technical_detail(exc)) from exc
        self.on_settings_changed()
        return self.privacy({})

    # -- Modelos --------------------------------------------------------

    def _summary_installed(self):
        return {entry["id"]: bool(self.summary_models.summary_model_is_installed(entry["id"]))
                for entry in summary_catalog()}

    def models(self, _params):
        snapshot = self.voice.status_snapshot() if self.voice is not None else {}
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        voice = []
        for entry in selectable_catalog():
            active = bool(snapshot.get("download_active") and snapshot.get("download_profile") == entry["profile"])
            voice.append({
                "id": entry["profile"],
                "label": profile_display_label(entry["profile"]),
                "name": entry["upstream_model"],
                "purpose": tr(entry["purpose"]),
                "size": format_size(entry["size_bytes"]),
                "license": entry["license_id"],
                "installed": (self.voice is not None
                              and bool(voice_models.model_is_installed(entry, self.voice.cache_dir))),
                "downloading": active,
            })
        installed = self._summary_installed()
        with self._lock:
            running = self._summary_download
        summary = [{
            "id": entry["id"], "name": entry["name"], "label": tr(entry["label"]),
            "description": tr(entry["description"]),
            "size": format_model_size(entry["size_bytes"]), "license": entry["license_id"],
            "installed": installed.get(entry["id"], False),
            "downloading": bool(running and running["id"] == entry["id"]),
        } for entry in summary_catalog()]
        return {"voice": voice, "summary": summary,
                "summaryProgress": self._progress_view(running) if running else None}

    def download_voice_model(self, params):
        profile = _text(params, "id", limit=64, required=True)
        entry = next((item for item in selectable_catalog() if item["profile"] == profile), None)
        if entry is None or self.voice is None:
            raise RpcFailure(tr("Pedido inválido."), code=INVALID_PARAMS)
        if params.get("confirmed") is not True:
            return {"status": "confirm", "title": tr("Baixar modelo de voz"), "message": tr(
                "Baixar {purpose} ({size})?\n\nLicença: {license}\n{attribution}\n\n"
                "O arquivo fica no cache local de modelos. A entrada por voz não será ativada automaticamente.",
                purpose=tr(entry["purpose"]), size=format_size(entry["size_bytes"]),
                license=entry["license_id"], attribution=entry["attribution"])}
        if not self.voice.download_profile(profile):
            raise RpcFailure(tr("Aguarde o download atual terminar e tente de novo."))
        return {"status": "started"}

    def cancel_voice_model(self, params):
        profile = _text(params, "id", limit=64, required=True)
        return {"cancelled": bool(self.voice is not None and self.voice.cancel_profile_download(profile))}

    def download_summary_model(self, params):
        model_id = _text(params, "id", limit=64, required=True)
        entry = summary_catalog_entry(model_id)
        if entry is None:
            raise RpcFailure(tr("O modelo de resumo selecionado não existe no catálogo."), code=INVALID_PARAMS)
        if params.get("confirmed") is not True:
            terms = ""
            if entry["requires_acceptance"]:
                terms = tr("\n\n{notice}\n\nTermos completos: {url}"
                           "\n\nAo continuar, você confirma que leu e aceita esses termos.",
                           notice=tr(entry.get("license_notice") or N_("Leia a licença antes de continuar.")),
                           url=entry["license_url"])
            return {"status": "confirm",
                    "title": tr("Licença do modelo") if entry["requires_acceptance"] else tr("Baixar modelo local"),
                    "message": tr("Baixar {name} ({size})?\nLicença: {license}{terms}", name=entry["name"],
                                  size=format_model_size(entry["size_bytes"]), license=entry["license_id"],
                                  terms=terms)}
        with self._lock:
            if self._summary_download is not None:
                raise RpcFailure(tr("Aguarde o download do modelo de resumo terminar."))
            job = self._summary_download = {"id": model_id, "name": entry["name"], "done": 0,
                                            "total": entry["size_bytes"], "cancel": threading.Event(), "pushed": 0.0}
        threading.Thread(target=self._run_summary_download, args=(job,), name="manager-summary-model",
                         daemon=True).start()
        return {"status": "started"}

    @staticmethod
    def _progress_view(job):
        return {"id": job["id"], "done": job["done"], "total": job["total"],
                "status": tr("Baixando {name} com verificação SHA-256…", name=job["name"])}

    def _run_summary_download(self, job):
        def progress(done, total):
            job["done"], job["total"] = done, total or job["total"]
            now = time.monotonic()
            if now - job["pushed"] >= PROGRESS_INTERVAL:
                job["pushed"] = now
                self.notify("models.summaryProgress", self._progress_view(job))

        outcome = {"id": job["id"], "ok": True, "message": tr("{name} baixado e verificado.", name=job["name"])}
        try:
            self.summary_models.download_summary_model(job["id"], cancel_event=job["cancel"], progress=progress)
        except Exception as exc:  # noqa: BLE001 - every outcome must reach the window
            outcome = {"id": job["id"], "ok": False, "cancelled": job["cancel"].is_set(),
                       "message": (tr("Download cancelado; os bytes parciais poderão ser retomados.")
                                   if job["cancel"].is_set() else str(exc) or tr("Não foi possível baixar o modelo.")),
                       "detail": technical_detail(exc)}
        finally:
            with self._lock:
                self._summary_download = None
        self.notify("models.summaryFinished", outcome)

    def cancel_summary_model(self, _params):
        with self._lock:
            job = self._summary_download
        if job is None:
            raise RpcFailure(tr("Nenhum download de modelo está em andamento."))
        job["cancel"].set()
        return {"cancelled": True}

    def delete_summary_model(self, params):
        model_id = _text(params, "id", limit=64, required=True)
        entry = summary_catalog_entry(model_id)
        if entry is None:
            raise RpcFailure(tr("O modelo de resumo selecionado não existe no catálogo."), code=INVALID_PARAMS)
        if params.get("confirmed") is not True:
            return {"status": "confirm", "title": tr("Remover modelo"),
                    "message": tr("Remover {name} deste computador?", name=entry["name"])}
        with self._lock:
            if self._summary_download is not None and self._summary_download["id"] == model_id:
                raise RpcFailure(tr("Aguarde o download do modelo de resumo terminar."))
        try:
            self.summary_models.delete_summary_model(model_id)
        except Exception as exc:
            raise RpcFailure(tr("Não foi possível remover o modelo."), detail=technical_detail(exc)) from exc
        return {"status": "removed", "message": tr("{name} removido.", name=entry["name"])}

    # -- Pastas ---------------------------------------------------------

    def _location(self, params):
        kind = params.get("kind")
        if kind not in self.locations:
            raise RpcFailure(tr("Pedido inválido."), code=INVALID_PARAMS)
        return kind, self.locations[kind]

    def locations_view(self, _params):
        view = {}
        for kind, spec in self.locations.items():
            info = spec["info"]()
            view[kind] = {
                "path": info["path"],
                "locked": bool(info["env_locked"]),
                "atDefault": os.path.normcase(info["path"]) == os.path.normcase(info["default"]),
            }
        return view

    def plan_move(self, params):
        """Choose (or restore) a folder and return the confirmation, without moving anything."""
        kind, spec = self._location(params)
        info = spec["info"]()
        if info["env_locked"]:
            raise RpcFailure(tr("Pedido inválido."), code=INVALID_PARAMS)
        if params.get("restoreDefault") is True:
            target = info["default"]
        else:
            chosen = self.choose_directory(tr(spec["choose_title"]), "")
            if not chosen:
                return {"target": None}
            target = spec["choose"](chosen)
        if kind == "models":
            with self._lock:
                if self._summary_download is not None:
                    raise RpcFailure(tr("Aguarde o download do modelo de resumo terminar."))
        try:
            target = spec["validate"](info["path"], target)
        except data_relocation.RelocationError as exc:
            raise RpcFailure(str(exc)) from exc
        # ceiling: sizes the folder in this request; move it to a job if
        # libraries reach hundreds of thousands of files.
        size = format_size(spec["size"](info["path"]))
        return {
            "target": target,
            "title": tr(spec["title"]),
            "question": tr("{action} ({size}) de\n{current}\npara\n{target}?\n\n"
                           "O SnipVoice será fechado e aberto de novo. Entre discos diferentes, a cópia "
                           "pode levar alguns minutos antes de o ícone voltar à bandeja.",
                           action=tr(spec["action"]), size=size, current=info["path"], target=target),
        }

    def move(self, params):
        """Record the confirmed move; the engine then quits and relaunches."""
        kind, spec = self._location(params)
        target = _text(params, "target", limit=4096, required=True)
        info = spec["info"]()
        try:
            target = spec["validate"](info["path"], target)
        except data_relocation.RelocationError as exc:
            raise RpcFailure(str(exc)) from exc
        reason = spec["request"](target)
        if reason:
            raise RpcFailure(reason)
        return {"moving": True}
