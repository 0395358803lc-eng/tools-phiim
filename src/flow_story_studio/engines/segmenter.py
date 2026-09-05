"""Semantic-ish scene segmentation without external AI dependencies."""

from __future__ import annotations

import re

BOUNDARY_HINTS = re.compile(
    r"\b(sau đó|tiếp theo|đột nhiên|trong khi đó|cuối cùng|sáng hôm sau|tối hôm đó|"
    r"meanwhile|later|suddenly|finally|the next day)\b",
    re.IGNORECASE,
)

NON_NARRATIVE_SECTION = re.compile(
    r"\b(nhân vật|characters?|character(?: bible)?|cast|props?|objects?|"
    r"thông tin chung|tổng quan|thể loại|định dạng|phong cách|ghi chú sản xuất)\b",
    re.IGNORECASE,
)
NARRATIVE_SECTION = re.compile(
    r"\b(kịch bản chi tiết|nội dung chi tiết|phân cảnh|screenplay|"
    r"hồi\s+\d+|cảnh\s+\d+|scene\s+\d+)\b",
    re.IGNORECASE,
)
METADATA_LINE = re.compile(
    r"^(?:target\s*runtime|runtime|duration|genre|format|aspect\s*ratio|resolution|style|"
    r"purpose|audience|thể\s*loại|thời\s*lượng(?:\s*dự\s*kiến|\s*mục\s*tiêu)?|"
    r"bối\s*cảnh\s*chung|không\s*khí|tỷ\s*lệ|độ\s*phân\s*giải|"
    r"phong\s*cách\s*hình\s*ảnh|mục\s*tiêu)\s*:",
    re.IGNORECASE,
)
SCENE_CONTEXT_PREFIX = "[SCENE CONTEXT] "
SCENE_CONTEXT_SUFFIX = " [END CONTEXT]"
SCENE_CONTEXT_HEADING = re.compile(
    r"\b(cảnh\s+\d+|scene\s+\d+|flashback|trở lại hiện tại|một năm trước|cảnh cuối)\b",
    re.IGNORECASE,
)


def _append_scene_context(output: list[str], plain: str) -> None:
    while output and output[-1] == "":
        output.pop()
    marker = f"{SCENE_CONTEXT_PREFIX}{plain}{SCENE_CONTEXT_SUFFIX}"
    if output and output[-1].startswith(SCENE_CONTEXT_PREFIX):
        current = output[-1].removesuffix(SCENE_CONTEXT_SUFFIX)
        output[-1] = f"{current} | {plain}{SCENE_CONTEXT_SUFFIX}"
        return
    if output and output[-1] != "":
        output.append("")
    output.append(marker)


RUNTIME_RE = re.compile(
    r"(?:thời\s*lượng(?:\s*(?:mục\s*tiêu|dự\s*kiến))?|target\s*runtime|runtime|duration)"
    r"[^\n:]{0,30}:?\s*(?:khoảng|about|approx(?:imately)?|~)?\s*(?:(?P<hours>\d+(?:[.,]\d+)?)\s*(?:giờ|hours?|hrs?|h)\s*)?"
    r"(?:(?P<minutes>\d+(?:[.,]\d+)?)\s*(?:phút|minutes?|mins?|min|m)\s*)?"
    r"(?:(?P<seconds>\d+(?:[.,]\d+)?)\s*(?:giây|seconds?|secs?|sec|s))?",
    re.IGNORECASE,
)


def target_runtime_seconds(text: str) -> int | None:
    """Read an explicit production runtime without mistaking story timeline numbers for it."""
    for line in re.sub(r"\r\n?", "\n", text).splitlines():
        clean_line = re.sub(r"[*_`#>]+", " ", line)
        clean_line = re.sub(r"^\s*[-+]\s+", "", clean_line)
        match = RUNTIME_RE.search(clean_line)
        if not match:
            continue
        values = {key: value for key, value in match.groupdict().items() if value}
        if not values:
            continue
        hours = float(values.get("hours", "0").replace(",", "."))
        minutes = float(values.get("minutes", "0").replace(",", "."))
        seconds = float(values.get("seconds", "0").replace(",", "."))
        total = round(hours * 3600 + minutes * 60 + seconds)
        if 10 <= total <= 24 * 3600:
            return total
    return None


def _scene_blocks(scenes: list[str]) -> list[list[str]]:
    blocks: list[list[str]] = []
    current: list[str] = []
    for scene in scenes:
        if scene.lstrip().startswith(SCENE_CONTEXT_PREFIX) and current:
            blocks.append(current)
            current = []
        current.append(scene)
    if current:
        blocks.append(current)
    return blocks


def _merge_block(block: list[str], target_count: int) -> list[str]:
    if len(block) <= target_count:
        return block
    weights = [max(1, len(item.split())) for item in block]
    total = sum(weights)
    result: list[str] = []
    start = 0
    remaining_weight = total
    for slot in range(target_count):
        remaining_slots = target_count - slot
        if remaining_slots == 1:
            result.append(" ".join(block[start:]))
            break
        target_weight = remaining_weight / remaining_slots
        acc = 0
        end = start
        max_end = len(block) - (remaining_slots - 1)
        while end < max_end:
            next_weight = weights[end]
            if end > start and acc + next_weight > target_weight:
                break
            acc += next_weight
            end += 1
        if end == start:
            end += 1
            acc += weights[start]
        result.append(" ".join(block[start:end]))
        remaining_weight -= acc
        start = end
    return result


def consolidate_to_runtime(
    scenes: list[str], runtime_seconds: int | None, scene_duration: int
) -> list[str]:
    """Reduce over-segmentation while preserving explicit screenplay scene boundaries."""
    if not scenes or runtime_seconds is None:
        return scenes
    target_count = max(1, round(runtime_seconds / max(4, scene_duration)))
    blocks = _scene_blocks(scenes)
    target_count = max(len(blocks), min(len(scenes), target_count))
    if len(scenes) <= target_count:
        return scenes
    weights = [sum(max(1, len(scene.split())) for scene in block) for block in blocks]
    allocation = [1 for _ in blocks]
    remaining = target_count - len(blocks)
    while remaining > 0:
        candidates = [i for i, block in enumerate(blocks) if allocation[i] < len(block)]
        if not candidates:
            break
        index = max(candidates, key=lambda i: weights[i] / allocation[i])
        allocation[index] += 1
        remaining -= 1
    output: list[str] = []
    for block, count in zip(blocks, allocation, strict=True):
        output.extend(_merge_block(block, count))
    return output


def allocate_scene_durations(
    scenes: list[str], runtime_seconds: int | None, default_duration: int
) -> list[int]:
    if not scenes:
        return []
    if runtime_seconds is None:
        return [min(30, max(default_duration, speaking_duration(scene))) for scene in scenes]
    minimum_total = 4 * len(scenes)
    maximum_total = 30 * len(scenes)
    target = max(minimum_total, min(maximum_total, runtime_seconds))
    weights = [max(1, len(scene.split())) for scene in scenes]
    durations = [4 for _ in scenes]
    remaining = target - minimum_total
    while remaining > 0:
        candidates = [i for i, value in enumerate(durations) if value < 30]
        if not candidates:
            break
        index = max(candidates, key=lambda i: weights[i] / durations[i])
        durations[index] += 1
        remaining -= 1
    return durations


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
                if SCENE_CONTEXT_HEADING.search(plain):
                    _append_scene_context(output, plain)
                continue
            if active and SCENE_CONTEXT_HEADING.search(plain):
                _append_scene_context(output, plain)
                continue
            # Non-context headings are navigation, not visual action.
            continue
        plain_section_heading = bool(
            re.fullmatch(
                r"(?:characters?|character bible|cast|props?|objects?|metadata|story bible)",
                plain,
                re.IGNORECASE,
            )
        )
        if plain_section_heading:
            active = False
            continue
        plain_scene_heading = bool(
            re.match(r"^(?:cảnh|scene)\s*\d+\b", plain, re.IGNORECASE)
            or re.match(r"^(?:INT\.?|EXT\.?|INT\./EXT\.?)\s+", plain, re.IGNORECASE)
        )
        if plain_scene_heading:
            active = True
            _append_scene_context(output, plain)
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
        starts_new_beat = (
            sentence.startswith(SCENE_CONTEXT_PREFIX) or bool(BOUNDARY_HINTS.search(sentence))
        ) and current
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
    return consolidate_to_runtime(scenes, target_runtime_seconds(text), duration)


def speaking_duration(text: str, minimum: int = 4, maximum: int = 30) -> int:
    words = max(1, len(text.split()))
    seconds = round(words / 2.35)
    return max(minimum, min(maximum, seconds))
