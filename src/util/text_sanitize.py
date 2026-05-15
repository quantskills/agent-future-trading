import re


_MOJIBAKE_MARKERS = (
    "\u00c3",
    "\u00c2",
    "\u00c4",
    "\u00c5",
    "\u00c6",
    "\u00c7",
    "\u00c8",
    "\u00c9",
    "\u00e3",
    "\u00e4",
    "\u00e5",
    "\u00e6",
    "\u00e7",
    "\u00e8",
    "\u00e9",
    "\u20ac",
    "\u0153",
    "\u0161",
    "\u017e",
    "\u2018",
    "\u2019",
    "\u201c",
    "\u201d",
    "\u2026",
)

_MOJIBAKE_CHAR_RE = re.compile(r"[\u0080-\u009f\u00c0-\u00ff\u2018-\u2026\u20ac]")
_MOJIBAKE_SEGMENT_RE = re.compile(r"[\u0080-\u009f\u00c0-\u00ff\u2018-\u2026\u20ac]{2,}")
_CJK_MOJIBAKE_MARKERS = (
    "\u6d93",
    "\u56e7",
    "\u60c3",
    "\u6d7c",
    "\u6a3f",
    "\u944d",
    "\u9428",
    "\u9358",
    "\u93c8",
    "\u9366",
    "\u95c2",
)


def _count_cjk(text: str) -> int:
    return sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")


def _marker_hit_count(text: str) -> int:
    marker_hits = sum(text.count(marker) for marker in _MOJIBAKE_MARKERS)
    char_hits = len(_MOJIBAKE_CHAR_RE.findall(text))
    return marker_hits + char_hits


def _looks_like_mojibake(text: str) -> bool:
    if not text:
        return False
    return _marker_hit_count(text) >= 2 and _count_cjk(text) == 0


def _repair_utf8_mojibake(text: str) -> str:
    candidates = [text]

    for source_encoding in ("latin1", "cp1252"):
        try:
            candidates.append(text.encode(source_encoding).decode("utf-8"))
        except UnicodeError:
            continue

    best = text
    best_score = (_count_cjk(text), -_marker_hit_count(text))

    for candidate in candidates[1:]:
        candidate_score = (_count_cjk(candidate), -_marker_hit_count(candidate))
        if candidate_score > best_score:
            best = candidate
            best_score = candidate_score

    return best


def _repair_mojibake_segments(text: str) -> str:
    def replace_segment(match: re.Match) -> str:
        segment = match.group(0)
        if _marker_hit_count(segment) < 2:
            return segment
        return _repair_utf8_mojibake(segment)

    return _MOJIBAKE_SEGMENT_RE.sub(replace_segment, text)


def _cjk_mojibake_hit_count(text: str) -> int:
    return sum(text.count(marker) for marker in _CJK_MOJIBAKE_MARKERS)


def _repair_gbk_mojibake(text: str) -> str:
    if _cjk_mojibake_hit_count(text) == 0:
        return text

    try:
        candidate = text.encode("gbk").decode("utf-8")
    except UnicodeError:
        return text

    if _cjk_mojibake_hit_count(candidate) < _cjk_mojibake_hit_count(text):
        return candidate
    return text


def sanitize_visible_text(text: str) -> str:
    """Normalize human-facing text before it reaches logs or prompts."""
    if not text:
        return text

    cleaned = str(text)

    if _looks_like_mojibake(cleaned):
        cleaned = _repair_utf8_mojibake(cleaned)
    elif _marker_hit_count(cleaned) >= 2:
        cleaned = _repair_mojibake_segments(cleaned)

    cleaned = _repair_gbk_mojibake(cleaned)

    replacements = {
        chr(0x00D7): "*",
        chr(0x2192): "->",
        chr(0x2014): "-",
        chr(0x8133): "*",
        "\u6d5c\u30a6\u58ca": " quality",
    }
    for old, new in replacements.items():
        cleaned = cleaned.replace(old, new)

    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned.strip()
