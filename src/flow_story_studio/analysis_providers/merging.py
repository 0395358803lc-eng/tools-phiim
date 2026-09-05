"""Merge validated xKiro analysis data back into project domain models."""

from __future__ import annotations

import re
import unicodedata
from copy import deepcopy
from difflib import SequenceMatcher
from typing import Any

from ..engines.analyzer import GENERIC_REFERENCE_NAMES
from ..engines.prompt_generator import make_flow_prompt, make_visual_prompt
from ..models import Character, ContinuityState, Dialogue, Location, Project, Prop, StoryBible
from .finalization import finalize_project


def _semantic_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value).casefold().replace("đ", "d"))
    ascii_text = "".join(char for char in normalized if not unicodedata.combining(char))
    return " ".join(re.findall(r"[a-z0-9]+", ascii_text))


def _semantic_score(left: str, right: str) -> float:
    a = _semantic_key(left)
    b = _semantic_key(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.94
    a_tokens = set(a.split())
    b_tokens = set(b.split())
    overlap = len(a_tokens & b_tokens) / max(1, len(a_tokens | b_tokens))
    sequence = SequenceMatcher(None, a, b).ratio()
    return max(overlap, sequence)


def _next_entity_id(prefix: str, used_ids: set[str]) -> str:
    number = 1
    while f"{prefix}_{number:03d}" in used_ids:
        number += 1
    value = f"{prefix}_{number:03d}"
    used_ids.add(value)
    return value


def _merge_entity(existing: Any, incoming: Any, canonical_id: str) -> Any:
    merged = existing.model_dump() if existing is not None else {}
    merged.update(incoming.model_dump())
    merged["id"] = canonical_id
    return type(incoming).model_validate(merged)


def _reconcile_entities(
    draft_items: list[Any],
    ai_items: list[Any],
    *,
    prefix: str,
    keep_unmatched_draft: bool,
    threshold: float = 0.72,
) -> tuple[list[Any], dict[str, str]]:
    if not ai_items:
        return draft_items, {item.id: item.id for item in draft_items}

    used_ids = {item.id for item in draft_items}
    remaining_draft = {item.id: item for item in draft_items}
    canonical: list[Any] = []
    ai_to_canonical: dict[str, str] = {}
    emitted_ids: set[str] = set()

    for incoming in ai_items:
        best = None
        best_score = 0.0
        for candidate in remaining_draft.values():
            score = _semantic_score(incoming.name, candidate.name)
            if score > best_score:
                best = candidate
                best_score = score
        if best is not None and best_score >= threshold:
            canonical_id = best.id
            remaining_draft.pop(best.id, None)
            merged = _merge_entity(best, incoming, canonical_id)
        else:
            emitted_match = None
            emitted_score = 0.0
            for candidate in canonical:
                score = _semantic_score(incoming.name, candidate.name)
                if score > emitted_score:
                    emitted_match = candidate
                    emitted_score = score
            if emitted_match is not None and emitted_score >= threshold:
                canonical_id = emitted_match.id
                ai_to_canonical[incoming.id] = canonical_id
                index = next(i for i, item in enumerate(canonical) if item.id == canonical_id)
                canonical[index] = _merge_entity(emitted_match, incoming, canonical_id)
                continue
            canonical_id = incoming.id
            if canonical_id in used_ids or canonical_id in emitted_ids:
                canonical_id = _next_entity_id(prefix, used_ids | emitted_ids)
                used_ids.add(canonical_id)
            else:
                used_ids.add(canonical_id)
            merged = incoming.model_copy(update={"id": canonical_id})
        # An unmatched AI entity that reuses an existing draft ID is ambiguous.
        # Keep that raw ID bound to its original draft semantic identity; the
        # incoming entity may receive a fresh canonical ID, but references using
        # the colliding raw ID must never hijack the existing identity.
        if incoming.id in {item.id for item in draft_items} and best_score < threshold:
            ai_to_canonical[incoming.id] = incoming.id
        else:
            ai_to_canonical[incoming.id] = canonical_id
        if canonical_id not in emitted_ids:
            canonical.append(merged)
            emitted_ids.add(canonical_id)

    if keep_unmatched_draft:
        for item in draft_items:
            if item.id not in emitted_ids:
                canonical.append(item)
                emitted_ids.add(item.id)

    return canonical, ai_to_canonical


def _resolve_reference(
    value: object,
    mapping: dict[str, str],
    entities: list[Any],
    *,
    threshold: float = 0.82,
) -> str | None:
    raw = str(value).strip()
    if not raw:
        return None
    entity_ids = {item.id for item in entities}
    mapped = mapping.get(raw, raw)
    if mapped in entity_ids:
        return mapped

    best_id: str | None = None
    best_score = 0.0
    for entity in entities:
        score = _semantic_score(raw, entity.name)
        if score > best_score:
            best_id = entity.id
            best_score = score
    if best_id is not None and best_score >= threshold:
        return best_id
    return None


def _canonicalize_reference_dict(
    values: dict[str, str],
    mapping: dict[str, str],
    entities: list[Any],
) -> dict[str, str]:
    canonical: dict[str, str] = {}
    for key, value in values.items():
        resolved = _resolve_reference(key, mapping, entities)
        if resolved is not None:
            canonical[resolved] = value
    return canonical


_OFFSCREEN_MARKERS = {
    "v o",
    "o s",
    "offscreen",
    "off screen",
    "voice over",
    "voiceover",
    "over the phone",
    "through the phone",
    "on the phone",
    "phone call",
    "qua dien thoai",
    "tren dien thoai",
    "dau day ben kia",
    "giong qua dien thoai",
    "giong noi",
    "giong",
    "voice",
    "loa ngoai",
    "speakerphone",
}

_PRESENCE_STOPWORDS = {
    "cua",
    "the",
    "and",
    "with",
    "from",
    "mau",
    "color",
    "colour",
    "main",
    "chinh",
    "giay",
    "paper",
    "mot",
    "one",
    "old",
    "cu",
    "small",
    "large",
}


def _source_tokens(value: str) -> set[str]:
    return set(_semantic_key(value).split())


def _entity_mentioned(source_text: str, entity_name: str) -> bool:
    raw_source = str(source_text).casefold()
    raw_name = str(entity_name).casefold().strip()
    source_key = _semantic_key(source_text)
    name_key = _semantic_key(entity_name)
    if not source_key or not name_key:
        return False

    raw_name_tokens = re.findall(r"[\wÀ-ỹ]+", raw_name, re.UNICODE)
    name_tokens = [
        token for token in name_key.split() if token not in _PRESENCE_STOPWORDS and len(token) >= 2
    ]
    if not name_tokens:
        return False

    # For very short, single-token names (AN, LÊ, MY...), accent folding is unsafe:
    # Vietnamese words such as "ăn" can collapse to the same ASCII token. Require an
    # exact Unicode word-boundary match in the original text instead.
    if len(raw_name_tokens) == 1 and len(raw_name_tokens[0]) <= 3:
        pattern = rf"(?<!\w){re.escape(raw_name_tokens[0])}(?!\w)"
        return re.search(pattern, raw_source, re.UNICODE) is not None

    if name_key in source_key:
        return True
    source_tokens = set(source_key.split())
    overlap = sum(token in source_tokens for token in name_tokens)
    required = 1 if len(name_tokens) == 1 else 2
    return overlap >= required


def _character_is_offscreen_only(source_text: str, character_name: str) -> bool:
    source_key = _semantic_key(source_text)
    name_key = _semantic_key(character_name)
    if not source_key or not name_key or name_key not in source_key:
        return False
    tokens = source_key.split()
    name_tokens = name_key.split()
    marker_tokens = [marker.split() for marker in _OFFSCREEN_MARKERS]
    starts: list[int] = []
    width = len(name_tokens)
    for index in range(max(0, len(tokens) - width + 1)):
        if tokens[index : index + width] == name_tokens:
            starts.append(index)
    if not starts:
        return False
    remote_occurrences = 0
    for start in starts:
        left = max(0, start - 5)
        right = min(len(tokens), start + width + 5)
        window = tokens[left:right]
        window_text = " ".join(window)
        if any(" ".join(marker) in window_text for marker in marker_tokens):
            remote_occurrences += 1
    return remote_occurrences == len(starts)


def _character_is_explicitly_absent(source_text: str, character_name: str) -> bool:
    source_key = _semantic_key(source_text)
    name_key = _semantic_key(character_name)
    if not source_key or not name_key or name_key not in source_key:
        return False
    patterns = (
        rf"{re.escape(name_key)}\s+(?:is\s+)?not\s+(?:present|here|there|in\s+the\s+room|onscreen|on\s+screen)",
        rf"{re.escape(name_key)}\s+(?:does\s+not|doesnt)\s+(?:appear|enter)",
        rf"{re.escape(name_key)}\s+(?:khong|không)\s+(?:co\s+mat|có\s+mặt|o\s+day|ở\s+đây|o\s+trong|ở\s+trong)",
        rf"(?:without|khong\s+co|không\s+có)\s+{re.escape(name_key)}",
    )
    return any(re.search(pattern, source_key) for pattern in patterns)


def _visible_character_ids(
    source_text: str,
    candidate_ids: list[str],
    characters: list[Character],
) -> list[str]:
    by_id = {item.id: item for item in characters}
    visible: list[str] = []
    for character_id in candidate_ids:
        character = by_id.get(character_id)
        if character is None:
            continue
        if not _entity_mentioned(source_text, character.name):
            continue
        if _character_is_explicitly_absent(source_text, character.name):
            continue
        if _character_is_offscreen_only(source_text, character.name):
            continue
        visible.append(character_id)
    return list(dict.fromkeys(visible))


_MULTI_PERSON_CAMERA_MARKERS = {
    "two shot",
    "two-shot",
    "two people",
    "both characters",
    "both people",
    "hai nguoi",
    "ca hai",
    "hai nhan vat",
    "doi dien nhau",
    "across from each other",
}


def _camera_conflicts_with_visual_cast(
    camera: str,
    visible_character_ids: set[str],
    characters: list[Character],
) -> bool:
    camera_key = _semantic_key(camera)
    if not camera_key:
        return False
    if len(visible_character_ids) < 2:
        normalized_markers = {_semantic_key(value) for value in _MULTI_PERSON_CAMERA_MARKERS}
        if any(marker and marker in camera_key for marker in normalized_markers):
            return True
    for character in characters:
        if character.id in visible_character_ids:
            continue
        if _entity_mentioned(camera, character.name):
            return True
    return False


def _sanitize_camera_for_visual_cast(
    camera: str,
    visible_character_ids: set[str],
    characters: list[Character],
) -> tuple[str, bool]:
    if not _camera_conflicts_with_visual_cast(camera, visible_character_ids, characters):
        return camera, False
    if not visible_character_ids:
        return (
            "Wide environmental establishing shot with no person in frame; "
            "camera composition follows the source location and action only",
            True,
        )
    if len(visible_character_ids) == 1:
        return (
            "Medium single-subject cinematic shot; frame only the one visible character, "
            "with no second person, reflection, silhouette, or implied partner in frame",
            True,
        )
    return (
        "Balanced multi-subject cinematic composition containing only the visible characters "
        "declared for this scene",
        True,
    )


def _present_prop_ids(source_text: str, props: list[Prop]) -> set[str]:
    return {item.id for item in props if _entity_mentioned(source_text, item.name)}


def _sanitize_state_for_scene(
    state: ContinuityState,
    *,
    visible_character_ids: set[str],
    present_prop_ids: set[str],
) -> ContinuityState:
    data = state.model_dump()
    for field in ("character_positions", "character_wardrobe"):
        values = data.get(field) or {}
        data[field] = {key: value for key, value in values.items() if key in visible_character_ids}
    prop_positions = data.get("prop_positions") or {}
    data["prop_positions"] = {
        key: value for key, value in prop_positions.items() if key in present_prop_ids
    }
    return ContinuityState.model_validate(data)


def _assert_production_invariants(project: Project) -> None:
    for entity_group in (project.characters, project.locations, project.props):
        seen: dict[str, str] = {}
        for entity in entity_group:
            key = _semantic_key(entity.name)
            if key and key in seen and seen[key] != entity.id:
                raise ValueError(
                    f"Duplicate semantic entity before production: {entity.name} "
                    f"({seen[key]} and {entity.id})"
                )
            if key:
                seen[key] = entity.id

    character_ids = {item.id for item in project.characters}
    prop_ids = {item.id for item in project.props}
    for scene in project.scenes:
        visible = set(scene.characters)
        if not visible <= character_ids:
            raise ValueError(f"Invalid visual character reference in {scene.id}")
        if _camera_conflicts_with_visual_cast(scene.camera, visible, project.characters):
            raise ValueError(f"Camera conflicts with visual cast in {scene.id}")
        for state in (scene.start_state, scene.end_state):
            state_characters = set(state.character_positions) | set(state.character_wardrobe)
            if not state_characters <= visible:
                raise ValueError(f"Stale/off-screen character state in {scene.id}")
            if not set(state.prop_positions) <= prop_ids:
                raise ValueError(f"Invalid prop state in {scene.id}")

    prompts = [scene.flow_prompt.strip() for scene in project.scenes if scene.flow_prompt.strip()]
    if len(prompts) != len(set(prompts)):
        raise ValueError("Duplicate Flow prompts detected before production")


def merge_analysis(draft: Project, data: dict[str, Any], model: str) -> Project:
    project = deepcopy(draft)
    draft_scene_locations = {scene.id: scene.location_id for scene in draft.scenes}
    draft_location_names = {item.id: item.name for item in draft.locations}
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
                    blocked_names |= GENERIC_REFERENCE_NAMES
                    if (
                        len(name) < 2
                        or name.casefold() in blocked_names
                        or re.search(r"[#*_`{}\[\]]", name)
                    ):
                        continue
                values.append(value)
            except (ValueError, TypeError):
                continue
        return values or fallback

    ai_characters = validated_list("characters", Character, [])
    ai_locations = validated_list("locations", Location, [])
    ai_props = validated_list("props", Prop, [])

    project.characters, character_id_map = _reconcile_entities(
        project.characters,
        ai_characters,
        prefix="CHAR",
        keep_unmatched_draft=True,
    )
    project.locations, location_id_map = _reconcile_entities(
        project.locations,
        ai_locations,
        prefix="LOC",
        keep_unmatched_draft=True,
    )
    project.props, prop_id_map = _reconcile_entities(
        project.props,
        ai_props,
        prefix="PROP",
        keep_unmatched_draft=True,
    )
    project.master_prompt = str(data.get("master_prompt") or project.master_prompt)
    project.visual_style = str(data.get("visual_style") or project.visual_style)

    scene_data = {
        item.get("id"): item
        for item in data.get("scenes", [])
        if isinstance(item, dict) and item.get("id")
    }
    location_ids = {item.id for item in project.locations}
    for index, scene in enumerate(project.scenes):
        item = scene_data.get(scene.id)
        if not item:
            continue
        scene.ai_locked = True
        if item.get("_source_truth_fallback"):
            scene.ai_lock_reason = "Source-truth fallback: xKiro response incomplete"
            warning = "xKiro response incomplete; retained deterministic source-truth scene data"
            if warning not in scene.warnings:
                scene.warnings.append(warning)
        else:
            scene.ai_lock_reason = f"{model} reviewed scene and continuity"
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
            safe_chars = [
                resolved
                for value in chars
                if (resolved := _resolve_reference(value, character_id_map, project.characters))
                is not None
            ]
            if safe_chars:
                scene.characters = list(dict.fromkeys(safe_chars))
        scene.characters = _visible_character_ids(
            scene.source_text,
            scene.characters,
            project.characters,
        )
        scene.camera, camera_sanitized = _sanitize_camera_for_visual_cast(
            scene.camera,
            set(scene.characters),
            project.characters,
        )
        if camera_sanitized:
            warning = (
                "xKiro camera conflicted with source-grounded visual cast; "
                "camera sanitized to match visible cast"
            )
            if warning not in scene.warnings:
                scene.warnings.append(warning)
        incoming_location_id = item.get("location_id")
        canonical_location_id = _resolve_reference(
            incoming_location_id,
            location_id_map,
            project.locations,
        )
        if canonical_location_id in location_ids:
            draft_location_id = draft_scene_locations.get(scene.id, scene.location_id)
            draft_location_name = draft_location_names.get(draft_location_id, "")
            source_key = _semantic_key(scene.source_text)
            has_explicit_context = scene.source_text.lstrip().startswith("[SCENE CONTEXT]")
            candidate = next(
                (value for value in project.locations if value.id == canonical_location_id),
                None,
            )
            candidate_key = _semantic_key(candidate.name) if candidate is not None else ""
            draft_key = _semantic_key(draft_location_name)
            candidate_supported = bool(candidate_key and candidate_key in source_key)
            draft_is_generic = draft_key in {"", "boi canh chinh", "main setting"}
            if (
                canonical_location_id == draft_location_id
                or candidate_supported
                or (draft_is_generic and not has_explicit_context)
            ):
                scene.location_id = canonical_location_id
            else:
                scene.location_id = draft_location_id
        try:
            if isinstance(item.get("dialogues"), list):
                dialogues = []
                for value in item["dialogues"]:
                    if not isinstance(value, dict):
                        continue
                    safe_dialogue = {
                        key: field for key, field in value.items() if key in Dialogue.model_fields
                    }
                    if "character_id" in safe_dialogue:
                        resolved_character = _resolve_reference(
                            safe_dialogue["character_id"],
                            character_id_map,
                            project.characters,
                        )
                        if resolved_character is None:
                            continue
                        safe_dialogue["character_id"] = resolved_character
                    dialogues.append(Dialogue.model_validate(safe_dialogue))
                scene.dialogues = dialogues
            if isinstance(item.get("start_state"), dict):
                start_state = {
                    key: value
                    for key, value in item["start_state"].items()
                    if key in ContinuityState.model_fields
                }
                for field_name in ("character_positions", "character_wardrobe"):
                    if isinstance(start_state.get(field_name), dict):
                        start_state[field_name] = _canonicalize_reference_dict(
                            start_state[field_name],
                            character_id_map,
                            project.characters,
                        )
                if isinstance(start_state.get("prop_positions"), dict):
                    start_state["prop_positions"] = _canonicalize_reference_dict(
                        start_state["prop_positions"],
                        prop_id_map,
                        project.props,
                    )
                scene.start_state = ContinuityState.model_validate(start_state)
            if isinstance(item.get("end_state"), dict):
                end_state = {
                    key: value
                    for key, value in item["end_state"].items()
                    if key in ContinuityState.model_fields
                }
                for field_name in ("character_positions", "character_wardrobe"):
                    if isinstance(end_state.get(field_name), dict):
                        end_state[field_name] = _canonicalize_reference_dict(
                            end_state[field_name],
                            character_id_map,
                            project.characters,
                        )
                if isinstance(end_state.get("prop_positions"), dict):
                    end_state["prop_positions"] = _canonicalize_reference_dict(
                        end_state["prop_positions"],
                        prop_id_map,
                        project.props,
                    )
                scene.end_state = ContinuityState.model_validate(end_state)
        except (ValueError, TypeError):
            pass
        visible_ids = set(scene.characters)
        present_props = _present_prop_ids(scene.source_text, project.props)
        scene.start_state = _sanitize_state_for_scene(
            scene.start_state,
            visible_character_ids=visible_ids,
            present_prop_ids=present_props,
        )
        scene.end_state = _sanitize_state_for_scene(
            scene.end_state,
            visible_character_ids=visible_ids,
            present_prop_ids=present_props,
        )
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
            all_characters=project.characters,
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
    return finalize_project(project, source_project=draft)
