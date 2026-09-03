"""Canonical visual and Google Flow prompt generation."""

from __future__ import annotations

from ..models import Character, ContinuityState, Location, Scene, VideoSettings

NEGATIVE_CONSTRAINTS = (
    "identity drift, face changes, hairstyle changes, clothing changes, location changes, "
    "duplicated people, extra fingers, malformed hands, distorted anatomy, disappearing objects, "
    "teleporting characters, inconsistent props, random background changes, sudden lighting "
    "changes, unrealistic motion, floating objects, text artifacts, unrequested logos, "
    "inconsistent scale, inconsistent character age"
)


def global_visual_style(settings: VideoSettings) -> str:
    chosen = settings.custom_style.strip() or settings.style
    return (
        f"{chosen}. Cinematic coherent film language, realistic human anatomy, physically accurate "
        "lighting, natural motion, stable character identity, stable wardrobe, object permanence, "
        "consistent environment architecture and color grading, "
        f"{settings.aspect_ratio} composition, "
        f"{settings.resolution}, coherent temporal continuity."
    )


def describe_character(character: Character) -> str:
    return (
        f"{character.id} ({character.name}): {character.gender}, {character.estimated_age}, "
        f"{character.build}, {character.face}, hair {character.hairstyle}/{character.hair_color}, "
        f"eyes {character.eye_color}, skin {character.skin_tone}; wearing {character.clothing}; "
        f"accessories {character.accessories}; identifying feature: "
        f"{character.identifying_features}."
    )


def describe_location(location: Location) -> str:
    objects = ", ".join(location.objects) if location.objects else "fixed environmental objects"
    return (
        f"{location.id} ({location.name}): {location.place_type}; {location.architecture}; "
        f"layout {location.space}; colors {location.colors}; lighting {location.lighting}; "
        f"time {location.time_of_day}; weather {location.weather}; "
        f"anchors {location.spatial_anchors}; "
        f"objects: {objects}."
    )


def compact_state(state: ContinuityState) -> str:
    positions = "; ".join(f"{key}: {value}" for key, value in state.character_positions.items())
    props = "; ".join(f"{key}: {value}" for key, value in state.prop_positions.items())
    return (
        f"Characters [{positions or 'as established'}]. Props [{props or 'as established'}]. "
        f"Time: {state.time}. Weather: {state.weather}. Camera: {state.camera}. {state.notes}"
    ).strip()


def make_visual_prompt(
    *,
    action: str,
    characters: list[Character],
    location: Location,
    camera: str,
    lighting: str,
    atmosphere: str,
    style: str,
    start_state: ContinuityState,
    end_state: ContinuityState,
) -> str:
    subjects = " ".join(describe_character(item) for item in characters) or "No visible character."
    return (
        f"SUBJECT & APPEARANCE: {subjects}\n"
        f"LOCATION: {describe_location(location)}\n"
        f"ACTION: {action}\nCAMERA: {camera}\nLIGHTING: {lighting}\n"
        f"ATMOSPHERE: {atmosphere}\nSTYLE: {style}\n"
        f"CONTINUITY / START FRAME: {compact_state(start_state)}\n"
        f"END FRAME: {compact_state(end_state)}"
    )


def make_flow_prompt(
    scene: Scene,
    *,
    characters: list[Character],
    location: Location,
    visual_style: str,
    previous_scene_id: str | None,
) -> str:
    continuity = (
        f"Direct continuation of {previous_scene_id}."
        if previous_scene_id
        else "Opening scene; establish the canonical story world."
    )
    character_text = "\n".join(describe_character(item) for item in characters)
    return f"""SCENE ID: {scene.id}

CANONICAL CONTINUITY LOCK — DO NOT REINTERPRET OR REDESIGN ANY LOCKED ATTRIBUTE.

Continuity:
{continuity}

Character:
{character_text or "No visible character; preserve established world."}

Location:
{describe_location(location)}

Action:
{scene.action}

Camera:
{scene.camera}

Lighting:
{scene.lighting}

Environment:
{scene.atmosphere}

Visual style:
{visual_style}

Start frame:
{compact_state(scene.start_state)}

End frame:
{compact_state(scene.end_state)}

Consistency requirements:
Reproduce the exact same face geometry, age, body proportions, hairstyle, hair color, skin tone,
wardrobe and accessories for every recurring character. Reproduce the exact same architecture,
room layout, fixed objects, palette, weather and light sources. Maintain props, screen direction
and the final frame of the previous scene. No redesign or improvisation of locked attributes.
Natural physics and object permanence. Duration
{scene.duration} seconds, aspect ratio composition inherited from project settings.

Avoid:
{NEGATIVE_CONSTRAINTS}."""
