"""Pure presentation helpers for meeting transcripts and summaries.

The functions in this module only format already stored data.  They do not
call a model, mutate a meeting, or impose a UI-sized limit on full exports.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Iterable, Iterator, Mapping, Optional

import i18n
from i18n import N_, tr


READABLE = "readable"
FULL_TEXT = "full_text"
TIMESTAMPED = "timestamped"
DEFAULT_PREVIEW_CHARS = 1 << 20
# ceiling: raise only when the recording detail editor can safely display more
# than 1 MiB without blocking the Tk event loop.
DEFAULT_PARAGRAPH_CHARS = 900
DEFAULT_PAUSE_SECONDS = 4.0

_TRACK_LABELS = {
    "microphone": N_("Microfone"),
    "system": N_("Áudio do sistema"),
    "final": N_("Áudio final"),
}

_WEEKDAYS = (N_("seg."), N_("ter."), N_("qua."), N_("qui."), N_("sex."), N_("sáb."), N_("dom."))
_MONTHS = (N_("jan."), N_("fev."), N_("mar."), N_("abr."), N_("maio"), N_("jun."),
           N_("jul."), N_("ago."), N_("set."), N_("out."), N_("nov."), N_("dez."))
# Field order and the decimal mark differ by language, so they are locale data
# rather than catalog strings (a catalog entry must keep its placeholders).
_LOCALE_FORMATS = {
    "pt-BR": {"day_month": "{day:02d}/{month:02d}", "full": "{day:02d}/{month:02d}/{year}",
              "decimal": ","},
    "en-US": {"day_month": "{month_name} {day}", "full": "{month_name} {day}, {year}",
              "decimal": "."},
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
            prefix += f" {tr(label)}:"
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


_SUMMARY_SECTION_ORDER = (
    "summary", "key_points", "decisions", "feedback", "objections", "risks",
    "open_questions", "action_items", "follow_up_email",
)
_SUMMARY_SECTION_LABELS = {
    "key_points": N_("Pontos principais"),
    "decisions": N_("Decisões"),
    "feedback": N_("Feedback"),
    "objections": N_("Objeções"),
    "risks": N_("Riscos"),
    "open_questions": N_("Questões em aberto"),
    "action_items": N_("Ações"),
    "follow_up_email": N_("E-mail de acompanhamento"),
}
_DETAILED_NOTES_LABEL = N_("Notas detalhadas")


def _summary_payload(value):
    """Return the generated or section mapping from known report envelopes."""
    if not isinstance(value, Mapping):
        return value
    generated = value.get("generated")
    if isinstance(generated, Mapping):
        merged = dict(generated)
        reviewed = value.get("reviewed_artifact")
        reviewed_sections = reviewed.get("sections") if isinstance(reviewed, Mapping) else None
        if isinstance(reviewed_sections, Mapping):
            merged.update(reviewed_sections)
        value = merged
    sections = value.get("sections")
    if isinstance(sections, Mapping):
        # Reviewed-artifact and lightweight library projections use this shape.
        value = sections
    return value


def _summary_mapping_text(value) -> str:
    if not isinstance(value, Mapping):
        return _value(value)
    return _value(value.get("text", value.get("summary", "")))


def _summary_items(value, section) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if not isinstance(value, list):
        return []
    lines = []
    for item in value:
        if isinstance(item, Mapping):
            text = _value(item.get("text"))
            if section == "action_items":
                details = []
                owner = _value(item.get("owner"))
                deadline = _value(item.get("deadline"))
                if owner:
                    details.append(tr("responsável: {owner}", owner=owner))
                if deadline:
                    details.append(tr("prazo: {deadline}", deadline=deadline))
                if details:
                    text = f"{text} ({'; '.join(details)})"
        else:
            text = _value(item)
        if text:
            lines.append(text)
    return lines


def format_detailed_notes(value, *, markdown=False) -> str:
    """Render chronological chunk notes without dropping text or citations."""
    if isinstance(value, str):
        text = value.strip()
        return text
    if not isinstance(value, list):
        return ""
    lines = []
    ordered = sorted(
        enumerate(value),
        key=lambda pair: (float(pair[1].get("start", 0))
                          if isinstance(pair[1], Mapping) and isinstance(
                              pair[1].get("start", 0), (int, float)) else float("inf"),
                          pair[0]),
    )
    for _index, note in ordered:
        if not isinstance(note, Mapping):
            continue
        text = _value(note.get("text"))
        if not text:
            continue
        start = _timestamp(note.get("start", 0))
        end = _timestamp(note.get("end", note.get("start", 0)))
        citations = note.get("segment_ids")
        citation_text = ""
        if isinstance(citations, list):
            valid = [str(item) for item in citations if isinstance(item, str) and item]
            if valid:
                citation_text = f" [{tr('citações')}: {', '.join(valid)}]"
        prefix = f"{start}–{end}{citation_text}"
        lines.append((f"- {prefix}: " if markdown else f"{prefix}: ") + text)
    return "\n".join(lines)


def format_summary(value) -> str:
    """Render a generated summary in readable prose instead of raw JSON."""
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, Mapping):
        return ""
    value = _summary_payload(value)
    sections = []
    summary = _summary_mapping_text(value.get("summary"))
    if summary:
        sections.append(summary)
    for section in _SUMMARY_SECTION_ORDER[1:]:
        current = value.get(section)
        if section == "follow_up_email":
            if isinstance(current, Mapping):
                subject = _value(current.get("subject"))
                body = _value(current.get("body", current.get("text")))
                lines = []
                if subject:
                    lines.append(tr("Assunto: {subject}", subject=subject))
                if body:
                    lines.append(body)
            else:
                lines = [_value(current)] if _value(current) else []
        else:
            lines = _summary_items(current, section)
        if lines:
            label = tr(_SUMMARY_SECTION_LABELS[section])
            sections.append("\n".join([f"{label}:"] + [f"• {line}" for line in lines])
                            if section != "follow_up_email" else
                            "\n".join([f"{label}:"] + lines))
    detailed = format_detailed_notes(value.get("detailed_notes"))
    if detailed:
        sections.append("\n".join([f"{tr(_DETAILED_NOTES_LABEL)}:", detailed]))
    return "\n\n".join(sections)


def _locale_format():
    return _LOCALE_FORMATS[i18n.current_language()]


def _parse_timestamp(value):
    """An ISO-8601 instant as an aware datetime, or None for missing or malformed text.

    The store writes UTC with a trailing ``Z``; a value without an offset is
    read as UTC too, never as local time.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def _aware_now(now):
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware.")
    return now


def format_local_timestamp(value, now=None):
    """A stored UTC instant as local time for a list or a record.

    "Hoje 14:02", "Ontem 09:10", "qua. 14/10 09:10" within the last week,
    "14/10 09:10" this year, "14/10/2025" before it (en: "Today 14:02",
    "Wed Oct 14 09:10", "Oct 14, 2025"). Missing, malformed or out-of-range
    input reads "Sem data", never a made-up time. ``now`` (aware) fixes the
    clock and the local time zone for tests; by default each instant gets the
    system zone's own offset, so daylight saving changes do not shift older
    records.
    """
    moment = _parse_timestamp(value)
    if moment is None:
        return tr("Sem data")
    try:
        local = moment.astimezone() if now is None else moment.astimezone(_aware_now(now).tzinfo)
    except (OverflowError, OSError):
        # A well-formed instant the platform's local-time conversion cannot
        # hold (Windows raises for 1960 and for 3001); one stored value like
        # that must not break the list it sits in.
        return tr("Sem data")
    if now is None:
        now = datetime.now().astimezone()
    clock = f"{local.hour:02d}:{local.minute:02d}"
    days = (now.date() - local.date()).days
    if days == 0:
        return tr("Hoje {time}", time=clock)
    if days == 1:
        return tr("Ontem {time}", time=clock)
    layout = _locale_format()
    fields = {"day": local.day, "month": local.month, "year": local.year,
              "month_name": tr(_MONTHS[local.month - 1])}
    day_month = layout["day_month"].format(**fields)
    if 1 < days < 7:
        return f"{tr(_WEEKDAYS[local.weekday()])} {day_month} {clock}"
    if local.year == now.year:
        return f"{day_month} {clock}"
    return layout["full"].format(**fields)


def seconds_until(value, now=None):
    """Seconds from ``now`` until the stored instant (negative once past), or None."""
    moment = _parse_timestamp(value)
    if moment is None:
        return None
    return (moment - _aware_now(now)).total_seconds()


def format_expiry(value, now=None):
    """Time left before a deadline: "em 23 d", "em 3 h", "em 4 min", or "Expirada"."""
    remaining = seconds_until(value, now)
    if remaining is None:
        return tr("Sem data")
    if remaining <= 0:
        return tr("Expirada")
    if remaining >= 86400:
        return tr("em {count} d", count=int(remaining // 86400))
    if remaining >= 3600:
        return tr("em {count} h", count=int(remaining // 3600))
    return tr("em {count} min", count=max(1, int(remaining // 60)))


def format_storage_size(size_bytes):
    """Decimal storage size: "512 B", "46,2 MB", "1,3 GB" (en "1.3 GB").

    One decimal below 100 of a unit, none above, so a column of sizes keeps
    three significant digits. The choice follows the rounded number: 99.96 MB
    reads "100 MB" and 999.96 kB reads "1,0 MB".
    """
    if (isinstance(size_bytes, bool) or not isinstance(size_bytes, (int, float))
            or not math.isfinite(size_bytes) or size_bytes < 0):
        raise ValueError("size_bytes must be a non-negative number.")
    if size_bytes < 1e3:
        return f"{int(size_bytes)} B"
    for unit, scale in (("kB", 1e3), ("MB", 1e6), ("GB", 1e9)):
        value = size_bytes / scale
        digits = 1 if round(value, 1) < 100 else 0
        shown = round(value, digits)
        if shown < 1000 or unit == "GB":
            return f"{shown:.{digits}f}".replace(".", _locale_format()["decimal"]) + f" {unit}"
