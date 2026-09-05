"""Canonical film/state models for deterministic AI orchestration."""
from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AuthorityLevel(StrEnum):
    SOURCE_LOCKED = "source_locked"
    DERIVED_LOCKED = "derived_locked"
    CREATIVE_ALLOWED = "creative_allowed"


class DependencyMode(StrEnum):
    OPENING = "opening"
    DIRECT = "direct"
    CANONICAL = "canonical"
    TIME_JUMP = "time_jump"
    FLASHBACK = "flashback"
    PARALLEL = "parallel"
    LOCATION_TRANSITION = "location_transition"
    MONTAGE = "montage"


class ValidationResult(StrictModel):
    is_valid: bool = True
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class EntityLock(StrictModel):
    entity_id: str
    source_name: str
    lock_text: str = ""
    authority: AuthorityLevel = AuthorityLevel.SOURCE_LOCKED


class EntityVersion(StrictModel):
    entity_id: str
    version_id: str
    lock_text: str = ""
    valid_from_scene: int = 0
    authority: AuthorityLevel = AuthorityLevel.DERIVED_LOCKED


class CharacterState(StrictModel):
    entity_id: str
    location_id: str = ""
    position: str = ""
    wardrobe_version: str = "V1"
    condition: str = ""
    emotion: str = ""
    possessions: list[str] = Field(default_factory=list)
    visibility: str = "onscreen"


class PropState(StrictModel):
    entity_id: str
    location_id: str = ""
    owner_id: str = ""
    condition: str = "intact"
    visibility: str = "offscreen"
    transformation: str = ""


class EnvironmentState(StrictModel):
    time_label: str = ""
    weather: str = ""
    lighting: str = ""


class AudioState(StrictModel):
    ambience_id: str = ""
    music_id: str = ""
    persistent_sounds: list[str] = Field(default_factory=list)


class GlobalFilmState(StrictModel):
    scene_number: int = 0
    scene_id: str = ""
    timeline_branch: str = "main"
    active_location: str = ""
    character_states: dict[str, CharacterState] = Field(default_factory=dict)
    prop_states: dict[str, PropState] = Field(default_factory=dict)
    environment: EnvironmentState = Field(default_factory=EnvironmentState)
    audio: AudioState = Field(default_factory=AudioState)


class CharacterDelta(StrictModel):
    entity_id: str
    field: str
    old_value: Any = None
    new_value: Any = None
    authority: AuthorityLevel = AuthorityLevel.SOURCE_LOCKED


class PropDelta(StrictModel):
    entity_id: str
    field: str
    old_value: Any = None
    new_value: Any = None
    authority: AuthorityLevel = AuthorityLevel.SOURCE_LOCKED


class StateDelta(StrictModel):
    source_scene_id: str
    target_scene: int
    character_deltas: list[CharacterDelta] = Field(default_factory=list)
    prop_deltas: list[PropDelta] = Field(default_factory=list)
    active_location: str | None = None
    timeline_branch: str | None = None
    environment_changes: dict[str, str] = Field(default_factory=dict)
    audio_changes: dict[str, Any] = Field(default_factory=dict)


class SceneIntent(StrictModel):
    scene_id: str
    narrative_goal: str = ""
    required_characters: list[str] = Field(default_factory=list)
    required_props: list[str] = Field(default_factory=list)
    required_location: str = ""
    required_dialogue: list[str] = Field(default_factory=list)
    required_actions: list[str] = Field(default_factory=list)
    timeline_branch: str = "main"
    dependency_mode: DependencyMode = DependencyMode.CANONICAL
    state_delta: StateDelta | None = None


class AudioBible(StrictModel):
    voice_locks: dict[str, str] = Field(default_factory=dict)
    ambience_locks: dict[str, str] = Field(default_factory=dict)
    music_locks: dict[str, str] = Field(default_factory=dict)
    dialogue_locks: dict[str, str] = Field(default_factory=dict)
    target_lufs: float = -16.0
    true_peak_db: float = -1.0
    loudness_tolerance_lu: float = 4.0


class CanonicalFilmModel(StrictModel):
    film_id: str
    characters: dict[str, EntityLock] = Field(default_factory=dict)
    locations: dict[str, EntityLock] = Field(default_factory=dict)
    props: dict[str, EntityLock] = Field(default_factory=dict)
    character_versions: dict[str, list[EntityVersion]] = Field(default_factory=dict)
    location_versions: dict[str, list[EntityVersion]] = Field(default_factory=dict)
    prop_versions: dict[str, list[EntityVersion]] = Field(default_factory=dict)
    audio_bible: AudioBible = Field(default_factory=AudioBible)
    source_facts: dict[str, Any] = Field(default_factory=dict)
    global_constraints: dict[str, Any] = Field(default_factory=dict)

    def has_entity(self, entity_id: str) -> bool:
        return (
            entity_id in self.characters
            or entity_id in self.locations
            or entity_id in self.props
        )
