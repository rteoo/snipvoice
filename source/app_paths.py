"""Independent Snipvoice data paths; never migrate another app's files."""

import os

ENV_HOME = "SNIPVOICE_HOME"


def ensure_data_dir():
    override = os.environ.get(ENV_HOME)
    path = os.path.abspath(os.path.expanduser(override or "~/.snipvoice"))
    os.makedirs(path, exist_ok=True)
    return path
