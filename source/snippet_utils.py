import copy
import json
import os
import tempfile


DEFAULT_SNIPPETS = {
    "xname": "Example User",
    "_cnpj_numbers": {
        "empresa1": "12.345.678/0001-90",
        "empresa2": "98.765.432/0001-10",
    },
    "_cpf_numbers": {
        "fulano": "123.456.789-00",
    },
}

BUILTIN_DYNAMIC_PREFIXES = {
    "_cpf_numbers": "cpf",
    "_cnpj_numbers": "cnpj",
}


def get_default_snippets():
    """Return a fresh copy of the built-in static snippet defaults."""
    return copy.deepcopy(DEFAULT_SNIPPETS)


def load_json_file(path):
    """Load JSON data from disk using UTF-8."""
    with open(path, 'r', encoding='utf-8') as handle:
        return json.load(handle)


def validate_static_snippets(data):
    """Return valid static snippet data or None when the root shape is invalid."""
    return data if isinstance(data, dict) else None


def build_saveable_snippets(snippets, preserved=None):
    """Remove runtime callables before persisting snippets to JSON.

    ``preserved`` holds static values whose key is shadowed by a dynamic trigger
    in the merged map (see ``find_shadowed_statics``). Without it, saving the
    merged map silently deletes those keys from disk: the callable replaced the
    static value in memory and callables are not persistable.
    """
    saveable = {key: value for key, value in snippets.items() if not callable(value)}
    for key, value in (preserved or {}).items():
        saveable.setdefault(key, value)
    return saveable


def write_json_atomic(path, data):
    """Atomically replace a JSON file in the same directory."""
    directory = os.path.dirname(path) or "."
    prefix = f"{os.path.basename(path)}."
    file_descriptor, temp_path = tempfile.mkstemp(prefix=prefix, suffix='.tmp', dir=directory, text=True)

    try:
        with os.fdopen(file_descriptor, 'w', encoding='utf-8') as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.replace(temp_path, path)
    except Exception:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise


def merge_snippets(static_snippets, dynamic_snippets):
    """Merge static and dynamic snippets, keeping dynamic priority."""
    return {**static_snippets, **dynamic_snippets}


def find_shadowed_statics(static_snippets, dynamic_snippets):
    """Return the static entries a dynamic trigger overwrites in the merged map.

    These keys expand as the dynamic snippet, but their static value must still
    reach disk on the next save, so callers pass this to
    ``build_saveable_snippets`` as ``preserved``.
    """
    return {
        key: value
        for key, value in static_snippets.items()
        if key in dynamic_snippets and not callable(value)
    }


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


def calculate_max_trigger_length_with_mappings(snippets, fallback=20):
    """Include dynamic mapping prefixes plus item names when recomputing from scratch."""
    all_triggers = list(snippets.keys())
    prefixes = get_dynamic_prefixes(snippets)
    mapping_prefixes = {mapping_key: prefix for prefix, mapping_key in prefixes.items()}

    for mapping_key, mapping in snippets.items():
        if not mapping_key.startswith("_") or not isinstance(mapping, dict):
            continue

        prefix = mapping_prefixes.get(mapping_key, "")
        for item_name in mapping.keys():
            if item_name == "__prefix__":
                continue
            all_triggers.append(prefix + item_name)

    return max((len(trigger) for trigger in all_triggers), default=fallback)
