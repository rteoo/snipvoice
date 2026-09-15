"""Save-time validation for static snippet triggers.

Pure functions returning human-readable warning strings (Portuguese, since they
are shown to the user). These are warnings, not hard errors: the manager shows
them and lets the user confirm, so a deliberate edge case is never blocked.
"""

# Characters that end a word; a trigger containing one can misfire mid-word.
TERMINATOR_CHARS = frozenset(" \t\n\r.,;:!?)]}\"'")


def validate_trigger(trigger, existing_triggers, dynamic_trigger_names):
    """Return a list of warnings for a proposed static trigger.

    - existing_triggers: other static/composed triggers already registered.
    - dynamic_trigger_names: names of runtime dynamic snippets (they take merge
      priority, so a static trigger with the same name never fires).
    """
    warnings = []

    if any(ch.isspace() for ch in trigger):
        warnings.append("O trigger contém espaços em branco.")
    elif any(ch in TERMINATOR_CHARS for ch in trigger):
        warnings.append("O trigger contém pontuação/terminador e pode disparar no meio de palavras.")

    if len(trigger) <= 2:
        warnings.append("Trigger muito curto (1–2 caracteres) pode disparar por engano ao digitar.")

    if trigger in dynamic_trigger_names:
        warnings.append(
            "Já existe um snippet dinâmico com esse nome; ele tem prioridade e este nunca seria acionado."
        )

    for other in existing_triggers:
        if other == trigger:
            continue
        if trigger.endswith(other):
            warnings.append(f"O trigger termina com um trigger existente ('{other}').")
        elif other.endswith(trigger):
            warnings.append(f"Um trigger existente ('{other}') termina com este trigger.")

    return warnings
