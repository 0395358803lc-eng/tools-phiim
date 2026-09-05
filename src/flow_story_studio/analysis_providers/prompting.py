"""Pure prompt/world helpers for the xKiro analysis pipeline."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any

from ..engines.analyzer import GENERIC_REFERENCE_NAMES
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


def _semantic_key(value: object) -> str:
    import unicodedata

    normalized = unicodedata.normalize("NFKD", str(value or "").casefold().replace("đ", "d"))
    ascii_text = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return " ".join(re.findall(r"[a-z0-9]+", ascii_text))


def _same_semantic_identity(left: object, right: object) -> bool:
    from difflib import SequenceMatcher

    a = _semantic_key(left)
    b = _semantic_key(right)
    if not a or not b:
        return True
    if a == b or a in b or b in a:
        return True
    overlap = len(set(a.split()) & set(b.split())) / max(1, len(set(a.split()) | set(b.split())))
    return max(overlap, SequenceMatcher(None, a, b).ratio()) >= 0.72


def _next_world_id(prefix: str, items: list[dict[str, Any]]) -> str:
    used = {str(item.get("id") or "") for item in items}
    index = 1
    while f"{prefix}_{index:03d}" in used:
        index += 1
    return f"{prefix}_{index:03d}"


def merge_world(current: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(current)
    story = update.get("story_bible")
    if isinstance(story, dict):
        merged["story_bible"] = {**merged.get("story_bible", {}), **story}
    prefixes = {"characters": "CHAR", "locations": "LOC", "props": "PROP"}
    for key in ("characters", "locations", "props"):
        incoming = update.get(key)
        if not isinstance(incoming, list):
            continue
        ordered = [deepcopy(item) for item in merged.get(key, []) if isinstance(item, dict)]
        positions = {item.get("id"): index for index, item in enumerate(ordered) if item.get("id")}
        for item in incoming:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            candidate = deepcopy(item)
            candidate_name = str(candidate.get("name") or "").strip()
            if key == "characters" and candidate_name.casefold() in GENERIC_REFERENCE_NAMES:
                continue

            semantic_existing = next(
                (
                    index
                    for index, previous in enumerate(ordered)
                    if _same_semantic_identity(previous.get("name"), candidate_name)
                ),
                None,
            )
            if semantic_existing is not None:
                previous = ordered[semantic_existing]
                candidate["id"] = previous["id"]
                ordered[semantic_existing] = {**previous, **candidate}
                positions[previous["id"]] = semantic_existing
                continue

            existing = positions.get(candidate["id"])
            if existing is None:
                positions[candidate["id"]] = len(ordered)
                ordered.append(candidate)
                continue
            candidate["id"] = _next_world_id(prefixes[key], ordered)
            positions[candidate["id"]] = len(ordered)
            ordered.append(candidate)
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
important prop appears. Never create a new character for a pronoun, honorific, generic reference or
alias (for example he/she/the man/the woman or equivalent forms in the source language); resolve it
to the already-canonical person when the source supports that identity. Never create two semantic
entities for the same person, place or object under different IDs. Every explicit screenplay scene
heading location must resolve to exactly one canonical location. Every recurring, plot-relevant or
continuity-critical physical object explicitly present in the source must be represented exactly
once
in props even when the screenplay has no PROP section. Never classify headings, voice labels, camera
notes or production metadata as characters. Give every character immutable face, body, hair, eyes,
skin, wardrobe, accessories, shoes and identifying features. Give every location immutable
architecture, layout, materials, objects, palette and spatial anchors. Keep chronology and source
facts exact. Output JSON only."""


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
    relevant_locations = [item for item in locations if item.get("id") in current_location_ids]
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

PREVIOUS APPROVED END STATE (continuity reference only):
{json.dumps(previous_end_state, ensure_ascii=False)}

SCENES TO RETURN (return every supplied ID exactly once and no other IDs):
{json.dumps(scene_payload, ensure_ascii=False)}

Return one JSON object with a scenes array. Every scene must contain id, summary, characters,
location_id, action, camera, lighting, atmosphere, voiceover, dialogues, start_state and end_state.
Each dialogue contains character_id, text and emotion. Each state contains character_positions,
character_wardrobe, prop_positions, time, weather, camera and notes.

CONTINUITY RULE: reuse the previous approved end state only when the current source_text is a direct
spatial and temporal continuation. A [SCENE CONTEXT] marker, location change, flashback,
time jump or
explicit cut starts a new truthful state; never copy the prior composition merely to preserve
continuity. Within a direct continuation, chain start_state from the preceding end_state.

ANTI-DUPLICATION RULE: every supplied scene ID must express its own source beat. Do not copy or
paraphrase another scene's action, camera setup, staging or summary. Treat current_location_id
as the intended on-screen location; a place merely mentioned in dialogue must not replace it.
Preserve
recurring identity and world attributes, but vary composition, blocking and camera motivation when
the source beat changes.
Identity, wardrobe, props, architecture, screen direction, palette, weather and lighting may change
only when source_text or an explicit scene context supports the change. Describe filmable
action only.
Preserve the source meaning and chronology exactly. Output JSON only."""


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
labels or camera notes. A character must be a real on-screen person; never return "ft", "GiÃ¡Â»Âng",
"Voice", a section title or a technical role as a character. Fill a specific immutable visual
identity for each character and fixed architecture/spatial anchors for each location. For every
scene return: id, summary, characters (IDs), location_id, action,
camera, lighting, atmosphere, voiceover, dialogues, start_state and end_state. Each dialogue has
character_id, text and emotion. Each state has character_positions, character_wardrobe,
prop_positions, time, weather, camera and notes. Keep source meaning exact and make each start_state
follow the previous end_state. Output JSON only."""
