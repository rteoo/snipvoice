import difflib
import html
import tkinter as tk
from tkinter import font as tkfont

from ui_theme import mono_family


RICH_TEXT_KIND = "rich_text"
STYLE_BITS = {
    "bold": 1,
    "italic": 2,
    "underline": 4,
    "code": 8,
    "strike": 16,
}
STYLE_ORDER = ("bold", "italic", "underline", "code", "strike")
MAX_STYLE_MASK = sum(STYLE_BITS.values())
# Per-OS: "Consolas" on Windows (unchanged), Menlo on macOS. A Windows-only
# family silently substitutes elsewhere and changes the code span's metrics.
CODE_FONT_FAMILY = mono_family()
RTF_DEFAULT_FONT = "Arial"
RTF_CODE_FONT = "Consolas"


def _tag_name(mask):
    return f"fmt_{mask}"


def is_rich_text_payload(value):
    return isinstance(value, dict) and value.get("__kind__") == RICH_TEXT_KIND and isinstance(value.get("text"), str)


def extract_plain_text(value):
    if is_rich_text_payload(value):
        return value["text"]
    if value is None:
        return ""
    return str(value)


def normalize_style_spans(spans, text_length=None):
    if not isinstance(spans, (list, tuple)):
        # A corrupt payload can carry any spans shape; a scalar must degrade
        # to "no spans" like every other malformed value instead of raising.
        spans = []
    normalized = []
    for span in spans:
        if not isinstance(span, dict):
            continue
        tag = span.get("tag")
        start = span.get("start")
        end = span.get("end")
        if tag not in STYLE_BITS:
            continue
        if not isinstance(start, int) or not isinstance(end, int):
            continue
        if start < 0 or end <= start:
            continue
        if text_length is not None:
            if start >= text_length:
                continue
            end = min(end, text_length)
        normalized.append({"tag": tag, "start": start, "end": end})
    normalized.sort(key=lambda item: (item["start"], item["end"], item["tag"]))
    return normalized


def _build_masks(text, spans):
    masks = [0] * len(text)
    for span in normalize_style_spans(spans, len(text)):
        bit = STYLE_BITS[span["tag"]]
        for offset in range(span["start"], span["end"]):
            masks[offset] |= bit
    return masks


def _segment_text(text, masks):
    if not text:
        return []
    segments = []
    start = 0
    current_mask = masks[0] if masks else 0
    for index in range(1, len(text)):
        mask = masks[index] if masks else 0
        if mask != current_mask:
            segments.append((text[start:index], current_mask))
            start = index
            current_mask = mask
    segments.append((text[start:], current_mask))
    return segments


def _wrap_html_segment(text, mask):
    content = html.escape(text).replace("\n", "<br>")
    if mask & STYLE_BITS["code"]:
        content = f"<code>{content}</code>"
    if mask & STYLE_BITS["strike"]:
        content = f"<s>{content}</s>"
    if mask & STYLE_BITS["underline"]:
        content = f"<u>{content}</u>"
    if mask & STYLE_BITS["italic"]:
        content = f"<em>{content}</em>"
    if mask & STYLE_BITS["bold"]:
        content = f"<strong>{content}</strong>"
    return content


def build_html_fragment(text, spans):
    masks = _build_masks(text, spans)
    body = "".join(_wrap_html_segment(segment_text, mask) for segment_text, mask in _segment_text(text, masks))
    return f"<div>{body}</div>"


def _escape_rtf_text(text):
    parts = []
    for char in text:
        codepoint = ord(char)
        if char == "\\":
            parts.append(r"\\")
        elif char == "{":
            parts.append(r"\{")
        elif char == "}":
            parts.append(r"\}")
        elif char == "\n":
            parts.append(r"\par " + "\n")
        elif 32 <= codepoint <= 126:
            parts.append(char)
        elif codepoint > 0xFFFF:
            # \uN? units are signed 16-bit, so astral chars must be written as
            # their UTF-16 surrogate pair — one escape per unit (\uc1 gives each
            # its own fallback char).
            high, low = divmod(codepoint - 0x10000, 0x400)
            parts.append(fr"\u{0xD800 + high - 65536}?")
            parts.append(fr"\u{0xDC00 + low - 65536}?")
        else:
            signed = codepoint if codepoint < 32768 else codepoint - 65536
            parts.append(fr"\u{signed}?")
    return "".join(parts)


def _wrap_rtf_segment(text, mask):
    prefix = []
    suffix = []
    if mask & STYLE_BITS["code"]:
        prefix.append(r"\f1 ")
        suffix.insert(0, r"\f0 ")
    if mask & STYLE_BITS["bold"]:
        prefix.append(r"\b ")
        suffix.insert(0, r"\b0 ")
    if mask & STYLE_BITS["italic"]:
        prefix.append(r"\i ")
        suffix.insert(0, r"\i0 ")
    if mask & STYLE_BITS["underline"]:
        prefix.append(r"\ul ")
        suffix.insert(0, r"\ul0 ")
    if mask & STYLE_BITS["strike"]:
        prefix.append(r"\strike ")
        suffix.insert(0, r"\strike0 ")
    return "".join(prefix) + _escape_rtf_text(text) + "".join(suffix)


def build_rtf_document(text, spans):
    masks = _build_masks(text, spans)
    body = "".join(_wrap_rtf_segment(segment_text, mask) for segment_text, mask in _segment_text(text, masks))
    return (
        r"{\rtf1\ansi\deff0"
        rf"{{\fonttbl{{\f0 {RTF_DEFAULT_FONT};}}{{\f1 {RTF_CODE_FONT};}}}}"
        r"\viewkind4\uc1\pard\f0\fs20 "
        + body
        + "}"
    )


def build_rich_text_payload(text, spans):
    plain_text = extract_plain_text(text)
    normalized_spans = normalize_style_spans(spans, len(plain_text))
    if not normalized_spans:
        return plain_text
    return {
        "__kind__": RICH_TEXT_KIND,
        "text": plain_text,
        "spans": normalized_spans,
        "html": build_html_fragment(plain_text, normalized_spans),
        "rtf": build_rtf_document(plain_text, normalized_spans),
    }


def normalize_rich_text_payload(value):
    if not is_rich_text_payload(value):
        return value
    text = extract_plain_text(value)
    spans = normalize_style_spans(value.get("spans"), len(text))
    html_fragment = value.get("html") or build_html_fragment(text, spans)
    rtf_document = value.get("rtf") or build_rtf_document(text, spans)
    return {
        "__kind__": RICH_TEXT_KIND,
        "text": text,
        "spans": spans,
        "html": html_fragment,
        "rtf": rtf_document,
    }


def get_clipboard_payload(value):
    normalized = normalize_rich_text_payload(value)
    payload = {"text": extract_plain_text(normalized)}
    if is_rich_text_payload(normalized):
        if normalized.get("html"):
            payload["html"] = normalized["html"]
        if normalized.get("rtf"):
            payload["rtf"] = normalized["rtf"]
    return payload


def configure_rich_text_widget(text_widget):
    font_spec = text_widget.cget("font")
    try:
        base_font = tkfont.nametofont(font_spec).copy()
    except tk.TclError:
        base_font = tkfont.Font(font=font_spec)

    font_cache = {}
    for mask in range(MAX_STYLE_MASK + 1):
        font = base_font.copy()
        font.configure(
            family=CODE_FONT_FAMILY if mask & STYLE_BITS["code"] else base_font.cget("family"),
            weight="bold" if mask & STYLE_BITS["bold"] else "normal",
            slant="italic" if mask & STYLE_BITS["italic"] else "roman",
            underline=1 if mask & STYLE_BITS["underline"] else 0,
            overstrike=1 if mask & STYLE_BITS["strike"] else 0,
        )
        tag_name = _tag_name(mask)
        font_cache[tag_name] = font
        text_widget.tag_configure(tag_name, font=font)

    text_widget._rich_text_fonts = font_cache
    text_widget._rich_text_last_selection = None
    text_widget.configure(exportselection=False)

    def remember_selection(_event=None):
        ranges = text_widget.tag_ranges("sel")
        if len(ranges) == 2:
            start = _index_to_offset(text_widget, ranges[0])
            end = _index_to_offset(text_widget, ranges[1])
            if end > start:
                text_widget._rich_text_last_selection = (start, end)

    text_widget.bind("<ButtonRelease-1>", remember_selection, add="+")
    text_widget.bind("<KeyRelease>", remember_selection, add="+")


def _text_length(text_widget):
    return len(text_widget.get("1.0", "end-1c"))


def _index_to_offset(text_widget, index):
    return len(text_widget.get("1.0", index))


def _offset_to_index(offset):
    return f"1.0+{offset}c"


def _get_mask_at_offset(text_widget, offset):
    if offset >= _text_length(text_widget):
        return 0
    tags = set(text_widget.tag_names(_offset_to_index(offset)))
    for mask in range(MAX_STYLE_MASK + 1):
        if _tag_name(mask) in tags:
            return mask
    return 0


def _apply_masks_to_range(text_widget, start_offset, masks):
    if not masks:
        return
    end_offset = start_offset + len(masks)
    start_index = _offset_to_index(start_offset)
    end_index = _offset_to_index(end_offset)
    for mask in range(MAX_STYLE_MASK + 1):
        text_widget.tag_remove(_tag_name(mask), start_index, end_index)
    run_start = 0
    current_mask = masks[0]
    for index in range(1, len(masks) + 1):
        next_mask = masks[index] if index < len(masks) else None
        if next_mask != current_mask:
            range_start = _offset_to_index(start_offset + run_start)
            range_end = _offset_to_index(start_offset + index)
            text_widget.tag_add(_tag_name(current_mask), range_start, range_end)
            run_start = index
            current_mask = next_mask


def _selection_offsets(text_widget):
    ranges = text_widget.tag_ranges("sel")
    if len(ranges) == 2:
        start = _index_to_offset(text_widget, ranges[0])
        end = _index_to_offset(text_widget, ranges[1])
        if end > start:
            text_widget._rich_text_last_selection = (start, end)
            return start, end

    cached = getattr(text_widget, "_rich_text_last_selection", None)
    if not cached:
        return None

    start, end = cached
    max_length = _text_length(text_widget)
    if start >= max_length:
        return None
    end = min(end, max_length)
    if end <= start:
        return None
    return start, end


def toggle_text_style(text_widget, style_name):
    bit = STYLE_BITS[style_name]
    selection = _selection_offsets(text_widget)
    if not selection:
        return False
    start, end = selection
    masks = [_get_mask_at_offset(text_widget, offset) for offset in range(start, end)]
    should_enable = any((mask & bit) == 0 for mask in masks)
    updated = []
    for mask in masks:
        if should_enable:
            updated.append(mask | bit)
        else:
            updated.append(mask & ~bit)
    _apply_masks_to_range(text_widget, start, updated)
    return True


def clear_text_styles(text_widget):
    selection = _selection_offsets(text_widget)
    if selection:
        start, end = selection
    else:
        start, end = 0, _text_length(text_widget)
    if end <= start:
        return False
    _apply_masks_to_range(text_widget, start, [0] * (end - start))
    return True


def extract_style_spans_from_widget(text_widget):
    text = text_widget.get("1.0", "end-1c")
    masks = [_get_mask_at_offset(text_widget, offset) for offset in range(len(text))]
    spans = []
    for style_name in STYLE_ORDER:
        bit = STYLE_BITS[style_name]
        start = None
        for offset, mask in enumerate(masks):
            active = (mask & bit) != 0
            if active and start is None:
                start = offset
            elif not active and start is not None:
                spans.append({"tag": style_name, "start": start, "end": offset})
                start = None
        if start is not None:
            spans.append({"tag": style_name, "start": start, "end": len(text)})
    return spans


def serialize_text_widget_content(text_widget):
    text = text_widget.get("1.0", "end-1c")
    spans = extract_style_spans_from_widget(text_widget)
    return build_rich_text_payload(text, spans)


def rebuild_rich_text(original, new_text):
    """
    Return a new rich-text dict with updated plain text.
    Spans are remapped through the text diff; HTML and RTF are regenerated.
    Returns a plain string if no valid spans remain after clipping.
    """
    if not is_rich_text_payload(original):
        return new_text

    original_text = original["text"]
    original_spans = normalize_style_spans(
        original.get("spans", []), len(original_text)
    )
    if not original_spans:
        return new_text

    # Map every boundary in the old text to its corresponding boundary in
    # the new text. Equal runs map one-to-one; replacement starts map to the
    # replacement start while interior boundaries map to its end, deletions
    # collapse to their start, and insertions are placed after the inserted
    # run. This keeps spans around and after a changed token aligned while
    # making insertion/deletion/replacement behavior deterministic.
    boundaries = [0] * (len(original_text) + 1)
    matcher = difflib.SequenceMatcher(
        None, original_text, new_text, autojunk=False
    )
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag == "equal":
            for boundary in range(old_start, old_end + 1):
                boundaries[boundary] = new_start + (boundary - old_start)
        elif tag == "replace":
            boundaries[old_start] = new_start
            for boundary in range(old_start + 1, old_end + 1):
                boundaries[boundary] = new_end
        elif tag == "delete":
            for boundary in range(old_start, old_end + 1):
                boundaries[boundary] = new_start
        else:  # insert: no old characters, so place the boundary after it.
            boundaries[old_start] = new_end

    remapped_spans = []
    for span in original_spans:
        start = boundaries[span["start"]]
        end = boundaries[span["end"]]
        if end > start:
            remapped_spans.append({**span, "start": start, "end": end})

    clipped_spans = normalize_style_spans(remapped_spans, len(new_text))
    if not clipped_spans:
        return new_text

    return {
        "__kind__": RICH_TEXT_KIND,
        "text": new_text,
        "spans": clipped_spans,
        "html": build_html_fragment(new_text, clipped_spans),
        "rtf": build_rtf_document(new_text, clipped_spans),
    }


def load_value_into_text_widget(text_widget, value):
    text_widget.delete("1.0", tk.END)
    plain_text = extract_plain_text(value)
    if plain_text:
        text_widget.insert("1.0", plain_text)
    clear_text_styles(text_widget)
    normalized = normalize_rich_text_payload(value)
    if not is_rich_text_payload(normalized):
        return
    masks = _build_masks(plain_text, normalized.get("spans"))
    if masks:
        _apply_masks_to_range(text_widget, 0, masks)
