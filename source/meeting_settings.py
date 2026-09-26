"""Validated recording settings, independent of push-to-talk preferences."""

import os
from dataclasses import dataclass

from voice_catalog import DEFAULT_PROFILE, LANGUAGE_AUTO, is_selectable_profile, is_known_language
from voice_hotkey import parse_chord
from summary_catalog import DEFAULT_SUMMARY_MODEL, is_known_summary_model

SOURCES = ("both", "microphone", "system")
MAX_DESTINATION_LENGTH = 4096
_MISSING = object()
LEGACY_SUMMARY_MODELS = {
    "qwen3-1.7b-q4": DEFAULT_SUMMARY_MODEL,
    "granite-3.3-2b-q4": "lfm2.5-2.6b-q4",
    "granite-4.0-1b-q4": "lfm2.5-2.6b-q4",
    "granite-4.2-3b-q4": "lfm2.5-2.6b-q4",
    "gemma-3-1b-q4": "gemma-4-e2b-q4",
}


@dataclass(frozen=True)
class EndpointSelection:
    mode: str = "default"
    endpoint_id: str = ""
    default_role: str = "multimedia"

    def payload(self):
        return {"mode": self.mode, "endpoint_id": self.endpoint_id,
                "default_role": self.default_role}

    def argument(self):
        return self.endpoint_id if self.mode == "manual" else "default:" + self.default_role


def resolve_selection(value):
    if not isinstance(value, dict):
        return EndpointSelection()
    role = value.get("default_role", "multimedia")
    if role not in ("multimedia", "communications"):
        role = "multimedia"
    if value.get("mode") != "manual":
        return EndpointSelection(default_role=role)
    endpoint = value.get("endpoint_id")
    if (not isinstance(endpoint, str) or not endpoint.strip()
            or len(endpoint) > 1024 or any(ord(c) < 32 for c in endpoint)):
        raise ValueError("Selecione novamente o dispositivo de áudio manual.")
    return EndpointSelection("manual", endpoint, role)


def _resolve_bool(value, name, default=False):
    """Resolve persisted switches without treating strings as truthy values."""
    if value is _MISSING:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"A opção {name} deve ser verdadeira ou falsa.")
    return value


def _resolve_destination(value):
    """Normalize a destination lexically; never inspect or create it."""
    if value is _MISSING:
        return ""
    if not isinstance(value, str):
        raise ValueError("A pasta de gravações é inválida.")
    destination = value.strip()
    if not destination:
        return ""
    if (len(destination) > MAX_DESTINATION_LENGTH
            or any(ord(char) < 32 for char in destination)
            or not os.path.isabs(destination)):
        raise ValueError("A pasta de gravações é inválida.")
    try:
        # normpath is deliberately lexical: the caller is responsible for
        # deciding whether the destination exists and for creating it later.
        return os.path.normpath(destination)
    except (OSError, ValueError):
        raise ValueError("A pasta de gravações é inválida.")


@dataclass(frozen=True)
class MeetingSettings:
    sources: str = "both"
    microphone: EndpointSelection = EndpointSelection()
    system: EndpointSelection = EndpointSelection()
    hotkey: str = ""
    profile: str = DEFAULT_PROFILE
    language: str = LANGUAGE_AUTO
    summary_model: str = DEFAULT_SUMMARY_MODEL
    destination: str = ""
    input_enabled: bool = True
    output_enabled: bool = True
    auto_transcribe: bool = False
    auto_summary: bool = False
    voice_boost: bool = True

    def payload(self):
        return {"meeting_sources": self.sources,
                "meeting_microphone": self.microphone.payload(),
                "meeting_system": self.system.payload(), "meeting_hotkey": self.hotkey,
                "meeting_profile": self.profile, "meeting_language": self.language,
                "meeting_summary_model": self.summary_model,
                "meeting_destination": self.destination,
                "meeting_input_enabled": self.input_enabled,
                "meeting_output_enabled": self.output_enabled,
                "meeting_auto_transcribe": self.auto_transcribe,
                "meeting_auto_summary": self.auto_summary,
                "meeting_voice_boost": self.voice_boost}


def resolve_meeting_settings(value):
    data = value if isinstance(value, dict) else {}
    sources = data.get("meeting_sources", "both")
    if sources not in SOURCES:
        raise ValueError("Escolha microfone, áudio do sistema ou ambos.")
    hotkey = data.get("meeting_hotkey", "")
    if not isinstance(hotkey, str):
        raise ValueError("O atalho de gravação é inválido.")
    hotkey = parse_chord(hotkey).spec if hotkey.strip() else ""
    profile = data.get("meeting_profile", DEFAULT_PROFILE)
    if not is_selectable_profile(profile):
        raise ValueError("Selecione um modelo local disponível para gravações.")
    language = data.get("meeting_language", LANGUAGE_AUTO)
    if not is_known_language(language):
        raise ValueError("Selecione um idioma de transcrição válido.")
    model = data.get("meeting_summary_model", DEFAULT_SUMMARY_MODEL)
    # Migrate previous built-in IDs and former free-form Ollama settings.
    if isinstance(model, str):
        model = LEGACY_SUMMARY_MODELS.get(model, model)
    if not isinstance(model, str) or not is_known_summary_model(model):
        model = DEFAULT_SUMMARY_MODEL
    destination = _resolve_destination(data.get("meeting_destination", _MISSING))

    # New settings persist independent source switches. Older settings only
    # had meeting_sources, so migrate that enum without changing selection
    # semantics or manual endpoint identifiers.
    source_toggles = {
        "both": (True, True), "microphone": (True, False),
        "system": (False, True),
    }
    default_input, default_output = source_toggles[sources]
    input_value = data.get("meeting_input_enabled", _MISSING)
    output_value = data.get("meeting_output_enabled", _MISSING)
    input_enabled = _resolve_bool(input_value, "de entrada", default_input)
    output_enabled = _resolve_bool(output_value, "de saída", default_output)
    # Accept the track-oriented spelling used by early local recorder builds.
    if "meeting_microphone_enabled" in data and "meeting_input_enabled" not in data:
        input_enabled = _resolve_bool(data["meeting_microphone_enabled"],
                                      "de entrada", default_input)
    if "meeting_system_enabled" in data and "meeting_output_enabled" not in data:
        output_enabled = _resolve_bool(data["meeting_system_enabled"],
                                       "de saída", default_output)
    if not input_enabled and not output_enabled:
        raise ValueError("Ative o microfone, o áudio do sistema ou ambos.")
    sources = ("both" if input_enabled and output_enabled else
               "microphone" if input_enabled else "system")

    auto_transcribe = _resolve_bool(data.get("meeting_auto_transcribe", _MISSING),
                                    "de transcrição automática")
    auto_summary = _resolve_bool(data.get("meeting_auto_summary", _MISSING),
                                 "de resumo automático")
    if auto_summary and not auto_transcribe:
        raise ValueError("O resumo automático depende da transcrição automática.")
    voice_boost = _resolve_bool(data.get("meeting_voice_boost", _MISSING),
                                "de reforço do microfone", default=True)
    return MeetingSettings(sources, resolve_selection(data.get("meeting_microphone")),
                           resolve_selection(data.get("meeting_system")), hotkey,
                           profile, language, model, destination, input_enabled,
                           output_enabled, auto_transcribe, auto_summary, voice_boost)


def validate_hotkey_conflicts(settings):
    """Separate selective listeners must never suppress overlapping voice chords."""
    meeting = resolve_meeting_settings(settings)
    if not meeting.hotkey:
        return
    chord = parse_chord(meeting.hotkey)
    from voice_hotkey import DEFAULT_DICTATION_HOTKEY, DEFAULT_COMMAND_HOTKEY
    for name, default in (("voice_hotkey", DEFAULT_DICTATION_HOTKEY),
                          ("voice_command_hotkey", DEFAULT_COMMAND_HOTKEY)):
        other = parse_chord(settings.get(name, default))
        if chord.key == other.key and (chord.modifiers <= other.modifiers
                                       or other.modifiers <= chord.modifiers):
            raise ValueError("O atalho de gravação se sobrepõe a um atalho de ditado ou comando.")
