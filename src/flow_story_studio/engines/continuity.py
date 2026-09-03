"""Cross-scene continuity validation and safe auto-repair."""

from __future__ import annotations

from copy import deepcopy

from ..models import Project, Scene


def scene_warnings(previous: Scene | None, current: Scene, project: Project) -> list[str]:
    warnings: list[str] = []
    character_ids = {item.id for item in project.characters}
    location_ids = {item.id for item in project.locations}
    unknown = set(current.characters) - character_ids
    if unknown:
        warnings.append(f"Nhân vật chưa có trong Bible: {', '.join(sorted(unknown))}")
    if current.location_id not in location_ids:
        warnings.append(f"Location {current.location_id} chưa có trong Bible")
    if previous:
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
    previous: Scene | None = None
    for index, scene in enumerate(result.scenes):
        retained = [
            warning
            for warning in scene.warnings
            if warning.startswith("Thay đổi ở") or warning.startswith("Render failed:")
        ]
        scene.order = index + 1
        scene.id = f"SCENE_{index + 1:03d}"
        if previous and auto_fix:
            scene.start_state = deepcopy(previous.end_state)
        scene.warnings = retained + scene_warnings(previous, scene, result)
        all_warnings.extend(f"{scene.id}: {warning}" for warning in scene.warnings)
        previous = scene
    penalty = min(60, len(all_warnings) * 6)
    result.continuity_score = max(0, 100 - penalty)
    result.continuity_warnings = all_warnings
    return result
