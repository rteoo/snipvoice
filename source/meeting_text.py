"""Pure presentation helpers for meeting transcripts and summaries.

The functions in this module only format already stored data.  They do not
call a model, mutate a meeting, or impose a UI-sized limit on full exports.
"""

from dataclasses import dataclass
from typing import Iterable, Iterator, Mapping, Optional


READABLE = "readable"
FULL_TEXT = "full_text"
TIMESTAMPED = "timestamped"
DEFAULT_PREVIEW_CHARS = 1 << 20
# ceiling: raise only when the recording detail editor can safely display more
# than 1 MiB without blocking the Tk event loop.
DEFAULT_PARAGRAPH_CHARS = 900
DEFAULT_PAUSE_SECONDS = 4.0

_TRACK_LABELS = {
    "microphone": "Microfone",
    "system": "Áudio do sistema",
}


@dataclass(frozen=True)
class TextPreview:
    """A UI-safe preview and whether the complete value exceeded its limit."""

    text: str
    truncated: bool

    def __iter__(self):
        # Keep unpacking convenient for small UI callers.
        yield self.text
        yield self.truncated


def _mode(value: str) -> str:
    if value == FULL_TEXT:
        value = READABLE
    if value not in {READABLE, TIMESTAMPED}:
        raise ValueError("Transcript format must be 'readable' or 'timestamped'.")
    return value


def _timestamp(value) -> str:
    try:
        seconds = max(0, int(float(value)))
    except (TypeError, ValueError, OverflowError):
        seconds = 0
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _segment_text(segment: Mapping) -> str:
    value = segment.get("text", "")
    return value.strip() if isinstance(value, str) else ""


def _timestamped(segments: Iterable[Mapping]) -> Iterator[str]:
    for segment in segments:
        if not isinstance(segment, Mapping):
            continue
        text = _segment_text(segment)
        if not text:
            continue
        label = _TRACK_LABELS.get(segment.get("track"))
        prefix = _timestamp(segment.get("start", 0))
        if label:
            prefix += f" {label}:"
        else:
            prefix += ":"
        yield f"{prefix} {text}"


def _readable(segments: Iterable[Mapping], paragraph_chars: int,
              pause_seconds: float) -> Iterator[str]:
    paragraph = []
    paragraph_length = 0
    previous = None
    previous_end: Optional[float] = None

    for segment in segments:
        if not isinstance(segment, Mapping):
            continue
        text = _segment_text(segment)
        if not text:
            continue
        try:
            start = float(segment.get("start", 0))
            end = float(segment.get("end", start))
        except (TypeError, ValueError, OverflowError):
            start, end = 0.0, 0.0
        track = segment.get("track")
        gap = start - previous_end if previous_end is not None else 0.0
        should_break = bool(paragraph) and (
            track != previous or gap >= pause_seconds
            or paragraph_length + len(text) + 1 > paragraph_chars
        )
        if should_break:
            yield " ".join(paragraph)
            paragraph = []
            paragraph_length = 0
        paragraph.append(text)
        paragraph_length += len(text) + (1 if len(paragraph) > 1 else 0)
        previous = track
        previous_end = max(end, start)
    if paragraph:
        yield " ".join(paragraph)


def iter_formatted_transcript(segments: Iterable[Mapping], style: str = READABLE,
                              *, paragraph_chars: int = DEFAULT_PARAGRAPH_CHARS,
                              pause_seconds: float = DEFAULT_PAUSE_SECONDS) -> Iterator[str]:
    """Yield complete transcript paragraphs/lines without a UI size ceiling."""
    style = _mode(style)
    if style == TIMESTAMPED:
        yield from _timestamped(segments)
        return
    if not isinstance(paragraph_chars, int) or paragraph_chars < 1:
        raise ValueError("paragraph_chars must be a positive integer.")
    if not isinstance(pause_seconds, (int, float)) or pause_seconds < 0:
        raise ValueError("pause_seconds must be non-negative.")
    yield from _readable(segments, paragraph_chars, float(pause_seconds))


def format_transcript(segments: Iterable[Mapping], style: str = READABLE, **kwargs) -> str:
    """Return the complete formatted transcript as one string."""
    return "\n\n".join(iter_formatted_transcript(segments, style, **kwargs))


def iter_transcript_text(segments: Iterable[Mapping], style: str = FULL_TEXT, **kwargs) -> Iterator[str]:
    """Integration-facing alias for the complete streaming transcript."""
    return iter_formatted_transcript(segments, style, **kwargs)


def preview_transcript(segments: Iterable[Mapping], style: str = READABLE,
                       max_chars: int = DEFAULT_PREVIEW_CHARS, **kwargs) -> TextPreview:
    """Format only enough of a transcript to produce a bounded UI preview."""
    if not isinstance(max_chars, int) or max_chars < 0:
        raise ValueError("max_chars must be a non-negative integer.")
    parts = []
    length = 0
    for part in iter_formatted_transcript(segments, style, **kwargs):
        separator = "\n\n" if parts else ""
        if length + len(separator) + len(part) > max_chars:
            return TextPreview((separator + part)[:max_chars] if not parts else
                               ("\n\n".join(parts))[:max_chars], True)
        parts.append(part)
        length += len(separator) + len(part)
    return TextPreview("\n\n".join(parts), False)


def transcript_preview(segments: Iterable[Mapping], style: str = FULL_TEXT,
                      max_chars: int = DEFAULT_PREVIEW_CHARS, **kwargs):
    """Return a JSON-friendly bounded preview for the recording detail UI."""
    preview = preview_transcript(segments, style, max_chars, **kwargs)
    return {"text": preview.text, "truncated": preview.truncated}


def _value(value, fallback="") -> str:
    return str(value).strip() if value is not None else fallback


def format_summary(value) -> str:
    """Render a generated summary in readable prose instead of raw JSON."""
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, Mapping):
        return ""
    # Report envelopes keep the model result under ``generated``.  Legacy
    # meeting metadata stores the same fields at the top level.
    if not any(key in value for key in ("summary", "decisions", "action_items")):
        generated = value.get("generated")
        if isinstance(generated, Mapping):
            value = generated
    sections = []
    summary_value = value.get("summary")
    if isinstance(summary_value, Mapping):
        summary_value = summary_value.get("text", summary_value.get("summary", ""))
    summary = _value(summary_value)
    if summary:
        sections.append(summary)
    decisions = value.get("decisions")
    if isinstance(decisions, list) and decisions:
        lines = ["Decisões:"]
        for item in decisions:
            text = _value(item.get("text") if isinstance(item, Mapping) else item)
            if text:
                lines.append(f"• {text}")
        if len(lines) > 1:
            sections.append("\n".join(lines))
    actions = value.get("action_items")
    if isinstance(actions, list) and actions:
        lines = ["Ações:"]
        for item in actions:
            if isinstance(item, Mapping):
                text = _value(item.get("text"))
                details = []
                owner = _value(item.get("owner"))
                deadline = _value(item.get("deadline"))
                if owner:
                    details.append(f"responsável: {owner}")
                if deadline:
                    details.append(f"prazo: {deadline}")
                if details:
                    text = f"{text} ({'; '.join(details)})"
            else:
                text = _value(item)
            if text:
                lines.append(f"• {text}")
        if len(lines) > 1:
            sections.append("\n".join(lines))
    return "\n\n".join(sections)
