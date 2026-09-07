from __future__ import annotations

import math
import re
from dataclasses import dataclass

from ..models import Scene

_SENTENCE_SPLIT_RE = re.compile(
    r"(?:(?<=[.!?])|(?<=[.!?][\"'”’]))\s+(?=[A-ZÀ-ỸĐ0-9])"
)
_FLOW_DURATIONS = (4, 6, 8, 10)


class SubclipPlanningError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class FlowSubclip:
    index: int
    count: int
    semantic_duration: int
    flow_duration: int
    start_second: int
    end_second: int
    beats: tuple[str, ...]
    prompt: str


def action_beats(action: str) -> list[str]:
    text = " ".join((action or "").split())
    if not text:
        return []
    beats = [item.strip() for item in _SENTENCE_SPLIT_RE.split(text) if item.strip()]
    return beats or [text]


def _semantic_durations(total_seconds: int, count: int) -> list[int]:
    if count <= 0:
        raise SubclipPlanningError("Subclip count phải lớn hơn 0")
    base, remainder = divmod(total_seconds, count)
    durations = [base + (1 if index < remainder else 0) for index in range(count)]
    if min(durations) < 4 or max(durations) > 10:
        raise SubclipPlanningError(
            f"Không thể chia {total_seconds}s thành {count} subclip trong khoảng 4-10s"
        )
    return durations


def _flow_duration(semantic_duration: int) -> int:
    try:
        return next(item for item in _FLOW_DURATIONS if item >= semantic_duration)
    except StopIteration as exc:
        raise SubclipPlanningError(
            f"Không có Google Flow duration cho subclip {semantic_duration}s"
        ) from exc


def _partition_beats(
    beats: list[str],
    durations: list[int],
) -> list[tuple[str, ...]]:
    count = len(durations)
    if len(beats) < count:
        raise SubclipPlanningError(
            "Scene không có đủ beat độc lập để chia subclip mà không lặp narrative."
        )

    weights = [max(1, len(item)) for item in beats]
    total_weight = sum(weights)
    total_duration = sum(durations)
    partitions: list[tuple[str, ...]] = []
    cursor = 0
    consumed_weight = 0

    for index, duration in enumerate(durations):
        remaining_parts = count - index
        remaining_beats = len(beats) - cursor
        if remaining_parts == 1:
            partitions.append(tuple(beats[cursor:]))
            break

        target_weight = total_weight * duration / total_duration
        chosen: list[str] = []
        chosen_weight = 0
        max_take = remaining_beats - (remaining_parts - 1)
        while len(chosen) < max_take:
            next_weight = weights[cursor + len(chosen)]
            if chosen and chosen_weight + next_weight > target_weight:
                break
            chosen.append(beats[cursor + len(chosen)])
            chosen_weight += next_weight

        if not chosen:
            chosen = [beats[cursor]]
            chosen_weight = weights[cursor]

        partitions.append(tuple(chosen))
        cursor += len(chosen)
        consumed_weight += chosen_weight
        total_weight -= chosen_weight
        total_duration -= duration

    if sum(len(item) for item in partitions) != len(beats):
        raise SubclipPlanningError("Subclip beat partition bị mất hoặc lặp beat")
    return partitions


def _dialogue_lock(scene: Scene, beat_text: str) -> str:
    lines: list[str] = []
    folded = beat_text.casefold()
    for dialogue in scene.dialogues:
        text = " ".join(dialogue.text.split())
        if text and text.casefold() in folded:
            lines.append(
                f'- {dialogue.character_id}|{dialogue.delivery}: "{text}"'
            )
    if not lines:
        return "No authored dialogue belongs to this subclip. Do not invent speech."
    return "\n".join(lines)


def _build_prompt(
    scene: Scene,
    *,
    index: int,
    count: int,
    semantic_duration: int,
    flow_duration: int,
    start_second: int,
    end_second: int,
    beats: tuple[str, ...],
) -> str:
    beat_text = " ".join(beats)
    hold = (
        ""
        if flow_duration == semantic_duration
        else (
            f"\nTIMING TAIL: finish the owned beat by {semantic_duration}.0s, then hold "
            f"the exact final physical state until {flow_duration}.0s. Do not begin the "
            "next beat; TH Media will trim this hold tail."
        )
    )
    return f"""SCENE SUBCLIP CONTRACT — {scene.id} — PART {index}/{count}

NARRATIVE OWNERSHIP:
This clip owns ONLY the authored beats listed below. Do not replay a completed beat from an
earlier part and do not anticipate any beat assigned to a later part.

OWNED SOURCE-GROUNDED BEATS:
{beat_text}

CLIP POSITION:
Scene time {start_second}.0s through {end_second}.0s of {scene.duration}.0s total.
The first frame supplied by TH Media is the exact physical entry state when present. Preserve it.
The final frame becomes the exact continuity anchor for the next subclip.

VISUAL IDENTITY / WORLD LOCK:
{scene.image_plan.identity_lock or scene.visual_plan.lock_prompt}
{scene.image_plan.composition_lock}

CAMERA / LIGHT / ATMOSPHERE:
Camera: {scene.camera}
Lighting: {scene.lighting}
Atmosphere: {scene.atmosphere}

DIALOGUE OWNERSHIP:
{_dialogue_lock(scene, beat_text)}

AUDIO:
Preserve source-grounded ambience and physically motivated sound for the owned beats only.
Do not invent narration, dialogue, future sound cues or repeat prior sound cues.

DURATION:
Semantic duration: {semantic_duration}s. Google Flow generation duration: {flow_duration}s.
{hold}

NEGATIVE CONSTRAINTS:
{scene.image_plan.negative_prompt}
"""


def plan_flow_subclips(scene: Scene) -> list[FlowSubclip]:
    if scene.duration <= 10:
        return []
    count = math.ceil(scene.duration / 10)
    durations = _semantic_durations(scene.duration, count)
    beats = action_beats(scene.action)
    partitions = _partition_beats(beats, durations)

    plans: list[FlowSubclip] = []
    cursor = 0
    for index, (duration, owned_beats) in enumerate(
        zip(durations, partitions, strict=True),
        start=1,
    ):
        start_second = cursor
        end_second = cursor + duration
        flow_duration = _flow_duration(duration)
        plans.append(
            FlowSubclip(
                index=index,
                count=count,
                semantic_duration=duration,
                flow_duration=flow_duration,
                start_second=start_second,
                end_second=end_second,
                beats=owned_beats,
                prompt=_build_prompt(
                    scene,
                    index=index,
                    count=count,
                    semantic_duration=duration,
                    flow_duration=flow_duration,
                    start_second=start_second,
                    end_second=end_second,
                    beats=owned_beats,
                ),
            )
        )
        cursor = end_second

    if cursor != scene.duration:
        raise SubclipPlanningError("Subclip durations không khớp duration scene")
    flattened = [beat for plan in plans for beat in plan.beats]
    if flattened != beats:
        raise SubclipPlanningError("Subclip narrative coverage không còn đúng thứ tự nguồn")
    return plans
