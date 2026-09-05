"""Cross-scene continuity validation and safe auto-repair."""

from __future__ import annotations

from copy import deepcopy

from ..models import Project, Scene
from .segmenter import SCENE_CONTEXT_PREFIX


def _scene_context(source_text: str) -> str:
    stripped = source_text.lstrip()
    if not stripped.startswith(SCENE_CONTEXT_PREFIX):
        return ""
    context = stripped[len(SCENE_CONTEXT_PREFIX) :]
    if "[END CONTEXT]" in context:
        context = context.split("[END CONTEXT]", 1)[0]
    return context.casefold()


def is_direct_continuation(previous: Scene | None, current: Scene) -> bool:
    if previous is None:
        return False
    if previous.location_id != current.location_id:
        return False

    current_context = _scene_context(current.source_text)
    if not current_context:
        return True
    if "song song" in current_context or "parallel" in current_context:
        return False
    continuous = "liên tục" in current_context or "continuous" in current_context
    if not continuous:
        return False

    previous_context = _scene_context(previous.source_text)
    previous_flashback = "flashback" in previous_context
    current_flashback = "flashback" in current_context
    return previous_flashback == current_flashback


def can_inherit_previous_frame(previous: Scene | None, current: Scene) -> bool:
    """Return whether a previous final frame is a safe literal start-frame anchor."""
    if previous is None or not is_direct_continuation(previous, current):
        return False
    if set(previous.characters) != set(current.characters):
        return False

    previous_state = previous.end_state
    current_state = current.start_state
    return (
        previous_state.character_positions == current_state.character_positions
        and previous_state.character_wardrobe == current_state.character_wardrobe
        and previous_state.prop_positions == current_state.prop_positions
        and previous_state.time == current_state.time
        and previous_state.weather == current_state.weather
    )


def scene_warnings(previous: Scene | None, current: Scene, project: Project) -> list[str]:
    warnings: list[str] = []
    character_ids = {item.id for item in project.characters}
    location_ids = {item.id for item in project.locations}
    unknown = set(current.characters) - character_ids
    if unknown:
        warnings.append(f"Nhân vật chưa có trong Bible: {', '.join(sorted(unknown))}")
    if current.location_id not in location_ids:
        warnings.append(f"Location {current.location_id} chưa có trong Bible")
    if is_direct_continuation(previous, current):
        if previous.end_state.time != current.start_state.time:
            warnings.append("Mốc thời gian đầu cảnh không khớp trạng thái cuối cảnh trước")
        for char_id, position in previous.end_state.character_positions.items():
            next_position = current.start_state.character_positions.get(char_id)
            if next_position and next_position != position:
                warnings.append(f"{char_id} đổi vị trí mà chưa có diễn biến chuyển tiếp")
        for prop_id, position in previous.end_state.prop_positions.items():
            next_position = current.start_state.prop_positions.get(prop_id)
            if next_position and next_position != position:
                warnings.append(f"{prop_id} đổi vị trí giữa hai cảnh")
    if not current.action.strip():
        warnings.append("Cảnh chưa có hành động rõ ràng")
    if current.duration < max(4, round(len(current.voiceover.split()) / 2.8)):
        warnings.append("Voiceover có thể dài hơn thời lượng cảnh")
    return warnings


def check_project(project: Project, auto_fix: bool = False) -> Project:
    result = deepcopy(project)
    all_warnings: list[str] = []
    active_warnings: list[str] = []
    previous: Scene | None = None
    for index, scene in enumerate(result.scenes):
        retained = [
            warning
            for warning in scene.warnings
            if warning.startswith("Thay đổi ở")
            or warning.startswith("Render failed:")
            or warning.startswith("xKiro ")
        ]
        scene.order = index + 1
        if auto_fix and is_direct_continuation(previous, scene):
            scene.start_state = deepcopy(previous.end_state)
        current_warnings = scene_warnings(previous, scene, result)
        scene.warnings = retained + current_warnings
        all_warnings.extend(f"{scene.id}: {warning}" for warning in scene.warnings)
        active_warnings.extend(f"{scene.id}: {warning}" for warning in current_warnings)
        previous = scene
    penalty = min(60, len(active_warnings) * 6)
    result.continuity_score = max(0, 100 - penalty)
    result.continuity_warnings = all_warnings
    return result
