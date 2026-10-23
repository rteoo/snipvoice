"""Pure, bounded projections for meeting readiness and truthful job progress."""

from dataclasses import dataclass
import math

from i18n import tr
from meeting_support import processing_job_label


@dataclass(frozen=True)
class StatusRow:
    key: str
    tone: str
    text: str
    action: str = ""


class ReadinessProbe:
    """One outstanding inventory read, admitted at most once per second.

    The UI supplies the worker. A failed read replaces a previous healthy
    result with an explicit unknown state instead of manufacturing readiness.
    """

    def __init__(self):
        self.next_read = 0.0
        self.pending = False
        self.state = "pending"
        self.profile = None
        self.voice = None

    def admit(self, now):
        if self.pending or now < self.next_read:
            return False
        self.pending = True
        self.next_read = now + 1.0
        return True

    def settle(self, value, error):
        self.pending = False
        if error or not isinstance(value, dict):
            self.state, self.profile, self.voice = "unknown", None, None
            return
        self.profile = value.get("profile")
        self.voice = value.get("voice") if isinstance(value.get("voice"), dict) else None
        self.state = "ready" if isinstance(self.profile, str) and self.profile else "missing"


def recent_attention(items):
    """The attention window is exactly the loaded library page, never a total."""
    return [item for item in items if item.get("status") in {"failed", "partial", "interrupted"}]


def readiness_rows(snapshot, probe, *, summary_state, voice_available, startup_ready,
                   startup_text, attention_count):
    rows = []
    if not startup_ready:
        startup_text = " ".join(str(startup_text or "").split())[:320]
        rows.append(StatusRow("startup", "warning", startup_text or tr("Recuperação local pendente."),
                              tr("Ver detalhes")))
    if probe.state == "pending":
        rows.append(StatusRow("inventory", "text_muted", tr("Verificando modelos locais…")))
    elif probe.state == "unknown":
        rows.append(StatusRow("inventory", "warning", tr("Não foi possível verificar os modelos locais."),
                              tr("Ver modelos")))
    elif probe.state == "missing":
        rows.append(StatusRow("transcription", "warning", tr("Sem modelo de transcrição instalado."),
                              tr("Ver modelos")))
    if summary_state == "pending":
        rows.append(StatusRow("summary", "text_muted", tr("Verificando modelo de resumo…")))
    elif summary_state == "unknown":
        rows.append(StatusRow("summary", "warning", tr("Não foi possível verificar o modelo de resumo."),
                              tr("Ver modelos")))
    elif summary_state == "missing":
        rows.append(StatusRow("summary", "warning", tr("Sem modelo de resumo instalado."), tr("Ver modelos")))
    if snapshot.get("processing"):
        rows.append(StatusRow("job", "info", tr("Em execução: {job}.", job=processing_job_label(snapshot)),
                              tr("Cancelar")))
    if attention_count:
        rows.append(StatusRow("attention", "warning",
                              tr("Gravações com problemas nesta página: {count}", count=attention_count), tr("Ver")))
    if voice_available and isinstance(probe.voice, dict):
        voice = probe.voice
        if not voice.get("enabled"):
            rows.append(StatusRow("dictation", "text_muted", tr("Ditado desligado."), tr("Abrir Ditado")))
        elif voice.get("state") == "unavailable":
            rows.append(StatusRow("dictation", "warning",
                                  tr("Entrada por voz indisponível. Verifique o runtime de transcrição."),
                                  tr("Abrir Ditado")))
        elif voice.get("state") in {"loading", "recording", "transcribing", "routing"}:
            status = " ".join(str(voice.get("status") or tr("Processando")).split())[:160]
            rows.append(StatusRow("dictation", "info", status, tr("Abrir Ditado")))
    if not rows:
        rows.append(StatusRow("healthy", "text_muted", tr("Modelos locais prontos.")))
    return tuple(rows)


def processing_view(snapshot, selected, started, now):
    """Only the selected recording's job; percentage comes solely from export."""
    if not selected or not snapshot.get("processing") or snapshot.get("processing_session") != selected:
        return None
    elapsed = max(0, int(now - started))
    stage = snapshot.get("postprocess") or processing_job_label(snapshot)
    result = {"stage": str(stage or tr("Processando")), "elapsed": elapsed, "percent": None}
    export = snapshot.get("audio_export") or {}
    percent = export.get("percent")
    if export.get("session_id") == selected and export.get("stage") in {"preparing", "encoding", "finalizing"} \
            and isinstance(percent, (int, float)) and not isinstance(percent, bool) and math.isfinite(percent):
        result["percent"] = min(100, max(0, percent))
    return result


def completion_can_select(context, *, session, destination, navigation, section, selection=0):
    return context == (session, destination, navigation, section, selection)
