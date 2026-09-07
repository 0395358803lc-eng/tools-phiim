"""Canonical visual identity manifest and scene dependency planning."""

from __future__ import annotations

import re

from .engines.continuity import enforce_frame_anchor_policy
from .film.canonical import DependencyMode
from .film.dependency import classify_dependency
from .film.image_plan import compile_project_image_plans
from .film.orchestrator import prepare
from .models import Project, SceneVisualPlan, VisualBible, VisualReference


def _character_lock(item) -> str:
    return (
        f"{item.id} {item.name}: {item.gender}, {item.estimated_age}, {item.build}; "
        f"face {item.face}; hair {item.hairstyle}/{item.hair_color}; eyes {item.eye_color}; "
        f"skin {item.skin_tone}; clothing {item.clothing}; accessories {item.accessories}; "
        f"identifying features {item.identifying_features}."
    )


_TRANSIENT_LOCATION_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    (r"(?i)rain[- ]facing window", "window"),
    (r"(?i)rainy window", "window"),
    (r"(?i)rain window", "window"),
    (r"(?i)cửa sổ có mưa bên ngoài", "cửa sổ"),
    (r"(?i)cửa sổ mưa", "cửa sổ"),
    (r"(?i)đêm mưa", ""),
    (r"(?i)mưa nhẹ", ""),
    (r"(?i)rainy[- ]night", ""),
    (r"(?i)rainy", ""),
    (r"(?i)rain", ""),
    (r"(?i)\b23:\d{2}\b", ""),
    (r"(?i)\b\d{1,2}:\d{2}\b", ""),
    (r"(?i)công suất thấp", ""),
    (r"(?i)low[- ]output", ""),
    (r"(?i)hallway spill", ""),
    (r"(?i)light spill", ""),
    (r"(?i)rim light", ""),
    (r"(?i)ambient xanh lạnh", ""),
    (r"(?i)cold blue ambient", ""),
)


def _stable_location_fragment(value: object) -> str:
    text = str(value or "")
    for pattern, replacement in _TRANSIENT_LOCATION_REPLACEMENTS:
        text = re.sub(pattern, replacement, text)
    text = re.sub(r"\s+([,.;:])", r"\1", text)
    text = re.sub(r"\s{2,}", " ", text)
    text = re.sub(r"(?:[,;]\s*){2,}", "; ", text)
    return text.strip(" ,;.-")


def canonical_location_lock(item) -> str:
    """Build a reusable location identity that excludes scene-state attributes."""
    stable_objects = [
        cleaned
        for raw in item.objects
        if (cleaned := _stable_location_fragment(raw))
    ]
    objects = ", ".join(stable_objects) if stable_objects else "fixed environmental objects"
    return (
        f"{item.id} {item.name}: {_stable_location_fragment(item.place_type)}; "
        f"architecture {_stable_location_fragment(item.architecture)}; "
        f"layout {_stable_location_fragment(item.space)}; "
        f"interior {_stable_location_fragment(item.interior)}; "
        f"spatial anchors {_stable_location_fragment(item.spatial_anchors)}; "
        f"fixed objects {objects}. "
        "CANONICAL BASELINE EXCLUDES time-of-day, weather/precipitation, temporary lighting "
        "intensity/color/spill, people, action props, temporary clutter and readable display "
        "content. Fixed windows, doors and light fixtures may remain as physical anchors, but "
        "exterior weather and fixture on/off/intensity/color state are scene-level only."
    )


def canonical_reference_lock(project: Project, reference: VisualReference) -> str:
    if reference.entity_type == "location":
        item = next(
            (
                location
                for location in getattr(project, "locations", [])
                if location.id == reference.entity_id
            ),
            None,
        )
        if item is not None:
            return canonical_location_lock(item)
    if reference.entity_type == "character":
        item = next(
            (
                character
                for character in getattr(project, "characters", [])
                if character.id == reference.entity_id
            ),
            None,
        )
        if item is not None:
            return _character_lock(item)
    if reference.entity_type == "prop":
        item = next(
            (
                prop
                for prop in getattr(project, "props", [])
                if prop.id == reference.entity_id
            ),
            None,
        )
        if item is not None:
            return _prop_lock(item)
    return reference.lock_text


def _location_lock(item) -> str:
    return canonical_location_lock(item)


def _prop_lock(item) -> str:
    return (
        f"{item.id} {item.name}: {item.description}; owner {item.owner}; "
        f"initial location {item.initial_location}; physical state {item.state}."
    )


def build_visual_bible(project: Project) -> Project:
    project = prepare(project)
    project = enforce_frame_anchor_policy(project)
    existing = {item.entity_id: item for item in project.visual_bible.references}
    refs: list[VisualReference] = []
    for item in project.characters:
        refs.append(
            VisualReference(
                id=f"VIS-{item.id}",
                entity_type="character",
                entity_id=item.id,
                name=item.name,
                lock_text=_character_lock(item),
                reference_images=list(
                    dict.fromkeys(
                        [
                            *item.reference_images,
                            *(existing[item.id].reference_images if item.id in existing else []),
                        ]
                    )
                ),
                status=(
                    existing[item.id].status
                    if item.id in existing
                    else "approved"
                    if item.reference_images
                    else "missing"
                ),
                approved_reference=(
                    existing[item.id].approved_reference
                    if item.id in existing and existing[item.id].approved_reference
                    else item.reference_images[0]
                    if item.reference_images
                    else ""
                ),
                source_scene_id=(existing[item.id].source_scene_id if item.id in existing else ""),
            )
        )
    for item in project.locations:
        refs.append(
            VisualReference(
                id=f"VIS-{item.id}",
                entity_type="location",
                entity_id=item.id,
                name=item.name,
                lock_text=_location_lock(item),
                reference_images=list(
                    dict.fromkeys(
                        [
                            *item.reference_images,
                            *(existing[item.id].reference_images if item.id in existing else []),
                        ]
                    )
                ),
                status=(
                    existing[item.id].status
                    if item.id in existing
                    else "approved"
                    if item.reference_images
                    else "missing"
                ),
                approved_reference=(
                    existing[item.id].approved_reference
                    if item.id in existing and existing[item.id].approved_reference
                    else item.reference_images[0]
                    if item.reference_images
                    else ""
                ),
                source_scene_id=(existing[item.id].source_scene_id if item.id in existing else ""),
            )
        )
    for item in project.props:
        refs.append(
            VisualReference(
                id=f"VIS-{item.id}",
                entity_type="prop",
                entity_id=item.id,
                name=item.name,
                lock_text=_prop_lock(item),
                reference_images=(
                    list(existing[item.id].reference_images) if item.id in existing else []
                ),
                status=(existing[item.id].status if item.id in existing else "missing"),
                approved_reference=(
                    existing[item.id].approved_reference if item.id in existing else ""
                ),
                source_scene_id=(existing[item.id].source_scene_id if item.id in existing else ""),
            )
        )
    project.visual_bible = VisualBible(version=1, references=refs)
    by_entity = {ref.entity_id: ref for ref in refs}
    previous = None
    current_anchor = ""
    for scene in project.scenes:
        classified = classify_dependency(previous, scene)
        if classified == DependencyMode.OPENING:
            mode = "opening"
            current_anchor = scene.id
        elif classified == DependencyMode.DIRECT:
            mode = "direct"
        else:
            mode = "canonical"
            current_anchor = scene.id
        char_refs = [by_entity[cid].id for cid in scene.characters if cid in by_entity]
        loc_ref = by_entity[scene.location_id].id if scene.location_id in by_entity else ""
        prop_ids = sorted(
            set(scene.start_state.prop_positions) | set(scene.end_state.prop_positions)
        )
        prop_refs = [by_entity[pid].id for pid in prop_ids if pid in by_entity]
        relevant = [by_entity[cid].lock_text for cid in scene.characters if cid in by_entity]
        if scene.location_id in by_entity:
            relevant.append(by_entity[scene.location_id].lock_text)
        relevant.extend(by_entity[pid].lock_text for pid in prop_ids if pid in by_entity)
        scene.visual_plan = SceneVisualPlan(
            dependency_mode=mode,
            anchor_scene_id=current_anchor,
            character_reference_ids=char_refs,
            location_reference_id=loc_ref,
            prop_reference_ids=prop_refs,
            lock_prompt="\n".join(relevant),
        )
        previous = scene

    # Each boundary between a scene and its successor gets ONE shared note that is
    # written identically to the previous scene's end_state and the current scene's
    # start_state. Direct continuations copy the previous end_state into the current
    # start_state (including notes), so keeping them byte-identical preserves the
    # continuity invariant `current.start_state == previous.end_state` while the
    # note still describes the frame-anchoring dependency of that single boundary.
    for index, scene in enumerate(project.scenes):
        if index + 1 >= len(project.scenes):
            scene.end_state.notes = "Final scene; no downstream frame anchor."
            continue

        next_scene = project.scenes[index + 1]
        if next_scene.visual_plan.dependency_mode == "direct":
            boundary_note = (
                f"Direct continuation from {scene.id}; {next_scene.id} may anchor to this "
                "accepted final frame as the physical-state anchor."
            )
        else:
            boundary_note = (
                f"{next_scene.id} begins as a Canonical cut/new beat; re-anchor to source "
                "truth and canonical references. Do not carry the previous final frame forward."
            )
        scene.end_state.notes = boundary_note
        next_scene.start_state.notes = boundary_note

    if project.scenes:
        project.scenes[0].start_state.notes = (
            "Opening scene; establish from canonical source truth and visual references."
        )
    return compile_project_image_plans(project)
