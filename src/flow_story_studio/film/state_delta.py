"""Deterministic state transition engine."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from ..models import ContinuityState, Scene
from .canonical import (
    CanonicalFilmModel,
    CharacterState,
    GlobalFilmState,
    PropState,
    StateDelta,
    ValidationResult,
)


class StateDeltaEngine:
    def __init__(self, film_model: CanonicalFilmModel) -> None:
        self.film_model = film_model

    def validate_delta(
        self, delta: StateDelta, current_state: GlobalFilmState
    ) -> ValidationResult:
        errors: list[str] = []
        warnings: list[str] = []
        for item in delta.character_deltas:
            if item.entity_id not in self.film_model.characters:
                errors.append(f"Unknown character: {item.entity_id}")
                continue
            if item.field == "location" and item.new_value:
                if item.new_value not in self.film_model.locations:
                    errors.append(
                        f"Unknown location for {item.entity_id}: {item.new_value}"
                    )
            state = current_state.character_states.get(item.entity_id)
            if state is not None and item.old_value is not None:
                current = _character_field(state, item.field)
                if current != item.old_value:
                    errors.append(
                        f"Stale character delta {item.entity_id}.{item.field}: "
                        f"{item.old_value!r}!={current!r}"
                    )

        for item in delta.prop_deltas:
            if item.entity_id not in self.film_model.props:
                errors.append(f"Unknown prop: {item.entity_id}")
                continue
            if item.field == "owner" and item.new_value:
                if item.new_value not in self.film_model.characters:
                    errors.append(
                        f"Unknown owner for {item.entity_id}: {item.new_value}"
                    )
            state = current_state.prop_states.get(item.entity_id)
            if state is not None and item.old_value is not None:
                current = _prop_field(state, item.field)
                if current != item.old_value:
                    errors.append(
                        f"Stale prop delta {item.entity_id}.{item.field}: "
                        f"{item.old_value!r}!={current!r}"
                    )
        if delta.active_location and delta.active_location not in self.film_model.locations:
            errors.append(f"Unknown active location: {delta.active_location}")
        return ValidationResult(
            is_valid=not errors,
            errors=errors,
            warnings=warnings,
        )

    def apply_delta(
        self, delta: StateDelta, current_state: GlobalFilmState
    ) -> GlobalFilmState:
        verdict = self.validate_delta(delta, current_state)
        if not verdict.is_valid:
            raise ValueError("; ".join(verdict.errors))

        new_state = current_state.model_copy(deep=True)
        for item in delta.character_deltas:
            state = new_state.character_states.setdefault(
                item.entity_id, CharacterState(entity_id=item.entity_id)
            )
            _set_character_field(state, item.field, item.new_value)

        for item in delta.prop_deltas:
            state = new_state.prop_states.setdefault(
                item.entity_id, PropState(entity_id=item.entity_id)
            )
            _set_prop_field(state, item.field, item.new_value)

        if delta.active_location is not None:
            new_state.active_location = delta.active_location
        if delta.timeline_branch is not None:
            new_state.timeline_branch = delta.timeline_branch
        for key, value in delta.environment_changes.items():
            if hasattr(new_state.environment, key):
                setattr(new_state.environment, key, value)
        for key, value in delta.audio_changes.items():
            if hasattr(new_state.audio, key):
                setattr(new_state.audio, key, value)

        new_state.scene_number = delta.target_scene
        new_state.scene_id = delta.source_scene_id
        return new_state


def direct_start_state(previous: GlobalFilmState, scene_id: str) -> GlobalFilmState:
    state = previous.model_copy(deep=True)
    state.scene_number = previous.scene_number + 1
    state.scene_id = scene_id
    return state


def _character_field(state: CharacterState, field: str):
    aliases = {
        "location": "location_id",
        "wardrobe": "wardrobe_version",
        "possession": "possessions",
    }
    return getattr(state, aliases.get(field, field), None)


def _prop_field(state: PropState, field: str):
    aliases = {"location": "location_id", "owner": "owner_id"}
    return getattr(state, aliases.get(field, field), None)
def _set_character_field(state: CharacterState, field: str, value) -> None:
    aliases = {
        "location": "location_id",
        "wardrobe": "wardrobe_version",
        "possession": "possessions",
    }
    name = aliases.get(field, field)
    if not hasattr(state, name):
        raise ValueError(f"Unsupported character state field: {field}")
    setattr(state, name, value)


def _set_prop_field(state: PropState, field: str, value) -> None:
    aliases = {"location": "location_id", "owner": "owner_id"}
    name = aliases.get(field, field)
    if not hasattr(state, name):
        raise ValueError(f"Unsupported prop state field: {field}")
    setattr(state, name, value)


def continuity_state_hash(state: ContinuityState) -> str:
    payload = json.dumps(
        state.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def derive_scene_state_delta(scene: Scene) -> dict[str, Any]:
    before = scene.start_state.model_dump(mode="json")
    after = scene.end_state.model_dump(mode="json")
    changes: dict[str, Any] = {}
    for key in sorted(set(before) | set(after)):
        if before.get(key) != after.get(key):
            changes[key] = {
                "from": before.get(key),
                "to": after.get(key),
            }
    return changes


def clear_accepted_runtime_state(scene: Scene) -> None:
    scene.accepted_end_state = None
    scene.accepted_state_hash = ""


def commit_accepted_runtime_state(scene: Scene) -> None:
    if scene.acceptance.status != "Accepted":
        raise ValueError("Cannot commit runtime state before Production Acceptance")
    scene.accepted_end_state = scene.end_state.model_copy(deep=True)
    scene.accepted_state_hash = continuity_state_hash(scene.accepted_end_state)
