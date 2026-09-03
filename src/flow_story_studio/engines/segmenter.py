"""Semantic-ish scene segmentation without external AI dependencies."""

from __future__ import annotations

import re

BOUNDARY_HINTS = re.compile(
    r"\b(sau đó|tiếp theo|đột nhiên|trong khi đó|cuối cùng|sáng hôm sau|tối hôm đó|"
    r"meanwhile|later|suddenly|finally|the next day)\b",
    re.IGNORECASE,
)

NON_NARRATIVE_SECTION = re.compile(
    r"\b(nhân vật|character(?: bible)?|thông tin chung|tổng quan|thể loại|"
    r"định dạng|phong cách|ghi chú sản xuất)\b",
    re.IGNORECASE,
)
NARRATIVE_SECTION = re.compile(
    r"\b(kịch bản chi tiết|nội dung chi tiết|phân cảnh|screenplay|"
    r"hồi\s+\d+|cảnh\s+\d+|scene\s+\d+)\b",
    re.IGNORECASE,
)
METADATA_LINE = re.compile(
    r"^(thể loại|thời lượng(?: dự kiến)?|bối cảnh chung|không khí|tỷ lệ|"
    r"độ phân giải|phong cách hình ảnh|mục tiêu)\s*:",
    re.IGNORECASE,
)


def narrative_text(text: str) -> str:
    """Remove screenplay front matter while preserving the actual ordered narrative."""
    normalized = re.sub(r"\r\n?", "\n", text).strip()
    lines = normalized.splitlines()
    has_narrative_section = any(
        line.lstrip().startswith("#") and NARRATIVE_SECTION.search(line) for line in lines
    )
    active = not has_narrative_section
    output: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            if active and output and output[-1] != "":
                output.append("")
            continue
        is_heading = bool(re.match(r"^#{1,6}\s+", stripped))
        plain = re.sub(r"^#{1,6}\s+", "", stripped)
        plain = re.sub(r"\*\*|__|`", "", plain).strip()
        if is_heading:
            if NON_NARRATIVE_SECTION.search(plain):
                active = False
                continue
            if NARRATIVE_SECTION.search(plain):
                active = True
                if output and output[-1] != "":
                    output.append("")
                continue
            # Headings are navigation, not visual action.
            continue
        if not active or METADATA_LINE.match(plain):
            continue
        plain = re.sub(r"^[-*+]\s+", "", plain)
        plain = re.sub(r"^\d+[.)]\s+", "", plain)
        if plain:
            output.append(plain)
    cleaned = "\n".join(output).strip()
    # If a highly unusual document was stripped completely, retain a conservative
    # Markdown-free fallback rather than returning no scenes.
    if not cleaned:
        cleaned = re.sub(r"(?m)^#{1,6}\s+.*$", "", normalized)
        cleaned = re.sub(r"\*\*|__|`", "", cleaned)
        cleaned = "\n".join(
            line for line in cleaned.splitlines() if not METADATA_LINE.match(line.strip())
        ).strip()
    return cleaned


def split_sentences(text: str) -> list[str]:
    normalized = re.sub(r"\r\n?", "\n", text).strip()
    # Keep closing quotation marks with the preceding sentence.
    marked = re.sub(r"([.!?…][”\"’']?)\s+", r"\1<SPLIT>", normalized)
    chunks = re.split(r"<SPLIT>|\n{2,}", marked)
    return [re.sub(r"\s+", " ", part).strip() for part in chunks if part.strip()]


def segment_story(text: str, duration: int) -> list[str]:
    """Group sentences by speech capacity while respecting narrative transitions."""
    sentences = split_sentences(narrative_text(text))
    if not sentences:
        return []
    target_words = max(8, round(duration * 2.2))
    hard_limit = max(target_words + 5, round(duration * 2.8))
    scenes: list[str] = []
    current: list[str] = []
    count = 0

    for sentence in sentences:
        words = sentence.split()
        starts_new_beat = bool(BOUNDARY_HINTS.search(sentence)) and current
        would_overflow = count + len(words) > hard_limit and current
        if starts_new_beat or would_overflow:
            scenes.append(" ".join(current))
            current, count = [], 0
        if len(words) > hard_limit:
            clauses = [
                part.strip()
                for part in re.split(
                    r"(?<=[,;:])\s+|(?=\b(?:sau đó|tiếp theo|trong khi đó|"
                    r"ngoài đường|cuối cùng)\b)",
                    sentence,
                    flags=re.IGNORECASE,
                )
                if part.strip()
            ]
            if len(clauses) > 1:
                if current:
                    scenes.append(" ".join(current))
                    current, count = [], 0
                for clause in clauses:
                    if current and count + len(clause.split()) > hard_limit:
                        scenes.append(" ".join(current))
                        current, count = [], 0
                    current.append(clause)
                    count += len(clause.split())
                continue
            while len(words) > hard_limit:
                take = words[:target_words]
                words = words[target_words:]
                if current:
                    scenes.append(" ".join(current))
                    current, count = [], 0
                scenes.append(" ".join(take))
            if words:
                current = [" ".join(words)]
                count = len(words)
        else:
            current.append(sentence)
            count += len(words)
    if current:
        scenes.append(" ".join(current))
    return scenes


def speaking_duration(text: str, minimum: int = 4, maximum: int = 30) -> int:
    words = max(1, len(text.split()))
    seconds = round(words / 2.35)
    return max(minimum, min(maximum, seconds))
