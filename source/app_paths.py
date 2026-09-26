"""Independent Snipvoice data paths; never migrate another app's files.

The data directory is user-movable. Its location is recorded in a small
pointer file under a fixed per-OS config directory, because the pointer has to
survive the move it describes. ``SNIPVOICE_HOME`` still wins over the pointer.
"""

import json
import os

from i18n import tr
from snippet_utils import write_json_atomic

ENV_HOME = "SNIPVOICE_HOME"
DEFAULT_HOME = "~/.snipvoice"
LOCATION_NAME = "location.json"


def config_dir(system=None):
    """Fixed, non-roaming folder for the location pointer and instance lock.

    Windows: ``%LOCALAPPDATA%\\Snipvoice``
    macOS: ``~/Library/Application Support/Snipvoice``
    Linux: ``$XDG_CONFIG_HOME/snipvoice`` or ``~/.config/snipvoice``
    """
    from platform_support import current_os

    os_name = system or current_os()
    home = os.path.expanduser("~")
    if os_name == "windows":
        root = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        return os.path.join(root, "Snipvoice")
    if os_name == "darwin":
        return os.path.join(home, "Library", "Application Support", "Snipvoice")
    root = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    return os.path.join(root, "snipvoice")


def location_file():
    return os.path.join(config_dir(), LOCATION_NAME)


def default_models_dir(system=None):
    """Default root holding the ``voice-models`` and ``summary-models`` caches."""
    from platform_support import current_os

    os_name = system or current_os()
    home = os.path.expanduser("~")
    if os_name == "windows":
        root = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        return os.path.join(root, "Snipvoice")
    if os_name == "darwin":
        return os.path.join(home, "Library", "Caches", "Snipvoice")
    return os.path.join(home, ".cache", "snipvoice")


def configured_models_dir():
    """The model root the user chose, or the per-OS default."""
    chosen = read_location().get("models_dir")
    if isinstance(chosen, str) and os.path.isabs(chosen):
        return os.path.abspath(chosen)
    return default_models_dir()


def default_data_dir():
    return os.path.abspath(os.path.expanduser(DEFAULT_HOME))


def env_data_dir():
    """Return the ``SNIPVOICE_HOME`` override, or None when unset."""
    override = os.environ.get(ENV_HOME)
    return os.path.abspath(os.path.expanduser(override)) if override else None


def read_location():
    """Return the pointer contents; a missing or malformed file reads as empty."""
    try:
        with open(location_file(), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_location(data):
    os.makedirs(config_dir(), exist_ok=True)
    write_json_atomic(location_file(), data)


def configured_data_dir():
    """The data directory the user chose, before checking that it is reachable."""
    override = env_data_dir()
    if override:
        return override
    chosen = read_location().get("data_dir")
    if isinstance(chosen, str) and os.path.isabs(chosen):
        return os.path.abspath(chosen)
    return default_data_dir()


def resolve_data_dir():
    """Return ``(path, warning)`` for this session.

    A chosen folder on a drive that is currently missing falls back to the
    default for this session only; the pointer is left unchanged so the next
    start uses the chosen folder again once it is reachable.
    """
    path = configured_data_dir()
    try:
        os.makedirs(path, exist_ok=True)
        return path, ""
    except OSError:
        if env_data_dir() or path == default_data_dir():
            raise
    fallback = default_data_dir()
    os.makedirs(fallback, exist_ok=True)
    return fallback, tr(
        "A pasta de dados {path} está indisponível. O SnipVoice está usando "
        "{fallback} nesta sessão.",
        path=path, fallback=fallback,
    )


def ensure_data_dir():
    return resolve_data_dir()[0]
