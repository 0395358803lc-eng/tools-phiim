"""Source-grounded dialogue and voiceover finalization.

The screenplay text is authoritative for spoken content. AI responses may enrich visual
fields, but they cannot invent, delete, reassign, or concatenate spoken lines.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from ..models import Character, Dialogue, Project, Scene


@dataclass(frozen=True)
class AudioEvent:
    scene_number: int | None
    speaker_id: str | None
    text: str
    kind: str  # dialogue | voiceover


def _fold(value: str) -> str:
    raw = str(value).casefold().replace("đ", "d")
    normalized = unicodedata.normalize("NFKD", raw)
    ascii_text = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return " ".join(re.findall(r"[a-z0-9]+", ascii_text))


def _scene_number_from_heading(line: str) -> int | None:
    folded = _fold(line)
    match = re.search(r"(?:canh|scene)\s+(\d{1,4})\b", folded)
    return int(match.group(1)) if match else None


def _scene_number_from_source(scene: Scene) -> int | None:
    context = re.search(r"\[SCENE CONTEXT\](.*?)\[END CONTEXT\]", scene.source_text, re.DOTALL)
    if context:
        number = _scene_number_from_heading(context.group(1))
        if number is not None:
            return number
    return _scene_number_from_heading(scene.source_text)


def _character_from_label(label: str, characters: list[Character]) -> Character | None:
    folded = _fold(label)
    # Remove channel/voice descriptors but preserve the actual character identity.
    folded = re.sub(
        r"\b(?:giong|voice|trong may ghi am|qua dien thoai|through the phone|"
        r"over the phone|on the phone|v o|o s|recorded)\b",
        " ",
        folded,
    )
    folded = " ".join(folded.split())
    best: Character | None = None
    best_score = -1
    for character in characters:
        name = _fold(character.name)
        if not name:
            continue
        if folded == name:
            return character
        if re.search(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", folded):
            score = len(name)
            if score > best_score:
                best = character
                best_score = score
    return best


def _is_voiceover_label(label: str) -> bool:
    folded = _fold(label)
    # Recorded/phone voices belong to canonical speakers, not narration voiceover.
    if any(
        marker in folded for marker in ("may ghi am", "recorder", "dien thoai", "phone", "recorded")
    ):
        return False
    return any(
        marker in folded
        for marker in ("voiceover", "voice over", "narrator", "nguoi dan chuyen", "v o")
    )


def _looks_like_speaker_label(line: str, characters: list[Character]) -> bool:
    stripped = line.strip()
    if not stripped or len(stripped) > 80:
        return False
    folded = _fold(stripped)
    ui_markers = (
        "cuoc goi den",
        "incoming call",
        "caller id",
        "tin nhan tu",
        "message from",
        "text from",
        "man hinh hien",
        "screen shows",
    )
    if any(marker in folded for marker in ui_markers):
        return False
    if _is_voiceover_label(stripped):
        return True
    if _character_from_label(stripped, characters) is None:
        return False
    # Screenplay speaker labels are normally uppercase; allow GIỌNG/VOICE prefixes.
    letters = [ch for ch in stripped if ch.isalpha()]
    return bool(letters) and (
        all(ch.isupper() for ch in letters) or _fold(stripped).startswith(("giong ", "voice "))
    )


def parse_screenplay_audio(original_text: str, characters: list[Character]) -> list[AudioEvent]:
    """Parse only explicit speaker-labelled spoken content from the screenplay."""
    lines = re.sub(r"\r\n?", "\n", original_text).splitlines()
    events: list[AudioEvent] = []
    current_scene: int | None = None
    index = 0
    while index < len(lines):
        raw = lines[index]
        line = raw.strip()
        scene_number = _scene_number_from_heading(line)
        if scene_number is not None:
            current_scene = scene_number
            index += 1
            continue
        if not _looks_like_speaker_label(line, characters):
            index += 1
            continue

        speaker = _character_from_label(line, characters)
        kind = "voiceover" if _is_voiceover_label(line) and speaker is None else "dialogue"
        text_lines: list[str] = []
        cursor = index + 1
        while cursor < len(lines):
            candidate = lines[cursor].strip()
            if not candidate:
                if text_lines:
                    break
                cursor += 1
                continue
            if _scene_number_from_heading(candidate) is not None:
                break
            if _looks_like_speaker_label(candidate, characters):
                break
            # Dialogue paragraphs end before screenplay action/narrative paragraphs.
            # In conventional screenplay formatting, spoken text immediately follows
            # the label and is a compact paragraph; consume only the first non-empty line.
            text_lines.append(candidate)
            break
        text = " ".join(text_lines).strip()
        if text:
            events.append(
                AudioEvent(
                    scene_number=current_scene,
                    speaker_id=speaker.id if speaker is not None else None,
                    text=text,
                    kind=kind,
                )
            )
        index = max(cursor, index + 1)
    return events


def _event_belongs_to_scene(event: AudioEvent, scene: Scene) -> bool:
    scene_number = _scene_number_from_source(scene)
    if event.scene_number is not None and scene_number is not None:
        if event.scene_number != scene_number:
            return False
    # When a screenplay scene has been split into multiple production shots, place the
    # event only in the shot whose source chunk actually contains the spoken text.
    source_folded = _fold(scene.source_text)
    event_folded = _fold(event.text)
    return bool(event_folded and event_folded in source_folded)


def finalize_audio(project: Project, source_project: Project | None = None) -> Project:
    """Replace AI audio fields with source-grounded dialogue/voiceover events."""
    source = source_project or project
    events = parse_screenplay_audio(source.original_text, project.characters)

    for scene in project.scenes:
        dialogues: list[Dialogue] = []
        voiceovers: list[str] = []
        for event in events:
            if not _event_belongs_to_scene(event, scene):
                continue
            if event.kind == "voiceover":
                voiceovers.append(event.text)
                continue
            if event.speaker_id is None:
                continue
            dialogues.append(
                Dialogue(
                    character_id=event.speaker_id,
                    text=event.text,
                    emotion="Theo ngữ cảnh kịch bản gốc",
                )
            )
        scene.dialogues = dialogues
        scene.voiceover = " ".join(voiceovers).strip()
        # Remove warnings created from AI/free-form voiceover that no longer exists.
        scene.warnings = [
            warning
            for warning in scene.warnings
            if "Voiceover" not in warning and "voiceover" not in warning
        ]
    return project
