"""Shared local-model library layout and settings validation.

The selected root is intentionally app-agnostic. Snipvoice owns only its
catalog directories below the category folders and may reuse, but never delete,
verified matching model files managed by another local application.
"""

import os


MODEL_LIBRARY_SETTING = "local_models_root"
MODEL_CATEGORIES = ("llm", "tts", "asr")
MAX_MODEL_ROOT_LENGTH = 4096


def normalize_model_library_root(value):
    """Return one absolute normalized root, or ``None`` for app defaults."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("A pasta compartilhada de modelos é inválida.")
    value = value.strip()
    if not value:
        return None
    expanded = os.path.expanduser(value)
    if (len(expanded) > MAX_MODEL_ROOT_LENGTH
            or any(ord(char) < 32 for char in expanded)
            or not os.path.isabs(expanded)):
        raise ValueError("Escolha uma pasta absoluta para os modelos locais.")
    try:
        return os.path.normpath(expanded)
    except (OSError, ValueError) as exc:
        raise ValueError("A pasta compartilhada de modelos é inválida.") from exc


def resolve_model_library_root(settings, warnings=None):
    """Resolve a saved root without allowing malformed JSON to break startup."""
    data = settings if isinstance(settings, dict) else {}
    try:
        return normalize_model_library_root(data.get(MODEL_LIBRARY_SETTING))
    except ValueError as exc:
        if warnings is not None:
            warnings.append(str(exc))
        return None


def model_category_dir(root, category):
    """Return the conventional category directory below a shared root."""
    if category not in MODEL_CATEGORIES:
        raise ValueError("Categoria de modelo local inválida.")
    normalized = normalize_model_library_root(root)
    if normalized is None:
        return None
    return os.path.join(normalized, category)


def ensure_model_library(root):
    """Create the small stable category layout after an explicit settings save."""
    normalized = normalize_model_library_root(root)
    if normalized is None:
        return ()
    created = []
    for category in MODEL_CATEGORIES:
        path = model_category_dir(normalized, category)
        os.makedirs(path, exist_ok=True)
        created.append(path)
    return tuple(created)


def model_library_payload(root):
    """Return the settings fragment for the selected root."""
    return {MODEL_LIBRARY_SETTING: normalize_model_library_root(root) or ""}
