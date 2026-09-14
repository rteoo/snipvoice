"""Load, normalize, and save the optional user settings file (settings.json).

Settings are entirely optional; a missing or unreadable file yields an empty
dict so the app always has working defaults. Writes are atomic.
"""

import math

from snippet_utils import load_json_file, write_json_atomic


RUNTIME_SETTING_DEFAULTS = {
    "terminator_mode": False,
    "bcb_timeout": 3,
    "bcb_cache_seconds": 300,
    "stock_cache_seconds": 600,
    "mirror_dir": None,
    "sync_export_dir": None,
}


def load_settings(path):
    """Return the settings dict, or an empty dict when missing/invalid."""
    try:
        data = load_json_file(path)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def normalize_runtime_settings(settings):
    """Return a runtime-safe copy and invalid known keys with their defaults.

    The loader deliberately preserves unknown keys and raw value types so a
    newer build never destroys settings written by an older one. This boundary
    normalizes only values consumed by the core runtime; malformed known values
    fall back instead of changing behavior through Python truthiness or raising
    later in a cache comparison.
    """
    normalized = dict(settings) if isinstance(settings, dict) else {}
    invalid = {}

    if "terminator_mode" in normalized and not isinstance(
        normalized["terminator_mode"], bool
    ):
        invalid["terminator_mode"] = RUNTIME_SETTING_DEFAULTS["terminator_mode"]

    numeric_rules = {
        "bcb_timeout": lambda value: value > 0,
        "bcb_cache_seconds": lambda value: value >= 0,
        "stock_cache_seconds": lambda value: value >= 0,
    }
    for key, predicate in numeric_rules.items():
        if key not in normalized:
            continue
        value = normalized[key]
        valid = (
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(value)
            and predicate(value)
        )
        if not valid:
            invalid[key] = RUNTIME_SETTING_DEFAULTS[key]

    for key in ("mirror_dir", "sync_export_dir"):
        if key not in normalized:
            continue
        value = normalized[key]
        if value is None:
            continue
        if not isinstance(value, str):
            invalid[key] = RUNTIME_SETTING_DEFAULTS[key]
        else:
            normalized[key] = value.strip() or None

    for key, default in invalid.items():
        normalized[key] = default
    return normalized, invalid


def save_settings(path, settings):
    """Persist settings atomically. Returns True on success."""
    try:
        write_json_atomic(path, settings)
        return True
    except Exception:
        return False
