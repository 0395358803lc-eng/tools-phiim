from __future__ import annotations

import re
import unicodedata
from copy import deepcopy
from difflib import SequenceMatcher

from ..models import Project, Scene

_STOPWORDS = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "of",
    "to",
    "in",
    "on",
    "at",
    "with",
    "scene",
    "shot",
    "camera",
    "cinematic",
    "canh",
    "mot",
    "va",
    "cua",
    "trong",
    "tai",
    "voi",
    "anh",
    "co",
}


def _fold(value: str) -> str:
    normalized = unicodedata.normalize(
        "NFKD",
        str(value).casefold().replace("đ", "d"),
    )
    ascii_text = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return " ".join(re.findall(r"[a-z0-9]+", ascii_text))


def _tokens(value: str) -> set[str]:
    return {
        token
        for token in _fold(value).split()
        if len(token) > 1 and token not in _STOPWORDS
    }


def semantic_similarity(left: str, right: str) -> float:
    a = _fold(left)
    b = _fold(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    token_score = len(left_tokens & right_tokens) / max(1, len(left_tokens | right_tokens))
    sequence_score = SequenceMatcher(None, a, b).ratio()
    return max(token_score, sequence_score)


def duplicate_scene_pairs(
    project: Project,
    *,
    comparison_window: int = 16,
) -> list[tuple[str, str, float, float, float]]:
    """Detect AI beat copying without quadratic cost on very long screenplays."""
    scenes = sorted(project.scenes, key=lambda item: item.order)
    duplicates: list[tuple[str, str, float, float, float]] = []
    exact_actions: dict[str, list[int]] = {}

    for index, current in enumerate(scenes):
        candidate_indices = set(range(max(0, index - comparison_window), index))
        action_key = _fold(current.action)

        # Exact copied actions are checked globally, including beyond the local AI batch/window.
        for previous_index in exact_actions.get(action_key, [])[-4:]:
            candidate_indices.add(previous_index)

        for previous_index in sorted(candidate_indices):
            previous = scenes[previous_index]
            source_similarity = semantic_similarity(previous.source_text, current.source_text)
            # Do not punish an authored screenplay that intentionally repeats a beat.
            if source_similarity >= 0.80:
                continue
            action_similarity = semantic_similarity(previous.action, current.action)
            summary_similarity = semantic_similarity(previous.summary, current.summary)
            duplicated = (
                action_similarity >= 0.94
                or (
                    action_similarity >= 0.86
                    and summary_similarity >= 0.90
                )
            )
            if duplicated:
                duplicates.append(
                    (
                        previous.id,
                        current.id,
                        action_similarity,
                        summary_similarity,
                        source_similarity,
                    )
                )

        if action_key:
            exact_actions.setdefault(action_key, []).append(index)

    return duplicates


def restore_ai_duplicate_beats(
    project: Project,
    source_project: Project | None,
) -> Project:
    """Restore copied AI beats from the deterministic source-grounded draft."""
    if source_project is None:
        return project

    source_by_id = {scene.id: scene for scene in source_project.scenes}
    # Multiple duplicated targets are repaired in order and then reevaluated.
    for _ in range(max(1, len(project.scenes))):
        duplicates = duplicate_scene_pairs(project)
        if not duplicates:
            break
        repaired_any = False
        repaired_targets: set[str] = set()
        for previous_id, current_id, *_scores in duplicates:
            if current_id in repaired_targets:
                continue
            current = next(
                (scene for scene in project.scenes if scene.id == current_id),
                None,
            )
            source = source_by_id.get(current_id)
            if current is None or source is None:
                continue
            current.action = source.action
            current.summary = source.summary
            # If the AI copied camera staging along with the beat, restore the source draft's
            # deterministic camera as well; later finalization may still sanitize cast conflicts.
            previous = next(
                (scene for scene in project.scenes if scene.id == previous_id),
                None,
            )
            if (
                previous is not None
                and semantic_similarity(previous.camera, current.camera) >= 0.94
            ):
                current.camera = source.camera
            warning = (
                f"xKiro duplicated production beat from {previous_id}; "
                "restored source-grounded action/summary"
            )
            if warning not in current.warnings:
                current.warnings.append(warning)
            repaired_targets.add(current_id)
            repaired_any = True
        if not repaired_any:
            break
    return project


def clone_scene_semantics(scene: Scene) -> dict[str, object]:
    """Small helper used by tests/diagnostics without copying render/runtime state."""
    return deepcopy(
        {
            "id": scene.id,
            "summary": scene.summary,
            "action": scene.action,
            "camera": scene.camera,
            "source_text": scene.source_text,
        }
    )
