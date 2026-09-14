"""Deterministic, user-configured corrections for voice transcripts."""

import re


MAX_REPLACEMENTS = 200
MAX_TERM_LENGTH = 120
MAX_REPLACEMENT_LENGTH = 240


def validate_replacements(value):
    """Return a normalized replacement mapping, or an empty mapping.

    Settings are user-editable JSON, so malformed values are ignored rather
    than allowed to affect the voice worker.
    """
    if not isinstance(value, dict) or len(value) > MAX_REPLACEMENTS:
        return {}
    result = {}
    for source, replacement in value.items():
        if not isinstance(source, str) or not isinstance(replacement, str):
            return {}
        source = source.strip()
        if (
            not source
            or not replacement.strip()
            or len(source) > MAX_TERM_LENGTH
            or len(replacement) > MAX_REPLACEMENT_LENGTH
        ):
            return {}
        if source.casefold() in {item.casefold() for item in result}:
            return {}
        result[source] = replacement
    return result


class VoiceTextReplacements:
    """Compiled, one-pass phrase replacer."""

    def __init__(self, replacements=None):
        self.replacements = validate_replacements(replacements)
        self._lookup = {
            source.casefold(): replacement
            for source, replacement in self.replacements.items()
        }
        alternatives = sorted(self._lookup, key=len, reverse=True)
        self._pattern = (
            re.compile(
                r"(?<!\w)(?:" + "|".join(re.escape(item) for item in alternatives) + r")(?!\w)",
                re.UNICODE,
            )
            if alternatives
            else None
        )

    def apply(self, text):
        if not isinstance(text, str) or self._pattern is None:
            return text
        folded, index_map, valid_starts, valid_ends = _casefold_with_index(text)
        if not folded:
            return text
        pieces = []
        cursor = 0
        for match in self._pattern.finditer(folded):
            if match.start() not in valid_starts or match.end() not in valid_ends:
                continue
            original_start = index_map[match.start()]
            original_end = index_map[match.end() - 1] + 1
            if original_start < cursor:
                continue
            pieces.append(text[cursor:original_start])
            pieces.append(self._lookup[match.group()])
            cursor = original_end
        if not pieces:
            return text
        pieces.append(text[cursor:])
        return "".join(pieces)


def _casefold_with_index(text):
    """Case-fold text while retaining complete original-character spans."""
    folded_parts = []
    index_map = []
    valid_starts = set()
    valid_ends = set()
    offset = 0
    for original_index, character in enumerate(text):
        folded_character = character.casefold()
        if not folded_character:
            continue
        valid_starts.add(offset)
        folded_parts.append(folded_character)
        index_map.extend([original_index] * len(folded_character))
        offset += len(folded_character)
        valid_ends.add(offset)
    return "".join(folded_parts), index_map, valid_starts, valid_ends


def apply_voice_replacements(text, replacements):
    return VoiceTextReplacements(replacements).apply(text)
