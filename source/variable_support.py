"""Variable substitution support for snippet values.

Syntax: %%variable_name%%

Kinds, resolved in this priority order:
  - %%clipboard-paste%%  → current clipboard plain text
  - %%trigger%%          → plain text of an existing static snippet (1 level deep)
  - %%trigger%%          → a runtime dynamic snippet (xhj, xdolar, xcot, ...): the
                           callable is invoked and its result substituted
  - %%prefixitem%%       → a dynamic mapping trigger (cpffulano) resolved from its
                           mapping container
  - %%field_name%%       → form-fill: collected via a dialog before insertion

Resolving a dynamic reference may block (network) or open a dialog (stock ticker,
WhatsApp prompt), so ``resolve_inline`` must only be called from the expansion
worker thread — never from the keyboard listener.
"""

import re

from rich_text_support import extract_plain_text
from snippet_utils import check_dynamic_pattern, get_dynamic_prefixes

VARIABLE_RE = re.compile(r'%%([^%\s]+)%%')


class VariableResolutionError(RuntimeError):
    """A required inline value could not be read without corrupting output."""


def find_variable_names(text):
    """Return unique variable names in order of first appearance."""
    seen = set()
    result = []
    for m in VARIABLE_RE.finditer(text):
        name = m.group(1)
        if name not in seen:
            seen.add(name)
            result.append(name)
    return result


def classify_variable(name, snippets, prefixes=None):
    """Return 'clipboard', 'snippet_ref', 'dynamic_ref', 'mapping_ref' or 'form_field'.

    ``prefixes`` is the precomputed dynamic-mapping prefix map; pass it when
    classifying many names so the map is not rebuilt per token.
    """
    if name == "clipboard-paste":
        return "clipboard"
    if name in snippets:
        return "dynamic_ref" if callable(snippets[name]) else "snippet_ref"
    if check_dynamic_pattern(snippets, name, prefixes)[0] is not None:
        return "mapping_ref"
    return "form_field"


def has_form_variables(text, snippets, prefixes=None):
    """True if text contains at least one form-field variable."""
    names = find_variable_names(text)
    if not names:
        return False
    if prefixes is None:
        prefixes = get_dynamic_prefixes(snippets)
    return any(
        classify_variable(name, snippets, prefixes) == "form_field"
        for name in names
    )


def resolve_inline(text, snippets, get_clipboard, _seen=None, prefixes=None, notify_failure=None):
    """
    Resolve %%clipboard-paste%%, snippet, dynamic and mapping references in text.
    Form-field variables (%%name%%) are left unchanged.

    May block or open a dialog when a dynamic reference is resolved — call from
    the expansion worker thread, never from the keyboard listener.

    _seen: set of trigger names already in the resolution chain (prevents circular refs).
    notify_failure: optional callback(name, value) so a marker result ("[Erro: ...]")
    notifies exactly as a directly-typed dynamic trigger does.
    """
    names = find_variable_names(text)
    if not names:
        return text

    if _seen is None:
        _seen = set()
    if prefixes is None:
        prefixes = get_dynamic_prefixes(snippets)

    for name in names:
        kind = classify_variable(name, snippets, prefixes)
        if kind == "clipboard":
            try:
                value = get_clipboard()
            except Exception as exc:
                raise VariableResolutionError(
                    "Não foi possível ler a área de transferência."
                ) from exc
            if value is None:
                raise VariableResolutionError(
                    "Não foi possível ler a área de transferência."
                )
            if not isinstance(value, str):
                raise VariableResolutionError(
                    "A área de transferência não retornou texto válido."
                )
            text = text.replace(f"%%{name}%%", value)
        elif name in _seen:
            continue  # circular reference: leave unchanged
        elif kind == "snippet_ref":
            # One level deep only: do not recursively resolve variables inside the ref.
            text = text.replace(f"%%{name}%%", extract_plain_text(snippets[name]))
        elif kind == "mapping_ref":
            value, _ = check_dynamic_pattern(snippets, name, prefixes)
            text = text.replace(f"%%{name}%%", extract_plain_text(value))
        elif kind == "dynamic_ref":
            text = text.replace(f"%%{name}%%", _resolve_dynamic(name, snippets, notify_failure))
        # form_field: leave unchanged

    return text


def _resolve_dynamic(name, snippets, notify_failure):
    """Invoke a dynamic snippet callable for inline substitution.

    A failing callable never aborts the containing expansion: it substitutes an
    empty string and notifies. An action-only flow (xwapp opening the browser)
    returns nothing and likewise substitutes empty.
    """
    try:
        result = snippets[name]()
    except Exception as e:
        if notify_failure:
            notify_failure(name, f"[Erro: {e}]")
        return ""
    if not result:
        return ""
    if notify_failure:
        notify_failure(name, result)
    return extract_plain_text(result)


def resolve_form_variables(text, form_data):
    """Substitute form-field variables with values collected from the dialog."""
    for name, value in form_data.items():
        text = text.replace(f"%%{name}%%", value)
    return text
