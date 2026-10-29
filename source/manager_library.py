"""Library actions for the Tauri manager: summary, chat, files and the trash.

``LibraryActions`` adds these JSON-RPC methods to ``ManagerApi``. Summary and
chat jobs hold a bridge worker for minutes, so each takes a cancel event the
window can set. Save and open dialogs run on the engine's Tk thread through
``choose_path``, which keeps a dialog plugin out of the window process.
Engine refusals (``RuntimeError``/``ValueError``) already carry written,
translated text; it becomes the error message with local paths redacted.
"""

import collections
import json
import os
import re
import secrets
import threading

from i18n import tr
from manager_bridge import INVALID_PARAMS, TONES, RpcFailure, _session_id, _text, technical_detail
from meeting_chat import UNCERTAINTY_LABELS, UNCERTAINTY_TONES
from meeting_files import _redact_absolute_paths
from meeting_gui import (
    MAX_ANSWER_CHARS,
    MAX_REPORT_REVIEW_CHARS,
    MAX_TRASH_ITEMS,
    REPORT_HISTORY_LIMIT,
    RETENTION_POLICY_LABELS,
    MeetingWindow,
    _safe_retention_text,
    active_transcript_revision,
    format_time,
    retention_plan_projection,
)
from meeting_intelligence import builtin_profile_description, builtin_profiles
from meeting_text import (
    format_detailed_notes,
    format_expiry,
    format_local_timestamp,
    format_storage_size,
    format_summary,
    seconds_until,
)
from meeting_titles import safe_filename_stem

CANCELLED = -32001
MAX_FOCUS_CHARS = 600
MAX_QUESTION_CHARS = 2000
MAX_CHAT_HISTORY = 24
# Answers stay here until saved; a window keeps at most a few conversations.
MAX_KEPT_ANSWERS = 64
MAX_KEPT_PLANS = 8
MAX_PROFILE_NAME = 128
MAX_PROFILE_INSTRUCTIONS = 2000
PRIMARY_PROFILES =("meeting_notes", "interview", "one_on_one", "sales", "customer_feedback")
AUDIO_PATTERNS = "*.wav *.mp3 *.aac *.m4a *.flac *.ogg *.opus"


def _engine_failure(error, fallback):
    """An RpcFailure that keeps the engine's written refusal visible."""
    if isinstance(error, (RuntimeError, ValueError)) and str(error).strip():
        message = _redact_absolute_paths(" ".join(str(error).split()))[:600]
        return RpcFailure(message, detail=technical_detail(error))
    return RpcFailure(fallback, detail=technical_detail(error))


class LibraryActions:
    """Summary, chat, files and trash over the meetings controller.

    ``meeting_settings()`` returns the saved ``MeetingSettings``;
    ``summary_model_installed(model_id)`` checks the local cache;
    ``choose_path(kind, title, filename, filetypes)`` shows a save
    (``kind="save"``) or open dialog and returns a path or ``""``;
    ``startup_status() -> (ready, message)`` gates destructive work.
    """

    def __init__(self, *, meetings, meeting_settings, summary_model_installed, choose_path, startup_status):
        self.meetings = meetings
        self.meeting_settings = meeting_settings
        self.summary_model_installed = summary_model_installed
        self.choose_path = choose_path
        self.startup_status = startup_status
        self._lock = threading.Lock()
        self._jobs = {}
        self._answers = collections.OrderedDict()
        self._plans = collections.OrderedDict()

    def methods(self):
        return {
            "library.summary": self.summary,
            "library.generateSummary": self.generate_summary,
            "library.versions": self.versions,
            "library.version": self.version,
            "library.reviewSection": self.review_section,
            "library.exportReport": self.export_report,
            "library.saveProfile": self.save_profile,
            "library.setProfileEnabled": self.set_profile_enabled,
            "library.deleteProfile": self.delete_profile,
            "library.ask": self.ask,
            "library.saveAnswer": self.save_answer,
            "library.cancelJob": self.cancel_job,
            "library.files": self.files,
            "library.exportText": self.export_text,
            "library.exportAudio": self.export_audio,
            "library.prepareAudio": self.prepare_audio,
            "library.importAudio": self.import_audio,
            "library.trashPreview": self.trash_preview,
            "library.trash": self.trash,
            "library.restore": self.restore,
            "trash.list": self.trash_list,
            "trash.purge": self.purge,
            "trash.emptyExpired": self.empty_expired,
        }

    # -- Jobs -----------------------------------------------------------

    def _begin(self, kind):
        with self._lock:
            if kind in self._jobs:
                raise RpcFailure(tr("Aguarde a resposta ou o salvamento terminar."))
            event = self._jobs[kind] = threading.Event()
        return event

    def _end(self, kind, event):
        with self._lock:
            if self._jobs.get(kind) is event:
                del self._jobs[kind]

    def cancel_job(self, params):
        kind = params.get("kind")
        if kind not in {"summary", "ask"}:
            raise RpcFailure(tr("Pedido inválido."), code=INVALID_PARAMS)
        with self._lock:
            event = self._jobs.get(kind)
        if event is not None:
            event.set()
        return {"cancelled": event is not None}

    def _run_job(self, kind, work, fallback, cancelled):
        event = self._begin(kind)
        try:
            return work(event)
        except RpcFailure:
            raise
        except Exception as exc:
            if event.is_set():
                raise RpcFailure(cancelled, code=CANCELLED) from exc
            raise _engine_failure(exc, fallback) from exc
        finally:
            self._end(kind, event)

    def _settings(self):
        try:
            return self.meeting_settings()
        except ValueError as exc:
            raise _engine_failure(exc, tr("As configurações de gravação são inválidas")) from exc

    # -- Summary --------------------------------------------------------

    def _language(self, settings):
        return "pt-BR" if settings.language in {"", "auto"} else settings.language

    def _profiles(self, language):
        try:
            profiles = self.meetings.list_report_profiles(language=language)
        except Exception:  # noqa: BLE001 - the built-in formats stay usable, as in Tk
            profiles = None
        clean = [item for item in profiles or () if isinstance(item, dict)] or builtin_profiles(language)
        clean.sort(key=lambda item: PRIMARY_PROFILES.index(item.get("id"))
                   if item.get("id") in PRIMARY_PROFILES else len(PRIMARY_PROFILES))
        return clean[:128]

    @staticmethod
    def _profile_view(profile, language):
        description = ""
        if profile.get("builtin"):
            try:
                description = builtin_profile_description(profile.get("id"), language)
            except ValueError:
                description = ""
        name = str(profile.get("name") or profile.get("id") or tr("Formato"))
        if not profile.get("builtin"):
            name = f"{name} · v{profile.get('version', 1)}"
        return {
            "id": str(profile.get("id") or ""),
            "name": name,
            "description": description or tr("Use seu formato personalizado e ajuste o foco se precisar."),
            "disabled": bool(profile.get("disabled")),
            "custom": not profile.get("builtin"),
            # The edit form starts from the stored name and instructions.
            "baseName": str(profile.get("name") or "")[:MAX_PROFILE_NAME],
            "instructions": str(profile.get("instructions") or "")[:MAX_PROFILE_INSTRUCTIONS],
        }

    def _latest_summary(self, session_id, profiles):
        """The newest summary version, falling back to the session's own summary."""
        reports = self.meetings.list_reports(session_id, limit=REPORT_HISTORY_LIMIT) or []
        rows = [item for item in reports if isinstance(item, dict) and item.get("kind") != "qa"]
        if rows:
            latest = rows[-1]
            report = self.meetings.get_report(session_id, latest.get("id", latest.get("report_id")))
            if isinstance(report, dict):
                merged = dict(report.get("generated") or {})
                reviewed = report.get("reviewed_artifact")
                if isinstance(reviewed, dict) and isinstance(reviewed.get("sections"), dict):
                    merged.update(reviewed["sections"])
                name = next((item.get("name") for item in profiles if item.get("id") == report.get("profile_id")),
                            tr("Resumo"))
                return format_summary(merged), name
        metadata = self.meetings.get_session(session_id) or {}
        return format_summary(metadata.get("summary") or metadata.get("reviewed_summary")), ""

    def summary(self, params):
        session_id = _session_id(params)
        settings = self._settings()
        language = self._language(settings)
        profiles = self._profiles(language)
        try:
            text, name = self._latest_summary(session_id, profiles)
        except Exception as exc:
            raise _engine_failure(exc, tr("Não foi possível carregar as versões do resumo")) from exc
        model_ready = bool(self.summary_model_installed(settings.summary_model))
        try:
            can_save = self.meetings.qa_mode() == "explicit_save"
        except ValueError:
            can_save = False
        default = next((item.get("id") for item in profiles if item.get("id") == "meeting_notes"),
                       profiles[0].get("id") if profiles else "")
        return {
            "text": text[:65536],
            "name": name,
            "profiles": [self._profile_view(item, language) for item in profiles],
            "profileId": default,
            "modelReady": model_ready,
            "canSaveAnswers": can_save,
            "hint": (tr("Resumo salvo. Escolha um formato para gerar outra versão.") if text
                     else tr("O resumo será gerado automaticamente com os modelos instalados.") if model_ready
                     else tr("Instale um modelo de resumo em Configurações → Modelos para gerar automaticamente.")),
        }

    def _transcribed_revision(self, session_id, refusal):
        revision = active_transcript_revision(self.meetings.get_session(session_id) or {})
        if not revision:
            raise RpcFailure(refusal)
        return revision

    def generate_summary(self, params):
        session_id = _session_id(params)
        profile_id = _text(params, "profileId", limit=128, required=True)
        focus = params.get("focus", "")
        if not isinstance(focus, str):
            raise RpcFailure(tr("Pedido inválido."), code=INVALID_PARAMS)
        focus = focus.strip()
        if len(focus) > MAX_FOCUS_CHARS:
            raise RpcFailure(tr("Use um foco adicional de até 600 caracteres."), code=INVALID_PARAMS)
        settings = self._settings()
        profile = next((item for item in self._profiles(self._language(settings)) if item.get("id") == profile_id), None)
        if profile is None:
            raise RpcFailure(tr("Escolha um formato para o resumo."))
        if profile.get("disabled"):
            raise RpcFailure(tr("Ative este formato ou escolha outro antes de gerar."))
        revision = self._transcribed_revision(session_id, tr("Transcreva esta gravação antes de gerar o resumo."))
        if not self.summary_model_installed(settings.summary_model):
            raise RpcFailure(tr("Instale um modelo de resumo em Configurações → Modelos para continuar."))

        def work(event):
            return self.meetings.generate_report(
                session_id, settings.summary_model, profile=dict(profile), revision=revision,
                focus=focus or None, cancel_event=event,
            )

        self._run_job("summary", work, tr("Não foi possível gerar o resumo"), tr("O resumo foi cancelado"))
        return self.summary({"id": session_id})

    # -- Versions and formats ------------------------------------------

    def versions(self, params):
        """Saved summary versions and saved answers, newest first, as the Tk Versões list names them."""
        session_id = _session_id(params)
        profiles = self._profiles(self._language(self._settings()))
        rows = self._engine_call(lambda: self.meetings.list_reports(session_id, limit=REPORT_HISTORY_LIMIT) or [],
                                 tr("Não foi possível carregar as versões do resumo"))
        items = []
        for item in rows:
            metadata = MeetingWindow._report_history_metadata(item)
            if metadata is None:
                continue
            qa = metadata.get("kind") == "qa"
            profile = next((entry.get("name") for entry in profiles if entry.get("id") == metadata.get("profile_id")),
                           tr("Resposta salva") if qa else tr("Resumo"))
            items.append({
                "id": metadata["id"],
                "label": f"{tr('Pergunta') if qa else tr('Relatório')} · {profile} · "
                         f"{format_local_timestamp(metadata.get('created_at'))}",
                "answer": qa,
                "reviewed": bool(metadata.get("reviewed")),
            })
        return {"items": list(reversed(items))}

    def version(self, params):
        """One saved version: its readable text and each section as the review editor shows it."""
        session_id = _session_id(params)
        report_id = _text(params, "reportId", limit=128, required=True)
        report = self._engine_call(lambda: self.meetings.get_report(session_id, report_id),
                                   tr("Não foi possível abrir este relatório"))
        if not isinstance(report, dict):
            raise RpcFailure(tr("Não foi possível abrir este relatório"))
        merged = dict(report.get("generated") or {})
        reviewed = report.get("reviewed_artifact")
        if isinstance(reviewed, dict) and isinstance(reviewed.get("sections"), dict):
            merged.update(reviewed["sections"])
        sections = []
        for name, value in list(merged.items())[:64]:
            if name == "detailed_notes":
                text = format_detailed_notes(value)
            else:
                text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
            sections.append({"name": str(name), "text": str(text)[:MAX_REPORT_REVIEW_CHARS]})
        return {
            "id": report_id,
            "text": format_summary(merged)[:65536],
            "sections": sections,
            # A legacy summary has no sections to review; a new report must be generated.
            "reviewable": report_id != "legacy-summary" and bool(sections),
            "generation": reviewed.get("generation", 0) if isinstance(reviewed, dict) else 0,
        }

    def review_section(self, params):
        session_id = _session_id(params)
        report_id = _text(params, "reportId", limit=128, required=True)
        section = _text(params, "section", limit=128, required=True)
        text = params.get("text")
        generation = params.get("generation")
        if not isinstance(text, str) or not isinstance(generation, int) or isinstance(generation, bool):
            raise RpcFailure(tr("Pedido inválido."), code=INVALID_PARAMS)
        if report_id == "legacy-summary":
            raise RpcFailure(tr("O resumo legado não aceita revisão por seção; gere um relatório novo."))
        if len(text) > MAX_REPORT_REVIEW_CHARS:
            raise RpcFailure(tr("Esta revisão é longa demais para salvar. Exporte a seção completa."))
        self._engine_call(lambda: self.meetings.review_report(session_id, report_id, {section: text},
                                                              expected_generation=generation),
                          tr("O relatório gerado continua intacto. Tente salvar de novo."))
        return self.version({"id": session_id, "reportId": report_id})

    def export_report(self, params):
        session_id = _session_id(params)
        report_id = _text(params, "reportId", limit=128, required=True)
        path = self.choose_path("save", tr("Exportar relatório"),
                                safe_filename_stem(self._title(session_id)) + ".md",
                                (("Markdown", "*.md"), (tr("Texto"), "*.txt"), ("JSON", "*.json")))
        if not path:
            return {"saved": False}
        suffix = os.path.splitext(path)[1].casefold()
        fmt = "json" if suffix == ".json" else "text" if suffix == ".txt" else "markdown"
        self._engine_call(lambda: self.meetings.export_report(session_id, report_id, path, format=fmt),
                          tr("Nenhum arquivo foi gravado. Tente de novo ou escolha outra pasta."))
        return {"saved": True, "name": os.path.basename(path)}

    def _new_profile_id(self, name, profiles):
        stem = re.sub(r"[^a-z0-9_-]+", "-", name.strip().lower()).strip("-")[:64] or "formato"
        existing = {item.get("id") for item in profiles}
        identifier, suffix = stem, 2
        while identifier in existing:
            identifier, suffix = f"{stem}-{suffix}", suffix + 1
        return identifier

    def save_profile(self, params):
        """Create, duplicate or edit a custom format, with the Tk form's limits."""
        mode = params.get("mode")
        if mode not in {"create", "duplicate", "edit"}:
            raise RpcFailure(tr("Pedido inválido."), code=INVALID_PARAMS)
        name, instructions = params.get("name"), params.get("instructions")
        if not isinstance(name, str) or not name.strip() or len(name) > MAX_PROFILE_NAME:
            raise RpcFailure(tr("Nome: use de 1 a 128 caracteres."), code=INVALID_PARAMS)
        if not isinstance(instructions, str) or len(instructions) > MAX_PROFILE_INSTRUCTIONS:
            raise RpcFailure(tr("Instruções: use até 2.000 caracteres."), code=INVALID_PARAMS)
        language = self._language(self._settings())
        profiles = self._profiles(language)
        base = next((item for item in profiles if item.get("id") == params.get("baseId")), None)
        if mode != "create" and base is None:
            raise RpcFailure(tr("Selecione um perfil para duplicar."))
        if mode == "edit" and base.get("builtin"):
            raise RpcFailure(tr("Perfis internos não podem ser editados; duplique um para personalizar."))
        if mode == "create":
            profile = {"id": self._new_profile_id(name, profiles), "name": name.strip(), "instructions": instructions,
                       "sections": list((base or {}).get("sections", ["summary"])), "language": language}
        else:
            profile = dict(base, name=name.strip(), instructions=instructions)
            for key in ("version", "profile_hash", "builtin"):
                profile.pop(key, None)
            if mode == "duplicate":
                profile.update(id=self._new_profile_id(name, profiles), disabled=False)
        self._engine_call(lambda: self.meetings.save_report_profile(profile),
                          tr("O perfil de relatório não foi alterado. Tente salvar de novo."))
        return {"profileId": profile["id"]}

    def _custom_profile(self, params):
        profile_id = _text(params, "profileId", limit=128, required=True)
        profile = next((item for item in self._profiles(self._language(self._settings()))
                        if item.get("id") == profile_id), None)
        if profile is None or profile.get("builtin"):
            raise RpcFailure(tr("Selecione um perfil personalizado."))
        return profile_id

    def set_profile_enabled(self, params):
        profile_id = self._custom_profile(params)
        enabled = params.get("enabled")
        if not isinstance(enabled, bool):
            raise RpcFailure(tr("Pedido inválido."), code=INVALID_PARAMS)
        call = self.meetings.enable_report_profile if enabled else self.meetings.disable_report_profile
        self._engine_call(lambda: call(profile_id),
                          tr("O perfil de relatório não foi alterado. Tente salvar de novo."))
        return {"profileId": profile_id, "enabled": enabled}

    def delete_profile(self, params):
        profile_id = self._custom_profile(params)
        if params.get("confirmed") is not True:
            return {"status": "confirm", "title": tr("Excluir perfil"), "message": tr("Excluir este perfil personalizado?")}
        self._engine_call(lambda: self.meetings.delete_report_profile(profile_id),
                          tr("O perfil de relatório não foi alterado. Tente salvar de novo."))
        return {"status": "removed"}

    # -- Chat -----------------------------------------------------------

    @staticmethod
    def _history(value):
        if not isinstance(value, list):
            raise RpcFailure(tr("Pedido inválido."), code=INVALID_PARAMS)
        turns = []
        for item in value[-MAX_CHAT_HISTORY:]:
            if not isinstance(item, dict):
                raise RpcFailure(tr("Pedido inválido."), code=INVALID_PARAMS)
            question, answer = item.get("question"), item.get("answer")
            if isinstance(question, str) and isinstance(answer, str):
                turns.append({"status": "complete", "question": question, "answer": answer})
        return MeetingWindow._chat_history(turns)

    def ask(self, params):
        session_id = _session_id(params)
        question = params.get("question")
        if not isinstance(question, str) or not question.strip():
            raise RpcFailure(tr("Digite uma pergunta sobre a gravação."), code=INVALID_PARAMS)
        question = question.strip()
        if len(question) > MAX_QUESTION_CHARS:
            raise RpcFailure(tr("Use uma pergunta de até 2.000 caracteres."), code=INVALID_PARAMS)
        history = self._history(params.get("history", []))
        model = self._settings().summary_model
        revision = self._transcribed_revision(session_id, tr("Transcreva esta gravação antes de conversar sobre ela."))
        if not self.summary_model_installed(model):
            raise RpcFailure(tr("O chat responde com um modelo local. Instale um para conversar sobre esta gravação."))

        def work(event):
            return self.meetings.ask_this_meeting(session_id, question, model, revision=revision,
                                                  history=history, cancel_event=event)

        result = self._run_job("ask", work, tr("Não foi possível responder"), tr("Resposta cancelada."))
        if not isinstance(result, dict):
            raise RpcFailure(tr("Resposta não concluída. Tente novamente."))
        answer_id = secrets.token_hex(8)
        with self._lock:
            self._answers[answer_id] = (session_id, question, result, model)
            while len(self._answers) > MAX_KEPT_ANSWERS:
                self._answers.popitem(last=False)
        level = str(result.get("uncertainty") or "").strip().casefold()
        label = UNCERTAINTY_LABELS.get(level)
        citations = result.get("citations")
        return {
            "answerId": answer_id,
            "answer": str(result.get("answer") or "")[:MAX_ANSWER_CHARS],
            "sources": len(citations) if isinstance(citations, list) else 0,
            "uncertainty": tr(label) if label else str(result.get("uncertainty") or "")[:200],
            "uncertaintyTone": TONES.get(UNCERTAINTY_TONES.get(level), "muted"),
        }

    def save_answer(self, params):
        session_id = _session_id(params)
        answer_id = _text(params, "answerId", limit=64, required=True)
        with self._lock:
            kept = self._answers.get(answer_id)
        if kept is None or kept[0] != session_id:
            raise RpcFailure(tr("Nada foi salvo. Tente enviar a pergunta de novo."))
        _session, question, result, model = kept
        try:
            self.meetings.save_answer(session_id, result, model, question=question, revision=result.get("revision"))
        except Exception as exc:
            raise _engine_failure(exc, tr("A resposta não foi salva")) from exc
        with self._lock:
            self._answers.pop(answer_id, None)
        return {"saved": True}

    # -- Files ----------------------------------------------------------

    def _title(self, session_id):
        metadata = self.meetings.get_session(session_id)
        if not isinstance(metadata, dict):
            raise RpcFailure(tr("Gravação não encontrada."))
        return " ".join(str(metadata.get("title") or "").split()) or tr("Sem título")

    def files(self, params):
        session_id = _session_id(params)
        try:
            inventory = self.meetings.audio_inventory(session_id) or {}
        except Exception as exc:
            raise _engine_failure(exc, tr("O áudio desta gravação não está disponível.")) from exc
        final = inventory.get("final")
        raw = any(inventory.get(track) for track in ("microphone", "system"))
        return {
            "final": ({"format": str(inventory.get("final_format") or "MP3"), "size": format_storage_size(final)}
                      if final is not None else None),
            "canPrepare": final is None and raw,
        }

    def _engine_call(self, call, fallback):
        try:
            return call()
        except RpcFailure:
            raise
        except Exception as exc:
            raise _engine_failure(exc, fallback) from exc

    def export_text(self, params):
        session_id = _session_id(params)
        style = params.get("format")
        if style not in {"markdown", "text"}:
            raise RpcFailure(tr("Pedido inválido."), code=INVALID_PARAMS)
        suffix = ".md" if style == "markdown" else ".txt"
        label = "Markdown" if style == "markdown" else tr("Texto")
        path = self.choose_path("save", tr("Exportar transcrição"),
                                safe_filename_stem(self._title(session_id)) + suffix, ((label, "*" + suffix),))
        if not path:
            return {"saved": False}
        self._engine_call(lambda: self.meetings.export(session_id, path, format=style),
                          tr("Não foi possível exportar a transcrição"))
        return {"saved": True, "name": os.path.basename(path)}

    def export_audio(self, params):
        session_id = _session_id(params)
        inventory = self._engine_call(lambda: self.meetings.audio_inventory(session_id) or {},
                                      tr("O áudio desta gravação não está disponível."))
        if inventory.get("final") is None:
            raise RpcFailure(tr("Esta gravação ainda não tem áudio final salvo."))
        extension = "." + (inventory.get("final_format") or "MP3").lower()
        path = self.choose_path("save", tr("Baixar áudio final"),
                                safe_filename_stem(self._title(session_id)) + extension,
                                ((tr("Áudio {format}", format=extension[1:].upper()), "*" + extension),))
        if not path:
            return {"saved": False}
        self._engine_call(lambda: self.meetings.export_final_audio(session_id, path),
                          tr("Não foi possível baixar o áudio final"))
        return {"saved": True, "name": os.path.basename(path)}

    def prepare_audio(self, params):
        session_id = _session_id(params)
        accepted = self._engine_call(lambda: self.meetings.recover_audio(session_id),
                                     tr("Não foi possível iniciar o processamento local"))
        if not accepted:
            raise RpcFailure(tr("Há uma captura ou outro processamento em andamento. Tente de novo depois."))
        return {"accepted": True}

    def import_audio(self, _params):
        settings = self._settings()
        path = self.choose_path("open", tr("Importar áudio"), "",
                                ((tr("Áudio compatível"), AUDIO_PATTERNS), (tr("Todos os arquivos"), "*")))
        if not path:
            return {"id": None}
        session_id = self._engine_call(lambda: self.meetings.import_audio(path, settings),
                                       tr("Nenhuma gravação foi criada. Verifique o arquivo e tente de novo."))
        return {"id": session_id}

    # -- Trash ----------------------------------------------------------

    def _require_ready(self, refusal):
        ready, _message = self.startup_status()
        if not ready:
            raise RpcFailure(refusal)

    @staticmethod
    def _preview_text(projection, title):
        operation = tr(RETENTION_POLICY_LABELS.get(projection.get("operation"), "retenção local"))
        lines = [
            tr("Reunião: {title}", title=_safe_retention_text(title, 160)),
            tr("Operação: {operation} · {status}", operation=operation,
               status=tr("elegível") if projection.get("eligible") else tr("bloqueada")),
            tr("Inventário: {count} alvo(s) · {size} bytes",
               count=projection.get("target_count", 0), size=projection.get("byte_estimate", 0)),
            tr("Recuperação: {mode}", mode=projection.get("recovery_mode", "none")),
        ]
        for label, key in (
            (tr("Motivos"), "reasons"), (tr("Capacidades perdidas"), "lost_capabilities"),
            (tr("Exportações externas preservadas"), "excluded_external_exports"),
            (tr("Alterações na biblioteca"), "canonical_changes"),
        ):
            values = projection.get(key) or []
            if values:
                lines.append(label + ":\n- " + "\n- ".join(values[:8]))
        for target in projection.get("targets", []):
            lines.append(tr("{kind}: {size}", kind=target["kind"], size=format_storage_size(target["bytes"])))
        return "\n".join(lines)[:6000]

    def trash_preview(self, params):
        self._require_ready(tr("A retenção aguarda a recuperação do workspace."))
        session_id = _session_id(params)
        metadata = self.meetings.get_session(session_id)
        if not isinstance(metadata, dict):
            raise RpcFailure(tr("Gravação não encontrada."))
        if metadata.get("status") == "recording":
            raise RpcFailure(tr("Finalize a captura antes de excluir esta gravação."))
        plan = self._engine_call(
            lambda: self.meetings.retention_plan(session_id, policy={"mode": "whole_meeting", "after_days": 0}),
            tr("Nenhuma exclusão foi tentada."))
        projection = retention_plan_projection(plan)
        title = " ".join(str(metadata.get("title") or "").split()) or tr("Sem título")
        details = self._preview_text(projection, title)
        if not projection.get("eligible"):
            return {"eligible": False, "reasons": "; ".join(projection.get("reasons", [])[:3]), "details": details}
        token = secrets.token_hex(8)
        with self._lock:
            self._plans[token] = (session_id, plan)
            while len(self._plans) > MAX_KEPT_PLANS:
                self._plans.popitem(last=False)
        duration = metadata.get("duration")
        duration = (format_time(duration) if isinstance(duration, (int, float)) and not isinstance(duration, bool)
                    and duration >= 0 else tr("Duração desconhecida"))
        return {
            "eligible": True,
            "token": token,
            "question": tr(
                "Mover “{title}” para a lixeira?\n\nDuração: {duration} · Tamanho: {size}\n"
                "Você poderá restaurar esta gravação por {days} dias. Cópias exportadas ficam preservadas.",
                title=_safe_retention_text(title, 400), duration=duration,
                size=format_storage_size(projection["byte_estimate"]),
                days=f"{float(projection['purge_after_days']):g}",
            ),
            "details": details,
        }

    def trash(self, params):
        self._require_ready(tr("A retenção aguarda a recuperação do workspace."))
        token = _text(params, "token", limit=64, required=True)
        with self._lock:
            kept = self._plans.pop(token, None)
        if kept is None:
            raise RpcFailure(tr("A prévia ficou inválida ou a retenção falhou. Revise e gere uma nova prévia."))
        session_id, plan = kept
        applied = self._engine_call(lambda: self.meetings.apply_retention(plan, confirm=True),
                                    tr("A prévia ficou inválida ou a retenção falhou. Revise e gere uma nova prévia."))
        if applied is False:
            raise RpcFailure(tr("A prévia ficou inválida ou a retenção falhou. Revise e gere uma nova prévia."))
        return {"id": session_id}

    def restore(self, params):
        session_id = _session_id(params)
        restored = self._engine_call(lambda: self.meetings.restore_session(session_id),
                                     tr("Abra a lixeira para tentar novamente."))
        if restored is False:
            raise RpcFailure(tr("Não foi possível restaurar a gravação"))
        return {"id": session_id}

    def trash_list(self, _params):
        self._require_ready(tr("A lixeira aguarda a recuperação do workspace."))
        entries = self._engine_call(self.meetings.list_trash, tr("Atualize a lixeira para tentar de novo."))
        items = []
        for entry in list(entries or ())[:MAX_TRASH_ITEMS]:
            get = entry.get if isinstance(entry, dict) else (lambda key, default=None, item=entry: getattr(item, key, default))
            session_id = str(get("session_id", ""))[:128]
            if not session_id:
                continue
            purge_after = _safe_retention_text(get("purge_after", ""), 40)
            remaining = seconds_until(purge_after)
            items.append({
                "id": session_id,
                "name": MeetingWindow._trash_name(session_id),
                "moved": format_local_timestamp(_safe_retention_text(get("deleted_at", ""), 40)),
                "expires": format_expiry(purge_after),
                "size": format_storage_size(max(0, int(get("byte_estimate", 0) or 0))),
                "expired": remaining is not None and remaining <= 0,
            })
        expired = sum(1 for item in items if item["expired"])
        if not items:
            summary = tr("A lixeira está vazia")
        else:
            parts = [tr("1 gravação na lixeira") if len(items) == 1
                     else tr("{count} gravações na lixeira", count=len(items))]
            parts.append(tr("nenhuma expirada") if not expired
                         else tr("1 expirada") if expired == 1 else tr("{count} expiradas", count=expired))
            summary = " · ".join(parts)
        return {"items": items, "summary": summary, "expired": expired}

    def purge(self, params):
        self._require_ready(tr("A lixeira aguarda a recuperação do workspace."))
        session_id = _session_id(params)
        purged = self._engine_call(lambda: self.meetings.purge_session(session_id, confirm=True),
                                   tr("Atualize a lista para ver o estado atual."))
        if purged is False:
            raise RpcFailure(tr("A lixeira não confirmou a ação"))
        return {"id": session_id}

    def empty_expired(self, _params):
        self._require_ready(tr("A lixeira aguarda a recuperação do workspace."))
        emptied = self._engine_call(lambda: self.meetings.empty_trash(confirm=True),
                                    tr("Atualize a lista para ver o estado atual."))
        if emptied is False:
            raise RpcFailure(tr("A lixeira não confirmou a ação"))
        return {"emptied": True}
