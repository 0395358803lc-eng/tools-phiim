"""Deterministic production finalization independent of AI response wording.

The AI provider is treated as an enrichment source only.  This module owns the
last mile before Production: canonical references, nested-state integrity,
camera/cast compatibility, prompt recompilation, and structural validation.
"""

from __future__ import annotations

import re
import unicodedata

from ..engines.continuity import check_project
from ..engines.prompt_generator import make_flow_prompt, make_visual_prompt
from ..models import Character, ContinuityState, Location, Project, Prop
from ..visual_bible import build_visual_bible
from .audio_finalization import finalize_audio
from .semantic_orchestrator import (
    normalize_semantic_scene,
    remap_to_world,
    source_canonical_world,
)


def _key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value).casefold().replace("đ", "d"))
    ascii_text = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return " ".join(re.findall(r"[a-z0-9]+", ascii_text))


def _canonicalize_entities(items: list[Character] | list[Location] | list[Prop]):
    """Collapse exact semantic duplicates while preserving first stable ID."""
    canonical = []
    remap: dict[str, str] = {}
    seen: dict[str, object] = {}
    for item in items:
        key = _key(item.name)
        existing = seen.get(key) if key else None
        if existing is None:
            canonical.append(item)
            if key:
                seen[key] = item
            remap[item.id] = item.id
            continue
        remap[item.id] = existing.id  # type: ignore[attr-defined]
    return canonical, remap


def _remap_state(
    state: ContinuityState,
    *,
    character_map: dict[str, str],
    prop_map: dict[str, str],
    visible_character_ids: set[str],
    valid_prop_ids: set[str],
) -> ContinuityState:
    data = state.model_dump()
    for field in ("character_positions", "character_wardrobe"):
        out: dict[str, str] = {}
        for raw_id, value in (data.get(field) or {}).items():
            canonical_id = character_map.get(raw_id, raw_id)
            if canonical_id in visible_character_ids:
                out.setdefault(canonical_id, value)
        data[field] = out
    props: dict[str, str] = {}
    for raw_id, value in (data.get("prop_positions") or {}).items():
        canonical_id = prop_map.get(raw_id, raw_id)
        if canonical_id in valid_prop_ids:
            props.setdefault(canonical_id, value)
    data["prop_positions"] = props
    return ContinuityState.model_validate(data)


def _camera_mentions_multiple_people(camera: str) -> bool:
    value = _key(camera)
    markers = (
        "two shot",
        "two person",
        "two people",
        "both characters",
        "both subjects",
        "hai nguoi",
        "hai nhan vat",
        "doi dien nhau",
        "couple shot",
        "group shot",
    )
    return any(marker in value for marker in markers)


def _camera_names_absent_character(
    camera: str,
    visible_ids: set[str],
    characters: list[Character],
) -> bool:
    # Use Unicode word boundaries on the original text for short names; do not
    # accent-fold them, because e.g. AN must never collide with Vietnamese "ăn".
    raw = camera.casefold()
    folded = _key(camera)
    for character in characters:
        if character.id in visible_ids:
            continue
        name = character.name.casefold().strip()
        words = re.findall(r"[\wÀ-ỹ]+", name, re.UNICODE)
        if len(words) == 1 and len(words[0]) <= 3:
            if re.search(rf"(?<!\w){re.escape(words[0])}(?!\w)", raw, re.UNICODE):
                return True
            continue
        key = _key(character.name)
        if key and re.search(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", folded):
            return True
    return False


def _sanitize_camera(
    camera: str,
    visible_ids: set[str],
    characters: list[Character],
) -> tuple[str, bool]:
    conflict = _camera_names_absent_character(camera, visible_ids, characters)
    if len(visible_ids) < 2 and _camera_mentions_multiple_people(camera):
        conflict = True
    if not conflict:
        return camera, False
    if not visible_ids:
        return (
            "Wide environmental cinematic shot focused on the location and physical action; "
            "no person is visible in frame",
            True,
        )
    if len(visible_ids) == 1:
        return (
            "Medium single-subject cinematic shot framing only the visible character; "
            "no second person or implied partner appears in frame",
            True,
        )
    return (
        "Balanced multi-subject cinematic composition containing only the scene's visible cast",
        True,
    )


def _scene_context(scene) -> str:
    match = re.search(
        r"\[SCENE CONTEXT\](.*?)\[END CONTEXT\]",
        scene.source_text,
        re.DOTALL,
    )
    return _key(match.group(1) if match else "")


def _source_time_label(scene, previous_scene) -> str:
    context = _scene_context(scene)
    if ("lien tuc" in context or "continuous" in context) and previous_scene is not None:
        previous_context = _scene_context(previous_scene)
        previous_flashback = "flashback" in previous_context
        current_flashback = "flashback" in context
        same_location = previous_scene.location_id == scene.location_id
        same_timeline = previous_flashback == current_flashback
        if same_location and same_timeline:
            return previous_scene.end_state.time

    flashback = "flashback" in context
    parallel = "song song" in context or "parallel" in context
    daypart = ""
    for marker, label in (
        ("binh minh", "dawn"),
        ("sang", "morning"),
        ("trua", "day"),
        ("chieu", "afternoon"),
        ("toi", "evening"),
        ("dem", "night"),
        ("night", "night"),
        ("day", "day"),
        ("morning", "morning"),
        ("afternoon", "afternoon"),
        ("evening", "evening"),
        ("dawn", "dawn"),
        ("dusk", "dusk"),
    ):
        if marker in context:
            daypart = label
            break

    if flashback:
        return f"Flashback — {daypart or 'source-defined time'}"
    if parallel:
        return f"Present parallel — {daypart or 'source-defined time'}"
    return f"Present — {daypart or 'source-defined time'}"


def _normalize_source_timeline(project: Project) -> Project:
    previous_scene = None
    for scene in project.scenes:
        label = _source_time_label(scene, previous_scene)
        scene.start_state.time = label
        scene.end_state.time = label
        previous_scene = scene
    return project


def _assert_structural_integrity(project: Project) -> None:
    character_ids = {item.id for item in project.characters}
    location_ids = {item.id for item in project.locations}
    prop_ids = {item.id for item in project.props}
    expected_orders = list(range(1, len(project.scenes) + 1))
    if [scene.order for scene in project.scenes] != expected_orders:
        raise ValueError("Scene order is not contiguous after production finalization")

    for scene in project.scenes:
        visible = set(scene.characters)
        if not visible <= character_ids:
            raise ValueError(f"Invalid character reference after finalization: {scene.id}")
        if scene.location_id not in location_ids:
            raise ValueError(f"Invalid location reference after finalization: {scene.id}")
        for dialogue in scene.dialogues:
            if dialogue.character_id not in character_ids:
                raise ValueError(f"Invalid dialogue speaker after finalization: {scene.id}")
        for state in (scene.start_state, scene.end_state):
            nested_chars = set(state.character_positions) | set(state.character_wardrobe)
            if not nested_chars <= visible:
                raise ValueError(f"Invalid nested visual cast after finalization: {scene.id}")
            if not set(state.prop_positions) <= prop_ids:
                raise ValueError(f"Invalid prop reference after finalization: {scene.id}")


def finalize_project(project: Project, source_project: Project | None = None) -> Project:
    """Finalize AI enrichment against screenplay-grounded semantic truth."""
    old_characters = list(project.characters)
    old_locations = list(project.locations)
    old_props = list(project.props)

    project.characters, project.locations, project.props = source_canonical_world(
        project, source_project
    )
    character_map = remap_to_world(old_characters, project.characters)
    location_map = remap_to_world(old_locations, project.locations)
    prop_map = remap_to_world(old_props, project.props)

    valid_character_ids = {item.id for item in project.characters}
    valid_location_ids = {item.id for item in project.locations}
    valid_prop_ids = {item.id for item in project.props}
    fallback_location = project.locations[0].id if project.locations else ""

    for scene in project.scenes:
        scene.characters = list(
            dict.fromkeys(
                character_map.get(character_id, character_id)
                for character_id in scene.characters
                if character_map.get(character_id, character_id) in valid_character_ids
            )
        )
        for dialogue in scene.dialogues:
            dialogue.character_id = character_map.get(dialogue.character_id, dialogue.character_id)
        scene.location_id = location_map.get(scene.location_id, scene.location_id)
        if scene.location_id not in valid_location_ids and fallback_location:
            scene.location_id = fallback_location

        visible = set(scene.characters)
        scene.start_state = _remap_state(
            scene.start_state,
            character_map=character_map,
            prop_map=prop_map,
            visible_character_ids=visible,
            valid_prop_ids=valid_prop_ids,
        )
        scene.end_state = _remap_state(
            scene.end_state,
            character_map=character_map,
            prop_map=prop_map,
            visible_character_ids=visible,
            valid_prop_ids=valid_prop_ids,
        )

    # Semantic orchestration is source-grounded and therefore runs after ID remapping.
    previous_scene = None
    for scene in project.scenes:
        normalize_semantic_scene(
            scene,
            characters=project.characters,
            props=project.props,
            previous_scene=previous_scene,
        )
        previous_scene = scene

    project = check_project(project, auto_fix=True)
    project = finalize_audio(project, source_project)

    # Auto continuity can rewrite nested state; normalize all semantic state first.
    previous_scene = None
    for scene in project.scenes:
        normalize_semantic_scene(
            scene,
            characters=project.characters,
            props=project.props,
            previous_scene=previous_scene,
        )
        visible = set(scene.characters)
        scene.start_state = _remap_state(
            scene.start_state,
            character_map=character_map,
            prop_map=prop_map,
            visible_character_ids=visible,
            valid_prop_ids=valid_prop_ids,
        )
        scene.end_state = _remap_state(
            scene.end_state,
            character_map=character_map,
            prop_map=prop_map,
            visible_character_ids=visible,
            valid_prop_ids=valid_prop_ids,
        )
        previous_scene = scene

    project = _normalize_source_timeline(project)
    # Audio finalization can remove AI-authored voiceover and semantic normalization can
    # resolve camera/cast conflicts. Recompute current continuity without mutating the
    # already source-grounded state so stale pre-finalization warnings do not survive.
    project = check_project(project, auto_fix=False)

    # Build the Visual Bible from the exact final semantic state, then compile prompts.
    project = build_visual_bible(project)
    for index, scene in enumerate(project.scenes):
        visible = set(scene.characters)
        location = next(item for item in project.locations if item.id == scene.location_id)
        visible_characters = [item for item in project.characters if item.id in visible]
        scene.visual_prompt = make_visual_prompt(
            action=scene.action,
            characters=visible_characters,
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
            characters=visible_characters,
            location=location,
            visual_style=project.visual_style,
            all_characters=project.characters,
            previous_scene_id=project.scenes[index - 1].id if index else None,
        )

    _assert_structural_integrity(project)
    return project
