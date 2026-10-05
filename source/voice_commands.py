"""Validate and atomically persist the private literal voice-command library."""

from i18n import tr
from snippet_utils import load_json_file, write_json_atomic


class CommandConflictError(ValueError):
    """The on-disk library changed after the editor opened."""


def validate_commands(commands):
    """Keep the existing commands.json schema, including hand-edited libraries."""
    if not isinstance(commands, dict) or any(
        not isinstance(phrase, str) or not phrase.strip() or phrase.startswith("_")
        or not isinstance(text, str) for phrase, text in commands.items()
    ):
        raise ValueError("Expected spoken phrases and literal text")
    return dict(commands)


def load_commands(path):
    try:
        return validate_commands(load_json_file(path))
    except FileNotFoundError:
        return {}


def save_commands(path, commands, expected):
    """Refuse stale drafts or malformed existing files before replacing the library."""
    checked = validate_commands(commands)
    if load_commands(path) != expected:
        raise CommandConflictError(tr("Os comandos foram alterados fora deste editor. Feche, recarregue os comandos e reabra o editor."))
    write_json_atomic(path, checked)


def validate_command(phrase, text, commands, original=None):
    """Normalize an edited phrase for exact spoken matching; preserve literal text."""
    phrase = " ".join(phrase.split()).lower() if isinstance(phrase, str) else ""
    # ceiling: editor phrases are 160 characters and inserted text is 100,000;
    # raise these bounds only when a real command needs a larger editor payload.
    if not phrase or phrase.startswith("_") or len(phrase) > 160:
        raise ValueError(tr("Use uma frase de até 160 caracteres, sem começar com sublinhado."))
    if not isinstance(text, str) or not text.strip() or len(text) > 100_000:
        raise ValueError(tr("Informe um texto de até 100.000 caracteres para inserir."))
    if any(
        key != original and " ".join(key.split()).lower() == phrase
        for key in commands
    ):
        raise ValueError(tr("Já existe um comando com essa frase."))
    return phrase, text
