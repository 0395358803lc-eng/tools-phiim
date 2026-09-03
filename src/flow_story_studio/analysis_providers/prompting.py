"""Pure prompt/world helpers for the xKiro analysis pipeline."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any

from ..models import Project


def split_source(text: str, max_chars: int) -> list[str]:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs or [text.strip()]:
        pieces = [paragraph]
        if len(paragraph) > max_chars:
            pieces = []
            remaining = paragraph
            while len(remaining) > max_chars:
                window = remaining[:max_chars]
                lower_bound = max_chars * 3 // 5
                boundaries = [
                    window.rfind(marker, lower_bound)
                    for marker in (". ", "! ", "? ", "; ", ", ", " ")
                ]
                boundary = max(boundaries)
                cut = boundary + 1 if boundary >= lower_bound else max_chars
                pieces.append(remaining[:cut].strip())
                remaining = remaining[cut:].lstrip()
            if remaining:
                pieces.append(remaining)
        for piece in pieces:
            candidate = f"{current}\n\n{piece}".strip() if current else piece
            if current and len(candidate) > max_chars:
                chunks.append(current)
                current = piece
            else:
                current = candidate
    if current:
        chunks.append(current)
    return chunks or [text]


def draft_world(draft: Project) -> dict[str, Any]:
    return {
        "story_bible": draft.story_bible.model_dump(),
        "characters": [item.model_dump() for item in draft.characters],
        "locations": [item.model_dump() for item in draft.locations],
        "props": [item.model_dump() for item in draft.props],
        "master_prompt": draft.master_prompt,
        "visual_style": draft.visual_style,
    }


def merge_world(current: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(current)
    story = update.get("story_bible")
    if isinstance(story, dict):
        merged["story_bible"] = {**merged.get("story_bible", {}), **story}
    for key in ("characters", "locations", "props"):
        incoming = update.get(key)
        if not isinstance(incoming, list):
            continue
        ordered = [deepcopy(item) for item in merged.get(key, []) if isinstance(item, dict)]
        positions = {item.get("id"): index for index, item in enumerate(ordered) if item.get("id")}
        for item in incoming:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            existing = positions.get(item["id"])
            if existing is None:
                positions[item["id"]] = len(ordered)
                ordered.append(deepcopy(item))
            else:
                ordered[existing] = {**ordered[existing], **item}
        merged[key] = ordered
    for key in ("master_prompt", "visual_style"):
        if isinstance(update.get(key), str) and update[key].strip():
            merged[key] = update[key].strip()
    return merged


def world_prompt(current: dict[str, Any], source_chunk: str, index: int, total: int) -> str:
    return f"""Update the canonical story world from screenplay part {index}/{total}.

CURRENT CANONICAL WORLD:
{json.dumps(current, ensure_ascii=False)}

SCREENPLAY PART {index}/{total}:
{source_chunk}

Return the COMPLETE updated canonical object with keys story_bible, characters, locations, props,
master_prompt and visual_style. Never delete a valid identity from the current world. Preserve every
existing ID and assign only new sequential IDs when a genuinely new on-screen person, location or
important prop appears. Never classify headings, role labels, voice labels, camera notes or metadata
as characters. Give every character immutable face, body, hair, eyes, skin, wardrobe, accessories,
shoes and identifying features. Give every location immutable architecture, layout, materials,
objects, palette and spatial anchors. Keep chronology and source facts exact. Output JSON only."""


def scene_prompt(
    world: dict[str, Any],
    scenes: list[Any],
    previous_end_state: dict[str, Any] | None,
    *,
    schema_version: int,
) -> str:
    source = " ".join(scene.source_text for scene in scenes).casefold()
    characters = [item for item in world.get("characters", []) if isinstance(item, dict)]
    locations = [item for item in world.get("locations", []) if isinstance(item, dict)]
    props = [item for item in world.get("props", []) if isinstance(item, dict)]
    current_character_ids = {value for scene in scenes for value in scene.characters}
    current_location_ids = {scene.location_id for scene in scenes}
    relevant_characters = [
        item
        for item in characters
        if item.get("id") in current_character_ids or str(item.get("name", "")).casefold() in source
    ]
    relevant_locations = [
        item
        for item in locations
        if item.get("id") in current_location_ids or str(item.get("name", "")).casefold() in source
    ]
    relevant_props = [item for item in props if str(item.get("name", "")).casefold() in source]
    compact_world = {
        "story_bible": world.get("story_bible"),
        "visual_style": world.get("visual_style"),
        "character_index": [
            {"id": item.get("id"), "name": item.get("name")} for item in characters
        ],
        "location_index": [{"id": item.get("id"), "name": item.get("name")} for item in locations],
        "characters_in_scope": relevant_characters,
        "locations_in_scope": relevant_locations,
        "props_in_scope": relevant_props,
    }
    scene_payload = [
        {
            "id": scene.id,
            "source_text": scene.source_text,
            "current_characters": scene.characters,
            "current_location_id": scene.location_id,
        }
        for scene in scenes
    ]
    return f"""Enrich this ordered scene batch using the locked canonical world.

SCENE RESPONSE PROTOCOL VERSION: {schema_version}

LOCKED WORLD (compact, IDs are immutable):
{json.dumps(compact_world, ensure_ascii=False)}

EXACT END STATE OF THE PREVIOUS APPROVED SCENE:
{json.dumps(previous_end_state, ensure_ascii=False)}

SCENES TO RETURN (return every supplied ID exactly once and no other IDs):
{json.dumps(scene_payload, ensure_ascii=False)}

Return one JSON object with a scenes array. Every scene must contain id, summary, characters,
location_id, action, camera, lighting, atmosphere, voiceover, dialogues, start_state and end_state.
Each dialogue contains character_id, text and emotion. Each state contains character_positions,
character_wardrobe, prop_positions, time, weather, camera and notes. The first start_state must
equal the supplied previous end state; then chain every next start_state from the preceding
end_state.
Identity, wardrobe, props, architecture, screen direction, palette, weather and lighting may change
only when a visible action in source_text causes the change. Describe filmable action only. Preserve
the source meaning and chronology exactly. Output JSON only."""


def analysis_prompt(request: Any, draft: Project) -> str:
    skeleton = {
        "story_bible": draft.story_bible.model_dump(),
        "characters": [item.model_dump() for item in draft.characters],
        "locations": [item.model_dump() for item in draft.locations],
        "props": [item.model_dump() for item in draft.props],
        "scenes": [
            {
                "id": item.id,
                "source_text": item.source_text,
                "characters": item.characters,
                "location_id": item.location_id,
            }
            for item in draft.scenes
        ],
    }
    return f"""Analyze this entire Vietnamese story for a continuity-first video pipeline.

ORIGINAL TEXT:
{request.original_text}

DRAFT IDS AND SCENE BOUNDARIES:
{json.dumps(skeleton, ensure_ascii=False)}

Return one JSON object with keys story_bible, characters, locations, props, master_prompt,
visual_style and scenes. Keep the supplied IDs. You may add missed characters/locations/props with
the next sequential ID. Do not create scenes from headings, metadata, character profiles, voice
labels or camera notes. A character must be a real on-screen person; never return "ft", "Giọng",
"Voice", a section title or a technical role as a character. Fill a specific immutable visual
identity for each character and fixed architecture/spatial anchors for each location. For every
scene return: id, summary, characters (IDs), location_id, action,
camera, lighting, atmosphere, voiceover, dialogues, start_state and end_state. Each dialogue has
character_id, text and emotion. Each state has character_positions, character_wardrobe,
prop_positions, time, weather, camera and notes. Keep source meaning exact and make each start_state
follow the previous end_state. Output JSON only."""
