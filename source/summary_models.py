"""Verified, on-demand cache for local summary GGUF files."""

import os

from summary_catalog import summary_catalog_entry
from voice_models import delete_model, download_model, installed_model_path, model_is_installed


ENV_SUMMARY_CACHE = "SNIPVOICE_SUMMARY_CACHE"
CACHE_DIR_NAME = "summary-models"


def default_summary_cache_dir(system=None):
    override = os.environ.get(ENV_SUMMARY_CACHE)
    if override:
        return os.path.abspath(os.path.expanduser(override))
    from platform_support import current_os
    os_name = system or current_os()
    home = os.path.expanduser("~")
    if os_name == "windows":
        root = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        return os.path.join(root, "Snipvoice", CACHE_DIR_NAME)
    if os_name == "darwin":
        return os.path.join(home, "Library", "Caches", "Snipvoice", CACHE_DIR_NAME)
    return os.path.join(home, ".cache", "snipvoice", CACHE_DIR_NAME)


def _entry(model_id):
    entry = summary_catalog_entry(model_id)
    if entry is None:
        raise ValueError("Selecione um modelo de resumo do catálogo do Snipvoice.")
    return entry


def summary_model_is_installed(model_id, cache_dir=None):
    return model_is_installed(_entry(model_id), cache_dir or default_summary_cache_dir())


def summary_model_path(model_id, cache_dir=None):
    return installed_model_path(_entry(model_id), cache_dir or default_summary_cache_dir())


def download_summary_model(model_id, cache_dir=None, progress=None, cancel_event=None, opener=None):
    return download_model(_entry(model_id), cache_dir or default_summary_cache_dir(),
                          progress=progress, cancel_event=cancel_event, opener=opener)


def delete_summary_model(model_id, cache_dir=None):
    return delete_model(_entry(model_id), cache_dir or default_summary_cache_dir())
