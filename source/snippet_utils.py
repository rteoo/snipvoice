import json
import os
import tempfile
import time


_ATOMIC_REPLACE_ATTEMPTS = 5
_ATOMIC_REPLACE_RETRY_SECONDS = 0.01


BUILTIN_DYNAMIC_PREFIXES = {
    "_cpf_numbers": "cpf",
    "_cnpj_numbers": "cnpj",
}


def load_json_file(path):
    """Load JSON data from disk using UTF-8."""
    with open(path, 'r', encoding='utf-8') as handle:
        return json.load(handle)


def write_json_atomic(path, data):
    """Atomically replace a JSON file in the same directory."""
    directory = os.path.dirname(path) or "."
    prefix = f"{os.path.basename(path)}."
    file_descriptor, temp_path = tempfile.mkstemp(prefix=prefix, suffix='.tmp', dir=directory, text=True)

    try:
        with os.fdopen(file_descriptor, 'w', encoding='utf-8') as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        for attempt in range(_ATOMIC_REPLACE_ATTEMPTS):
            try:
                os.replace(temp_path, path)
                break
            except PermissionError:
                if attempt + 1 >= _ATOMIC_REPLACE_ATTEMPTS:
                    raise
                time.sleep(_ATOMIC_REPLACE_RETRY_SECONDS * (attempt + 1))
    except Exception:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise


def get_dynamic_prefixes(snippets):
    """Return builtin and custom dynamic mapping prefixes."""
    prefixes = {}

    for mapping_key, prefix in BUILTIN_DYNAMIC_PREFIXES.items():
        if mapping_key in snippets:
            prefixes[prefix] = mapping_key

    for key, mapping in snippets.items():
        if key.startswith("_") and key.endswith(("_numbers", "_codes")) and key not in BUILTIN_DYNAMIC_PREFIXES:
            derived_prefix = key[1:].replace("_numbers", "").replace("_codes", "")
            prefix = mapping.get("__prefix__") if isinstance(mapping, dict) else None
            # A malformed override must not become a non-string key (breaking
            # startswith/concatenation). Empty strings deliberately create bare
            # triggers, so only non-string values fall back to the default.
            if not isinstance(prefix, str):
                prefix = derived_prefix
            prefixes[prefix] = key

    return prefixes


def check_dynamic_pattern(snippets, text, prefixes=None):
    """Resolve a typed dynamic trigger to its mapped value."""
    resolved_prefixes = prefixes if prefixes is not None else get_dynamic_prefixes(snippets)

    for prefix, mapping_key in resolved_prefixes.items():
        if text.startswith(prefix) and len(text) > len(prefix):
            name = text[len(prefix):]
            mapping = snippets.get(mapping_key)
            if isinstance(mapping, dict) and name in mapping and name != "__prefix__":
                return mapping[name], len(text)

    return None, 0
