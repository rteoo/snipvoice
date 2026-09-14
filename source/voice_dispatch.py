"""Route a finished transcript without feeding it through the keyboard listener.

Dictation is literal text. Voice command requires an exact match to a live
effective trigger. Form input updates a registered widget through GuiThread
and never synthesizes a global paste into the app's own window.
"""

from dataclasses import dataclass

from trigger_index import find_direct_trigger, find_dynamic_trigger


MODE_DICTATION = "dictation"
MODE_COMMAND = "command"
MODE_FORM = "form"

OUTCOME_INSERTED = "inserted"
OUTCOME_EXPANDED = "expanded"
OUTCOME_FORM = "form"
OUTCOME_NO_MATCH = "no_match"
OUTCOME_EMPTY = "empty"
OUTCOME_CANCELLED = "cancelled"
OUTCOME_SECURE_INPUT = "secure_input"
OUTCOME_TARGET_LOST = "target_lost"
OUTCOME_FAILED = "failed"


@dataclass(frozen=True)
class VoiceDispatchResult:
    """Outcome plus the result of any attempted clipboard recovery."""

    outcome: str
    clipboard_saved: bool | None = None


def normalize_command_transcript(text):
    """Collapse a finished transcript for exact trigger matching."""
    if not isinstance(text, str):
        return ""
    return " ".join(text.strip().split())


def _candidate_strings(text):
    normalized = normalize_command_transcript(text)
    if not normalized:
        return ()
    lowered = normalized.lower()
    compact = "".join(lowered.split())
    seen = []
    for item in (normalized, lowered, compact):
        if item and item not in seen:
            seen.append(item)
    return tuple(seen)


def match_voice_command(text, snippets, trigger_index):
    """Return the exact live trigger the transcript names, or None.

    No fuzzy match. Renamed or disabled dynamic triggers follow the compiled
    index, not the bundled key.
    """
    if trigger_index is None:
        return None
    for candidate in _candidate_strings(text):
        direct = find_direct_trigger(candidate, trigger_index)
        if direct == candidate:
            return direct
        trigger, value = find_dynamic_trigger(snippets, candidate, trigger_index)
        if trigger == candidate and value is not None:
            return trigger
    return None


def classify_voice_target(target, form_registered):
    """Pick the dispatcher mode from the captured target and the armed hotkey."""
    if form_registered:
        return MODE_FORM
    kind = getattr(target, "kind", None) if target is not None else None
    if kind == "form":
        return MODE_FORM
    return None


class VoiceTarget:
    """Foreground target captured when the hotkey was pressed."""

    __slots__ = ("kind", "handle")

    def __init__(self, kind, handle=None):
        self.kind = kind
        self.handle = handle


def dispatch_voice_result(
    transcript,
    mode,
    target,
    snippets,
    trigger_index,
    insert_text,
    expand_trigger,
    apply_form,
    restore_target,
    secure_input_blocks,
    leave_on_clipboard,
    cancelled=False,
    is_cancelled=None,
):
    """Apply one finished transcript. Never logs the text.

    ``insert_text`` / ``expand_trigger`` / ``apply_form`` are app callbacks.
    This function must not call ``_dispatch_expansion``.
    """
    def aborted():
        if cancelled:
            return True
        if is_cancelled is None:
            return False
        try:
            return bool(is_cancelled())
        except Exception:
            return True

    if aborted():
        return VoiceDispatchResult(OUTCOME_CANCELLED)
    text = transcript if isinstance(transcript, str) else ""
    if not text.strip():
        return VoiceDispatchResult(OUTCOME_EMPTY)

    resolved_mode = classify_voice_target(target, apply_form is not None and mode != MODE_COMMAND)
    if resolved_mode is None:
        resolved_mode = mode if mode in (MODE_DICTATION, MODE_COMMAND, MODE_FORM) else MODE_DICTATION

    if resolved_mode == MODE_FORM:
        if apply_form is None:
            return VoiceDispatchResult(OUTCOME_FAILED)
        if aborted():
            return VoiceDispatchResult(OUTCOME_CANCELLED)
        apply_form(text)
        if aborted():
            return VoiceDispatchResult(OUTCOME_CANCELLED)
        return VoiceDispatchResult(OUTCOME_FORM)

    if resolved_mode == MODE_COMMAND:
        trigger = match_voice_command(text, snippets, trigger_index)
        if trigger is None:
            return VoiceDispatchResult(OUTCOME_NO_MATCH)
        if aborted():
            return VoiceDispatchResult(OUTCOME_CANCELLED)
        if secure_input_blocks():
            if aborted():
                return VoiceDispatchResult(OUTCOME_CANCELLED)
            saved = leave_on_clipboard(text)
            if aborted():
                return VoiceDispatchResult(OUTCOME_CANCELLED)
            return VoiceDispatchResult(
                OUTCOME_SECURE_INPUT,
                clipboard_saved=bool(saved),
            )
        if aborted():
            return VoiceDispatchResult(OUTCOME_CANCELLED)
        if not restore_target(target):
            if aborted():
                return VoiceDispatchResult(OUTCOME_CANCELLED)
            saved = leave_on_clipboard(text)
            if aborted():
                return VoiceDispatchResult(OUTCOME_CANCELLED)
            return VoiceDispatchResult(
                OUTCOME_TARGET_LOST,
                clipboard_saved=bool(saved),
            )
        if aborted():
            return VoiceDispatchResult(OUTCOME_CANCELLED)
        expanded = expand_trigger(trigger)
        if aborted():
            return VoiceDispatchResult(OUTCOME_CANCELLED)
        if expanded:
            return VoiceDispatchResult(OUTCOME_EXPANDED)
        return VoiceDispatchResult(OUTCOME_FAILED)

    if aborted():
        return VoiceDispatchResult(OUTCOME_CANCELLED)
    if secure_input_blocks():
        if aborted():
            return VoiceDispatchResult(OUTCOME_CANCELLED)
        saved = leave_on_clipboard(text)
        if aborted():
            return VoiceDispatchResult(OUTCOME_CANCELLED)
        return VoiceDispatchResult(
            OUTCOME_SECURE_INPUT,
            clipboard_saved=bool(saved),
        )
    if aborted():
        return VoiceDispatchResult(OUTCOME_CANCELLED)
    if not restore_target(target):
        if aborted():
            return VoiceDispatchResult(OUTCOME_CANCELLED)
        saved = leave_on_clipboard(text)
        if aborted():
            return VoiceDispatchResult(OUTCOME_CANCELLED)
        return VoiceDispatchResult(
            OUTCOME_TARGET_LOST,
            clipboard_saved=bool(saved),
        )
    if aborted():
        return VoiceDispatchResult(OUTCOME_CANCELLED)
    inserted = insert_text(text)
    if aborted():
        return VoiceDispatchResult(OUTCOME_CANCELLED)
    if inserted:
        return VoiceDispatchResult(OUTCOME_INSERTED)
    if aborted():
        return VoiceDispatchResult(OUTCOME_CANCELLED)
    saved = leave_on_clipboard(text)
    if aborted():
        return VoiceDispatchResult(OUTCOME_CANCELLED)
    return VoiceDispatchResult(
        OUTCOME_FAILED,
        clipboard_saved=bool(saved),
    )
