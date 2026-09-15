"""Resolve optional voice keys from settings.json.

A missing or malformed value falls back to the documented default so a
hand-edited file cannot take the listener or the tray down.
"""

from voice_catalog import (
    DEFAULT_PROFILE,
    LANGUAGE_AUTO,
    default_language_for_profile,
    is_known_language,
    is_selectable_profile,
)
from voice_hotkey import DEFAULT_COMMAND_HOTKEY, DEFAULT_DICTATION_HOTKEY, parse_chord
from voice_text_support import validate_replacements


class VoiceSettings:
    """Normalized voice configuration used by the controller."""

    __slots__ = (
        "enabled",
        "profile",
        "language",
        "hotkey",
        "command_hotkey",
        "cache_dir",
        "voice_replacements",
    )

    def __init__(
        self,
        enabled,
        profile,
        language,
        hotkey,
        command_hotkey,
        cache_dir,
        voice_replacements=None,
    ):
        self.enabled = enabled
        self.profile = profile
        self.language = language
        self.hotkey = hotkey
        self.command_hotkey = command_hotkey
        self.cache_dir = cache_dir
        self.voice_replacements = voice_replacements or {}


def _as_bool(value, default=False):
    if isinstance(value, bool):
        return value
    return default


def _as_optional_dir(value):
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def resolve_voice_settings(settings, warnings=None):
    """Return VoiceSettings, appending human-readable warnings when given."""
    data = settings if isinstance(settings, dict) else {}
    notes = warnings if warnings is not None else []

    enabled = _as_bool(data.get("voice_enabled", False))

    profile = data.get("voice_profile", DEFAULT_PROFILE)
    if not is_selectable_profile(profile):
        if profile != DEFAULT_PROFILE:
            notes.append(
                f"voice_profile {profile!r} ainda não está liberado; "
                f"usando {DEFAULT_PROFILE}."
            )
        profile = DEFAULT_PROFILE

    language = data.get("voice_language", LANGUAGE_AUTO)
    if not is_known_language(language):
        notes.append(f"voice_language inválido ({language!r}); usando {LANGUAGE_AUTO}.")
        language = LANGUAGE_AUTO
    language = default_language_for_profile(profile, language)

    hotkey = data.get("voice_hotkey", DEFAULT_DICTATION_HOTKEY)
    try:
        parsed = parse_chord(hotkey)
        hotkey = parsed.spec
    except ValueError:
        notes.append(f"voice_hotkey inválido ({hotkey!r}); usando {DEFAULT_DICTATION_HOTKEY}.")
        hotkey = DEFAULT_DICTATION_HOTKEY

    command_hotkey = data.get("voice_command_hotkey", DEFAULT_COMMAND_HOTKEY)
    try:
        parsed_command = parse_chord(command_hotkey)
        command_hotkey = parsed_command.spec
    except ValueError:
        notes.append(
            f"voice_command_hotkey inválido ({command_hotkey!r}); "
            f"usando {DEFAULT_COMMAND_HOTKEY}."
        )
        command_hotkey = DEFAULT_COMMAND_HOTKEY

    if parse_chord(command_hotkey) == parse_chord(hotkey):
        notes.append(
            "voice_command_hotkey colide com voice_hotkey; "
            "comando recebe um atalho distinto."
        )
        for candidate in (DEFAULT_COMMAND_HOTKEY, "ctrl+shift+space"):
            if parse_chord(candidate) != parse_chord(hotkey):
                command_hotkey = parse_chord(candidate).spec
                break

    raw_replacements = data.get("voice_replacements", {})
    voice_replacements = validate_replacements(raw_replacements)
    if (
        "voice_replacements" in data
        and raw_replacements != {}
        and not voice_replacements
    ):
        notes.append("voice_replacements inválido; usando nenhuma correção.")

    return VoiceSettings(
        enabled=enabled,
        profile=profile,
        language=language,
        hotkey=hotkey,
        command_hotkey=command_hotkey,
        cache_dir=_as_optional_dir(data.get("voice_cache_dir")),
        voice_replacements=voice_replacements,
    )


def voice_settings_payload(voice_settings):
    """Subset written back into settings.json when the tray toggles voice."""
    return {
        "voice_enabled": bool(voice_settings.enabled),
        "voice_profile": voice_settings.profile,
        "voice_language": voice_settings.language,
        "voice_hotkey": voice_settings.hotkey,
        "voice_command_hotkey": voice_settings.command_hotkey,
        "voice_replacements": dict(voice_settings.voice_replacements),
    }
