from rich_text_support import extract_plain_text
from snippet_utils import check_dynamic_pattern, get_dynamic_prefixes
from variable_support import find_variable_names, has_form_variables


def _is_indexable_trigger(trigger):
    """Whether a snippets-dict key is a real, indexable trigger.

    An empty key would suffix-match every keystroke (``endswith("")`` is always
    true) and has no last character to bucket by; an ``_``-prefixed key names a
    mapping container, not a trigger. Both are excluded from every index set so
    the direct index and the metadata helpers never disagree about what "" is.
    Callable handling is intentionally left to each caller: the direct-index
    loop indexes keys regardless of value, while the metadata helpers skip
    callables.
    """
    return bool(trigger) and not trigger.startswith("_")


def _compute_form_triggers(snippets, prefixes=None):
    """Return triggers whose value needs a form-fill dialog.

    Computed once at compile time so the keyboard hot path never runs the
    form-variable regex per keystroke. Includes both direct triggers and
    triggers composed from a dynamic-mapping prefix plus item name.
    """
    if prefixes is None:
        prefixes = get_dynamic_prefixes(snippets)

    form_triggers = set()
    for trigger, value in snippets.items():
        if not _is_indexable_trigger(trigger) or callable(value):
            continue
        if has_form_variables(extract_plain_text(value), snippets, prefixes):
            form_triggers.add(trigger)

    for prefix, mapping_key in prefixes.items():
        mapping = snippets.get(mapping_key)
        if not isinstance(mapping, dict):
            continue
        for item_name, value in mapping.items():
            if item_name == "__prefix__" or callable(value):
                continue
            if has_form_variables(extract_plain_text(value), snippets, prefixes):
                form_triggers.add(prefix + item_name)

    return form_triggers


def _compute_slow_ref_triggers(snippets, slow_snippets):
    """Return direct triggers whose body references a slow dynamic trigger.

    Such a snippet must run on the async path: resolving the reference fetches
    over the network or opens a dialog, which would otherwise block the listener.
    """
    slow_ref_triggers = set()
    for trigger, value in snippets.items():
        if not _is_indexable_trigger(trigger) or callable(value):
            continue
        if any(name in slow_snippets for name in find_variable_names(extract_plain_text(value))):
            slow_ref_triggers.add(trigger)
    return slow_ref_triggers


def compile_trigger_index(snippets, slow_snippets):
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
        "slow_triggers": frozenset(slow_snippets) | _compute_slow_ref_triggers(snippets, slow_snippets),
        "form_triggers": frozenset(_compute_form_triggers(snippets, dynamic_prefixes)),
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
