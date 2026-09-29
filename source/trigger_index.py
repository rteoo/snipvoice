from snippet_utils import check_dynamic_pattern, get_dynamic_prefixes


def _is_indexable_trigger(trigger):
    """Whether a snippets-dict key is a real, indexable trigger.

    An empty key would suffix-match every keystroke (``endswith("")`` is always
    true) and has no last character to bucket by; an ``_``-prefixed key names a
    mapping container, not a trigger. The direct index covers keys regardless
    of value, callables included.
    """
    return bool(trigger) and not trigger.startswith("_")


def compile_trigger_index(snippets):
    """Precompute trigger lookup structures for the keyboard hot path.

    Within each last-character bucket, triggers are ordered longest-first so a
    trigger that is a suffix of another can never shadow the longer one
    (deterministic match; fixes the insertion-order hazard). Ties keep source
    order for stability.
    """
    direct_triggers = []
    direct_by_last_char = {}

    for trigger in snippets.keys():
        # An empty key has no last char (crash below) and would suffix-match
        # every keystroke; an ``_``-prefixed key is a mapping container.
        if not _is_indexable_trigger(trigger):
            continue

        direct_triggers.append(trigger)
        last_char = trigger[-1]
        direct_by_last_char.setdefault(last_char, []).append(trigger)

    for last_char, bucket in direct_by_last_char.items():
        bucket.sort(key=len, reverse=True)  # stable: equal lengths keep source order

    dynamic_prefixes = get_dynamic_prefixes(snippets)
    bare_mapping_by_last_char = {}
    bare_mapping_key = dynamic_prefixes.get("")
    bare_mapping = snippets.get(bare_mapping_key)
    if isinstance(bare_mapping, dict):
        for item_name in bare_mapping:
            if item_name == "__prefix__" or not isinstance(item_name, str) or not item_name:
                continue
            bare_mapping_by_last_char.setdefault(item_name[-1], []).append(item_name)
        for bucket in bare_mapping_by_last_char.values():
            bucket.sort(key=len, reverse=True)

    return {
        "direct_triggers": tuple(direct_triggers),
        "direct_by_last_char": {key: tuple(value) for key, value in direct_by_last_char.items()},
        "dynamic_prefixes": dynamic_prefixes,
        "ordered_prefixes": tuple(dynamic_prefixes.keys()),
        "bare_mapping_by_last_char": {
            key: tuple(value) for key, value in bare_mapping_by_last_char.items()
        },
    }


def find_direct_trigger(typed_text, trigger_index):
    """Return the longest direct trigger that matches the current suffix."""
    if not typed_text:
        return None

    candidates = trigger_index["direct_by_last_char"].get(typed_text[-1], ())
    for trigger in candidates:
        if typed_text.endswith(trigger):
            return trigger

    return None


def find_dynamic_trigger(snippets, typed_text, trigger_index):
    """Return the full typed trigger and mapped value for dynamic mappings."""
    if not typed_text:
        return None, None

    dynamic_prefixes = trigger_index["dynamic_prefixes"]
    for prefix in trigger_index["ordered_prefixes"]:
        if prefix == "":
            mapping = snippets.get(dynamic_prefixes[prefix])
            candidates = trigger_index["bare_mapping_by_last_char"].get(
                typed_text[-1], ()
            )
            for item_name in candidates:
                if typed_text.endswith(item_name):
                    value = mapping.get(item_name)
                    if value is not None:
                        return item_name, value
            continue
        if prefix in typed_text:
            prefix_start = typed_text.rfind(prefix)
            potential_trigger = typed_text[prefix_start:]
            value, _ = check_dynamic_pattern(snippets, potential_trigger, dynamic_prefixes)
            if value is not None:
                return potential_trigger, value

    return None, None
