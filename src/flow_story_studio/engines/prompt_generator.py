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
    all_characters: list[Character] | None = None,
) -> str:
    dependency_mode = scene.visual_plan.dependency_mode
    if dependency_mode == "opening":
        continuity = "Opening scene; establish the canonical story world."
    elif dependency_mode == "direct" and previous_scene_id:
        if scene.visual_plan.inherit_previous_frame:
            continuity = (
                f"Direct continuation of {previous_scene_id}; begin from that scene's accepted "
                "final frame and preserve physical state."
            )
        else:
            continuity = (
                f"Continuous story beat after {previous_scene_id}, but do not use its accepted "
                "final frame as a literal start-frame anchor. Re-anchor to this scene's "
                "source-truth start state because the authored boundary changes physical state."
            )
    else:
        continuity = (
            "Canonical cut/new beat; re-anchor to this scene's source truth and canonical "
            "references. Do not copy the previous scene's composition or final frame."
        )
    character_text = "\n".join(describe_character(item) for item in characters)
    name_by_id = {item.id: item.name for item in (all_characters or characters)}
    dialogue_lines = [
        f'- {name_by_id.get(item.character_id, item.character_id)}: "{item.text}" '
        f"[delivery/emotion: {item.emotion}]"
        for item in scene.dialogues
    ]
    if scene.voiceover:
        dialogue_lines.append(f'- VOICEOVER/NARRATION: "{scene.voiceover}"')
    audio_text = "\n".join(dialogue_lines) or "No spoken dialogue or voiceover in this scene."
    return f"""SCENE ID: {scene.id}

CANONICAL CONTINUITY LOCK — DO NOT REINTERPRET OR REDESIGN ANY LOCKED ATTRIBUTE.

Continuity:
{continuity}

VISUAL BIBLE LOCKS:
{scene.visual_plan.lock_prompt or "Use canonical project visual identity without redesign."}
Dependency mode: {scene.visual_plan.dependency_mode};
Inherit previous frame: {str(scene.visual_plan.inherit_previous_frame).lower()};
anchor scene: {scene.visual_plan.anchor_scene_id or scene.id}.

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

SOURCE TRUTH — VERBATIM:
{scene.source_text}
Preserve every explicit clock value, on-screen label/message, prop condition, sound cue and
causal action from the source beat above. Do not translate or paraphrase authored on-screen text.
Stage directions and SFX are not spoken dialogue unless the screenplay explicitly makes them
spoken.

AUDIO / DIALOGUE LOCK:
{audio_text}
Use the exact source-grounded speaker and words above. Do not paraphrase, invent narration,
change speaker identity, or turn on-screen text/stage direction into spoken audio. When a line is
phone/recorded/off-screen by scene context, preserve that delivery without making the speaker
visible.

Visual style:
{visual_style}

Start frame:
{compact_state(scene.start_state)}

End frame:
{compact_state(scene.end_state)}

Consistency requirements:
Reproduce the exact same face geometry, age, body proportions, hairstyle, hair color, skin tone,
wardrobe and accessories for every recurring character. Reproduce the exact same architecture,
room layout, fixed objects, palette, weather and light sources. Preserve source-grounded prop state
and screen direction. The previous accepted final frame may be used as a literal next start-frame
anchor only when Inherit previous frame is true. A direct story continuation with that flag false
must re-anchor to this scene's source-truth start state and canonical references. Canonical cuts
must also start from their own source truth and canonical references. No redesign or improvisation
of locked attributes. Natural physics and
object permanence. Duration
{scene.duration} seconds, aspect ratio composition inherited from project settings.

Avoid:
{NEGATIVE_CONSTRAINTS}."""
