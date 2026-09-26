"""Verified, on-demand cache for local summary GGUF files."""

import os

import app_paths
from i18n import tr
from summary_catalog import summary_catalog_entry
from voice_models import delete_model, download_model, installed_model_path, model_is_installed


ENV_SUMMARY_CACHE = "SNIPVOICE_SUMMARY_CACHE"
CACHE_DIR_NAME = "summary-models"


def default_summary_cache_dir(system=None):
    override = os.environ.get(ENV_SUMMARY_CACHE)
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(app_paths.default_models_dir(system), CACHE_DIR_NAME)


def summary_cache_dir():
    """Active summary cache: env override, then the user-chosen model folder."""
    override = os.environ.get(ENV_SUMMARY_CACHE)
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(app_paths.configured_models_dir(), CACHE_DIR_NAME)


def _entry(model_id):
    entry = summary_catalog_entry(model_id)
    if entry is None:
        raise ValueError(tr("Selecione um modelo de resumo do catálogo do SnipVoice."))
    return entry


def summary_model_is_installed(model_id, cache_dir=None):
    return model_is_installed(_entry(model_id), cache_dir or summary_cache_dir())


def summary_model_path(model_id, cache_dir=None):
    return installed_model_path(_entry(model_id), cache_dir or summary_cache_dir())


def download_summary_model(model_id, cache_dir=None, progress=None, cancel_event=None, opener=None):
    return download_model(_entry(model_id), cache_dir or summary_cache_dir(),
                          progress=progress, cancel_event=cancel_event, opener=opener)


def delete_summary_model(model_id, cache_dir=None):
    return delete_model(_entry(model_id), cache_dir or summary_cache_dir())
