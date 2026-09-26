"""Interface language selection and string lookup.

Brazilian Portuguese is the source language: UI literals are written in
pt-BR and double as catalog keys, so the default interface needs no lookup.
Other languages map each pt-BR literal to a translation. Text is resolved at
display time, so a language change takes effect for every widget built after
it; the manager window is rebuilt to apply it.
"""

import threading

from i18n_en_us import EN_US

DEFAULT_LANGUAGE = "pt-BR"
LANGUAGE_NAMES = {
    "pt-BR": "Português (Brasil)",
    "en-US": "English (US)",
}
_CATALOGS = {"en-US": EN_US}

_lock = threading.Lock()
_language = DEFAULT_LANGUAGE


def normalize_language(value):
    """Return a supported language code, falling back to the default."""
    return value if value in LANGUAGE_NAMES else DEFAULT_LANGUAGE


def set_language(value):
    global _language
    with _lock:
        _language = normalize_language(value)
    return _language


def current_language():
    return _language


def N_(text):
    """Mark a pt-BR literal for the catalog without translating it yet.

    Module-level tables are built before the language is known; they hold
    marked source text and pass it through ``tr`` where it is displayed.
    """
    return text


def tr(text, /, **values):
    """Translate a pt-BR literal into the active language, then format it.

    A missing entry falls back to the pt-BR text; the catalog coverage test
    keeps that from shipping.
    """
    catalog = _CATALOGS.get(_language)
    if catalog is not None:
        text = catalog.get(text, text)
    return text.format(**values) if values else text
