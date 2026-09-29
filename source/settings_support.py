"""Load and save the optional user settings file (settings.json).

Settings are entirely optional; a missing or unreadable file yields an empty
dict so the app always has working defaults. Writes are atomic.
"""

from snippet_utils import load_json_file, write_json_atomic


def load_settings(path):
    """Return the settings dict, or an empty dict when missing/invalid."""
    try:
        data = load_json_file(path)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save_settings(path, settings):
    """Persist settings atomically. Returns True on success."""
    try:
        write_json_atomic(path, settings)
        return True
    except Exception:
        return False
