"""Deterministic local titles for recordings created without a user title."""

import re
import time

from i18n import tr


_FALLBACK_WORDS = ("Gravacao", "sem", "transcricao")
_FALLBACK_DESCRIPTION = "-".join(_FALLBACK_WORDS)
_AUTO_TITLE = re.compile(
    rf"^(?P<stamp>\d{{4}}-\d{{2}}-\d{{2}}-\d{{2}}-\d{{2}})-{_FALLBACK_DESCRIPTION}$",
    re.IGNORECASE,
)
_SESSION_STAMP = re.compile(r"^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})\d{2}(?:-|$)")
_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_STOPWORDS = frozenset({
    "a", "as", "ao", "aos", "de", "da", "das", "do", "dos", "e", "em", "eu",
    "hoje", "nos", "o", "os", "para", "por", "que", "sobre", "um", "uma", "vamos",
    "agora", "aqui", "bom", "boa", "dia", "entao", "gente", "pessoal", "the", "a",
    "an", "and", "for", "from", "in", "of", "on", "that", "this", "to", "we",
})


def initial_recording_title(title, timestamp=None):
    """Preserve a supplied title or create a local timestamp placeholder."""
    if not isinstance(title, str):
        raise ValueError(tr("O título da gravação deve ser texto."))
    title = title.strip()
    if title:
        return title
    moment = time.localtime(time.time() if timestamp is None else float(timestamp))
    return time.strftime("%Y-%m-%d-%H-%M-", moment) + _FALLBACK_DESCRIPTION


def refine_recording_title(title, session_id, segments):
    """Replace only an automatic placeholder with 3–5 transcript words."""
    if not isinstance(title, str) or not isinstance(session_id, str):
        raise ValueError(tr("Os dados do título automático são inválidos."))
    match = _AUTO_TITLE.fullmatch(title.strip())
    if title.strip() and match is None:
        return title
    words = _description_words(segments)
    if not words:
        return title or _session_title(session_id, _FALLBACK_WORDS)
    stamp = match.group("stamp") if match else _session_stamp(session_id)
    return stamp + "-" + "-".join(words)


def _session_title(session_id, words):
    return _session_stamp(session_id) + "-" + "-".join(words)


def _session_stamp(session_id):
    match = _SESSION_STAMP.match(session_id)
    if match is None:
        return time.strftime("%Y-%m-%d-%H-%M", time.localtime())
    return "-".join(match.groups())


def _description_words(segments):
    candidates = []
    for segment in segments:
        text = segment.get("text") if isinstance(segment, dict) else None
        if not isinstance(text, str):
            continue
        for raw in _WORD.findall(text):
            if not any(character.isalpha() for character in raw):
                continue
            candidates.append(raw[:40])
            if len(candidates) >= 64:
                break
        if len(candidates) >= 64:
            break
    if len(candidates) < 3:
        return []
    selected = [
        index for index, word in enumerate(candidates)
        if len(word) > 1 and word.casefold() not in _STOPWORDS
    ][:5]
    if len(selected) < 3:
        selected_set = set(selected)
        for index in range(len(candidates)):
            if index not in selected_set:
                selected.append(index)
                selected_set.add(index)
            if len(selected) >= 3:
                break
    words = [_display_word(candidates[index]) for index in sorted(selected[:5])]
    return words[:5]


def _display_word(word):
    return word if word.isupper() else word[:1].upper() + word[1:]


__all__ = ["initial_recording_title", "refine_recording_title"]
