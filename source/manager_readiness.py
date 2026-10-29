"""The Gravação readiness strip for the Tauri manager.

``ReadinessActions.readiness`` reads the same facts the Tk strip does (startup
recovery, installed transcription and summary models, the running job,
recordings needing attention, dictation state) and renders them with
``ops_status.readiness_rows``, so both windows say the same thing.
"""

from i18n import tr
from manager_bridge import TONES
from meeting_settings import FOLLOW_DICTATION, dictation_profile
from ops_status import ReadinessProbe, readiness_rows, recent_attention
from summary_catalog import summary_catalog

# The attention count covers the library's first page, as the Tk strip does.
ATTENTION_PAGE = 50


class ReadinessActions:
    """``meetings``, ``voice``, ``summary_models`` (module), ``settings_getter``,
    ``startup_status() -> (ready, message)`` and ``voice_status() -> dict``."""

    def __init__(self, *, meetings, voice, summary_models, settings_getter, startup_status, voice_status):
        self.meetings = meetings
        self.voice = voice
        self.summary_models = summary_models
        self.settings_getter = settings_getter
        self.startup_status = startup_status
        self.voice_status = voice_status

    def methods(self):
        return {"recording.readiness": self.readiness}

    def readiness(self, _params):
        settings = self.settings_getter()
        preferred = settings.get("meeting_profile", FOLLOW_DICTATION)
        if preferred == FOLLOW_DICTATION:
            preferred = dictation_profile(settings)
        probe = ReadinessProbe()
        try:
            probe.settle({"profile": self.meetings.installed_transcription_profile(preferred),
                          "voice": self.voice_status() if self.voice is not None else None}, None)
        except Exception:  # noqa: BLE001 - the strip says "could not check", never "ready"
            probe.settle(None, "failed")
        try:
            summary = "ready" if any(self.summary_models.summary_model_is_installed(entry["id"])
                                     for entry in summary_catalog()) else "missing"
        except Exception:  # noqa: BLE001 - as above
            summary = "unknown"
        try:
            page = self.meetings.list_sessions_page(limit=ATTENTION_PAGE, cursor=None, query="", status="")
            attention = recent_attention(page.get("items", ()) if isinstance(page, dict) else ())
        except Exception:  # noqa: BLE001 - a library read failure leaves attention uncounted
            attention = []
        ready, message = self.startup_status()
        rows = readiness_rows(self.meetings.snapshot() or {}, probe, summary_state=summary,
                              voice_available=self.voice is not None, startup_ready=ready,
                              startup_text=tr(message) if message else "", attention_count=len(attention))
        return {
            "rows": [{"key": row.key, "tone": TONES.get(row.tone, "muted"),
                      "text": row.text, "action": row.action} for row in rows],
            "attentionId": attention[0].get("id") if attention else None,
        }
