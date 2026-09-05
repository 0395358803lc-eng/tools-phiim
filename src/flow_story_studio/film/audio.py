"""Canonical audio locks derived from source-grounded project data."""
from __future__ import annotations

from ..models import Project, Scene
from .canonical import AudioBible


def build_audio_bible(project: Project) -> AudioBible:
    voice_locks = {
        item.id: (
            f"VOICE-{item.id}; character={item.name}; keep one stable voice identity "
            "for every occurrence; preserve authored delivery channel per dialogue."
        )
        for item in project.characters
    }
    ambience_locks = {
        item.id: (
            f"AMBIENCE-{item.id}; location={item.name}; architecture={item.architecture}; "
            f"time={item.time_of_day}; weather={item.weather}; preserve acoustic identity."
        )
        for item in project.locations
    }
    dialogue_locks: dict[str, str] = {}
    for scene in project.scenes:
        for index, dialogue in enumerate(scene.dialogues, start=1):
            lock_id = dialogue_lock_id(scene, index)
            dialogue_locks[lock_id] = (
                f"{dialogue.character_id}|{dialogue.delivery}|{dialogue.text}"
            )
    return AudioBible(
        voice_locks=voice_locks,
        ambience_locks=ambience_locks,
        dialogue_locks=dialogue_locks,
    )


def dialogue_lock_id(scene: Scene, index: int) -> str:
    return f"DLG-{scene.id}-{index:02d}"


def scene_audio_locks(
    project: Project,
    scene: Scene,
    bible: AudioBible | None = None,
) -> dict[str, object]:
    bible = bible or build_audio_bible(project)
    referenced_voice_ids = set(scene.characters) | {
        dialogue.character_id for dialogue in scene.dialogues
    }
    voice_ids = sorted(
        character_id
        for character_id in referenced_voice_ids
        if character_id in bible.voice_locks
    )
    dialogue_ids = [
        dialogue_lock_id(scene, index)
        for index, _dialogue in enumerate(scene.dialogues, start=1)
    ]
    ambience_id = (
        scene.location_id if scene.location_id in bible.ambience_locks else ""
    )
    return {
        "voice_lock_ids": voice_ids,
        "voice_locks": {
            item_id: bible.voice_locks[item_id]
            for item_id in voice_ids
        },
        "ambience_lock_id": ambience_id,
        "ambience_lock": bible.ambience_locks.get(ambience_id, ""),
        "dialogue_lock_ids": dialogue_ids,
        "dialogue_locks": {
            item_id: bible.dialogue_locks[item_id]
            for item_id in dialogue_ids
            if item_id in bible.dialogue_locks
        },
        "target_lufs": bible.target_lufs,
        "true_peak_db": bible.true_peak_db,
        "loudness_tolerance_lu": bible.loudness_tolerance_lu,
        "no_unrequested_speech": not bool(scene.dialogues or scene.voiceover),
    }
