"""Scene-response normalization helpers for xKiro analysis."""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any


def looks_like_scene(item: object) -> bool:
    if not isinstance(item, dict):
        return False
    keys = set(item)
    return bool(
        keys.intersection({"id", "scene_id", "sceneId"})
        and keys.intersection({"action", "summary", "start_state", "end_state"})
    )


def extract_scene_items(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Accept list, mapping, singular and top-level scene response shapes."""
    containers: list[object] = []
    for key in ("scenes", "scene", "results", "items"):
        if key in data:
            containers.append(data[key])
    if looks_like_scene(data):
        containers.append(data)

    items: list[dict[str, Any]] = []
    for container in containers:
        if isinstance(container, list):
            items.extend(item for item in container if isinstance(item, dict))
            continue
        if not isinstance(container, dict):
            continue
        if looks_like_scene(container):
            items.append(container)
            continue
        for key, value in container.items():
            if not isinstance(value, dict):
                continue
            candidate = deepcopy(value)
            if not candidate.get("id") and re.fullmatch(r"SCENE_\d+", str(key), re.I):
                candidate["id"] = str(key).upper()
            if looks_like_scene(candidate):
                items.append(candidate)
    unique: dict[str, dict[str, Any]] = {}
    anonymous = 0
    for item in items:
        identity = str(item.get("id") or item.get("scene_id") or "")
        if not identity:
            anonymous += 1
            identity = f"__anonymous_{anonymous}"
        unique[identity] = item
    return list(unique.values())


def scene_payload_shape(data: dict[str, Any]) -> str:
    if looks_like_scene(data):
        return "object cảnh đơn ở top-level"
    for key in ("scenes", "scene", "results", "items"):
        value = data.get(key)
        if isinstance(value, list):
            return f"{key}=mảng {len(value)} phần tử"
        if isinstance(value, dict):
            return f"{key}=object {len(value)} phần tử"
    return "schema không nhận diện: " + ", ".join(list(data)[:8])


def normalize_scene_result(item: dict[str, Any], world: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(item)
    aliases = {
        "id": ("scene_id", "sceneId"),
        "characters": ("character_ids", "characterIds", "cast"),
        "location_id": ("location", "locationId", "setting_id"),
        "action": ("visual_action", "description", "content"),
        "camera": ("camera_direction", "cameraDirection", "shot"),
        "lighting": ("light", "illumination"),
        "atmosphere": ("mood", "ambience"),
        "start_state": ("start", "initial_state", "startState"),
        "end_state": ("end", "final_state", "endState"),
    }
    for canonical, alternatives in aliases.items():
        if result.get(canonical) is not None:
            continue
        for alternative in alternatives:
            if result.get(alternative) is not None:
                result[canonical] = result[alternative]
                break
    if isinstance(result.get("id"), str):
        result["id"] = result["id"].strip().upper()

    characters = [value for value in world.get("characters", []) if isinstance(value, dict)]
    character_by_name = {
        str(value.get("name", "")).strip().casefold(): value.get("id")
        for value in characters
        if value.get("id") and value.get("name")
    }
    raw_characters = result.get("characters")
    if isinstance(raw_characters, dict):
        raw_characters = list(raw_characters)
    if isinstance(raw_characters, list):
        normalized_characters: list[str] = []
        for value in raw_characters:
            candidate = value.get("id") or value.get("name") if isinstance(value, dict) else value
            if not isinstance(candidate, str):
                continue
            candidate = candidate.strip()
            canonical = character_by_name.get(candidate.casefold(), candidate.upper())
            if canonical and canonical not in normalized_characters:
                normalized_characters.append(str(canonical))
        result["characters"] = normalized_characters

    locations = [value for value in world.get("locations", []) if isinstance(value, dict)]
    location_by_name = {
        str(value.get("name", "")).strip().casefold(): value.get("id")
        for value in locations
        if value.get("id") and value.get("name")
    }
    location = result.get("location_id")
    if isinstance(location, dict):
        location = location.get("id") or location.get("name")
    if isinstance(location, str):
        result["location_id"] = location_by_name.get(
            location.strip().casefold(), location.strip().upper()
        )

    for field in ("camera", "lighting", "atmosphere"):
        value = result.get(field)
        if isinstance(value, dict):
            result[field] = "; ".join(
                str(part).strip() for part in value.values() if str(part).strip()
            )
        elif isinstance(value, list):
            result[field] = "; ".join(str(part).strip() for part in value if str(part).strip())
    for field in ("start_state", "end_state"):
        state = result.get(field)
        if not isinstance(state, dict):
            continue
        state_aliases = {
            "character_positions": ("characterPositions",),
            "character_wardrobe": ("characterWardrobe", "wardrobe"),
            "prop_positions": ("propPositions",),
        }
        for canonical, alternatives in state_aliases.items():
            if state.get(canonical) is not None:
                continue
            for alternative in alternatives:
                if state.get(alternative) is not None:
                    state[canonical] = state[alternative]
                    break
    return result


def valid_scene_result(item: object, required_ids: set[str]) -> bool:
    if not isinstance(item, dict) or item.get("id") not in required_ids:
        return False
    text_fields = ("action", "camera", "lighting", "atmosphere")
    if any(
        not isinstance(item.get(field), str) or not item[field].strip() for field in text_fields
    ):
        return False
    return (
        isinstance(item.get("characters"), list)
        and isinstance(item.get("location_id"), str)
        and isinstance(item.get("start_state"), dict)
        and isinstance(item.get("end_state"), dict)
    )


def chain_scene_states(
    ordered: list[dict[str, Any]], previous: dict[str, Any] | None
) -> dict[str, Any] | None:
    anchor = deepcopy(previous)
    for item in ordered:
        if anchor is not None:
            item["start_state"] = deepcopy(anchor)
        end_state = item.get("end_state")
        if isinstance(end_state, dict):
            anchor = deepcopy(end_state)
    return anchor
