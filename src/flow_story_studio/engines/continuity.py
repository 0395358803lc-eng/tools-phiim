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


def is_direct_frame_anchor(previous: Scene | None, current: Scene) -> bool:
    """Return True only when the next scene can reuse the exact accepted final frame."""
    if previous is None or not is_direct_continuation(previous, current):
        return False
    before = previous.end_state
    after = current.start_state
    before_chars = set(previous.characters)
    after_chars = set(current.characters)
    before_props = set(before.prop_positions)
    after_props = set(after.prop_positions)
    return before_chars == after_chars and before_props == after_props


def sanitize_visual_state_scope(project: Project) -> Project:
    """Remove stale nested visual entities before dependency classification."""
    result = deepcopy(project)
    valid_props = {item.id for item in result.props}
    for scene in result.scenes:
        visible = set(scene.characters)
        for state in (scene.start_state, scene.end_state):
            state.character_positions = {
                key: value
                for key, value in state.character_positions.items()
                if key in visible
            }
            state.character_wardrobe = {
                key: value
                for key, value in state.character_wardrobe.items()
                if key in visible
            }
            state.prop_positions = {
                key: value
                for key, value in state.prop_positions.items()
                if key in valid_props
            }
    return result


def enforce_frame_anchor_policy(project: Project) -> Project:
    """Copy complete entry state only when the visible inventory can be anchored."""
    result = sanitize_visual_state_scope(project)
    previous: Scene | None = None
    for scene in result.scenes:
        if is_direct_frame_anchor(previous, scene):
            scene.start_state = deepcopy(previous.end_state)
        previous = scene
    return result


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
        exact_frame = current.semantic_truth.frame_anchor == "previous_final_frame"
        if exact_frame:
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
        else:
            # A canonical re-composition may legitimately change what is visible or
            # where it sits in the frame. Validate physical continuity from the
            # semantic ledger instead of comparing render-facing descriptions.
            previous_props = previous.semantic_truth.exit_props
            current_props = current.semantic_truth.entry_props
            for prop_id in sorted(set(previous_props) & set(current_props)):
                before = previous_props[prop_id]
                after = current_props[prop_id]
                before_signature = (
                    before.present,
                    before.part,
                    before.owner_id,
                    before.location_id,
                    before.container,
                    before.condition,
                    before.piece_count,
                    before.scope,
                )
                after_signature = (
                    after.present,
                    after.part,
                    after.owner_id,
                    after.location_id,
                    after.container,
                    after.condition,
                    after.piece_count,
                    after.scope,
                )
                if before_signature != after_signature:
                    warnings.append(
                        f"{prop_id} thay đổi trạng thái vật lý tại biên cảnh mà chưa có "
                        "diễn biến chuyển tiếp"
                    )
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
        if auto_fix and is_direct_frame_anchor(previous, scene):
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
