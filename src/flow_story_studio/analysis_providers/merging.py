"""Merge validated xKiro analysis data back into project domain models."""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from ..engines.continuity import check_project
from ..engines.prompt_generator import make_flow_prompt, make_visual_prompt
from ..models import Character, ContinuityState, Dialogue, Location, Project, Prop, StoryBible


def merge_analysis(draft: Project, data: dict[str, Any], model: str) -> Project:
    project = deepcopy(draft)
    story = data.get("story_bible")
    if isinstance(story, dict):
        safe_story = {key: value for key, value in story.items() if key in StoryBible.model_fields}
        project.story_bible = StoryBible.model_validate(
            {**project.story_bible.model_dump(), **safe_story}
        )

    def validated_list(key: str, model_type: type[Any], fallback: list[Any]) -> list[Any]:
        raw = data.get(key)
        if not isinstance(raw, list):
            return fallback
        values: list[Any] = []
        for item in raw:
            try:
                if not isinstance(item, dict):
                    continue
                safe_item = {
                    field_name: field_value
                    for field_name, field_value in item.items()
                    if field_name in model_type.model_fields
                }
                value = model_type.model_validate(safe_item)
                if model_type is Character:
                    name = str(value.name).strip()
                    blocked_names = {
                        "ft",
                        "giọng",
                        "voice",
                        "voiceover",
                        "narrator voice",
                        "camera",
                        "bối cảnh",
                        "nhân vật",
                        "scene",
                        "cảnh",
                    }
                    if (
                        len(name) < 3
                        or name.casefold() in blocked_names
                        or re.search(r"[#*_`{}\[\]]", name)
                    ):
                        continue
                values.append(value)
            except (ValueError, TypeError):
                continue
        return values or fallback

    project.characters = validated_list("characters", Character, project.characters)
    project.locations = validated_list("locations", Location, project.locations)
    project.props = validated_list("props", Prop, project.props)
    project.master_prompt = str(data.get("master_prompt") or project.master_prompt)
    project.visual_style = str(data.get("visual_style") or project.visual_style)

    scene_data = {
        item.get("id"): item
        for item in data.get("scenes", [])
        if isinstance(item, dict) and item.get("id")
    }
    character_ids = {item.id for item in project.characters}
    location_ids = {item.id for item in project.locations}
    for index, scene in enumerate(project.scenes):
        item = scene_data.get(scene.id)
        if not item:
            continue
        scene.ai_locked = True
        scene.ai_lock_reason = f"{model} đã duyệt scene và continuity"
        for field in (
            "summary",
            "action",
            "camera",
            "lighting",
            "atmosphere",
            "voiceover",
        ):
            if isinstance(item.get(field), str) and item[field].strip():
                setattr(scene, field, item[field].strip())
        chars = item.get("characters")
        if isinstance(chars, list):
            safe_chars = [value for value in chars if value in character_ids]
            if safe_chars:
                scene.characters = safe_chars
        if item.get("location_id") in location_ids:
            scene.location_id = item["location_id"]
        try:
            if isinstance(item.get("dialogues"), list):
                scene.dialogues = [
                    Dialogue.model_validate(
                        {key: field for key, field in value.items() if key in Dialogue.model_fields}
                    )
                    for value in item["dialogues"]
                    if isinstance(value, dict)
                ]
            if isinstance(item.get("start_state"), dict):
                scene.start_state = ContinuityState.model_validate(
                    {
                        key: value
                        for key, value in item["start_state"].items()
                        if key in ContinuityState.model_fields
                    }
                )
            if isinstance(item.get("end_state"), dict):
                scene.end_state = ContinuityState.model_validate(
                    {
                        key: value
                        for key, value in item["end_state"].items()
                        if key in ContinuityState.model_fields
                    }
                )
        except (ValueError, TypeError):
            pass
        location = next(value for value in project.locations if value.id == scene.location_id)
        visible = [value for value in project.characters if value.id in scene.characters]
        scene.visual_prompt = make_visual_prompt(
            action=scene.action,
            characters=visible,
            location=location,
            camera=scene.camera,
            lighting=scene.lighting,
            atmosphere=scene.atmosphere,
            style=project.visual_style,
            start_state=scene.start_state,
            end_state=scene.end_state,
        )
        scene.flow_prompt = make_flow_prompt(
            scene,
            characters=visible,
            location=location,
            visual_style=project.visual_style,
            previous_scene_id=project.scenes[index - 1].id if index else None,
        )
    project.timeline.append(f"Story analysis provider: xKiro · {model}")
    project.settings.character_lock = True
    project.settings.location_lock = True
    project.settings.auto_continuity = True
    for scene in project.scenes:
        scene.ai_locked = True
        if not scene.ai_lock_reason or scene.ai_lock_reason.startswith("AI đã"):
            scene.ai_lock_reason = f"{model} · Character + Location + Continuity đã khóa"
    return check_project(project, auto_fix=True)
