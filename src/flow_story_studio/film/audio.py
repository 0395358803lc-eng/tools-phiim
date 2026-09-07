from __future__ import annotations

import re

from ..engines.continuity import is_direct_continuation
from ..models import Project, Scene
from .canonical import AudioBible


def _scene_body(source_text: str) -> str:
    return re.sub(
        r"\[SCENE CONTEXT\].*?\[END CONTEXT\]",
        " ",
        str(source_text),
        flags=re.DOTALL | re.IGNORECASE,
    ).strip()


def _source_sentences(source_text: str) -> list[str]:
    body = _scene_body(source_text)
    parts = re.split(r"(?<=[.!?…])\s+|\n+", body)
    return [" ".join(part.split()) for part in parts if part.strip()]


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _source_sound_cues(source_text: str) -> list[str]:
    markers = (
        "tiếng ",
        "âm thanh",
        "click",
        "rung",
        "bấm play",
        "nhạc",
        "giai điệu",
        "sound ",
        "audio ",
        "music",
        "ring",
        "buzz",
        "vibrat",
        "recording",
        "playback",
    )
    cues: list[str] = []
    for sentence in _source_sentences(source_text):
        folded = sentence.casefold()
        if any(marker in folded for marker in markers):
            cues.append(sentence)
    return _unique(cues)


def _diegetic_events(source_text: str) -> list[str]:
    raw = _scene_body(source_text).casefold()
    rules = (
        (r"\bclick\b", "small mechanical click"),
        (
            r"\bđiện thoại\b.{0,40}\brung\b|\brung\b.{0,40}\bđiện thoại\b|"
            r"\bphone\b.{0,40}\b(?:vibrat|buzz)",
            "phone vibration",
        ),
        (
            r"\bxé\b.{0,40}\b(?:giấy|vé)\b|\b(?:tear|rip)(?:s|ped)?\b.{0,40}"
            r"\b(?:paper|ticket)\b",
            "paper or ticket tearing",
        ),
        (
            r"\bđoàn tàu\b.{0,60}\b(?:chạy|đi)\s+qua\b|"
            r"\btrain\b.{0,60}\bpass(?:es|ed|ing)?\b",
            "passing train and rail rumble",
        ),
        (
            r"\bxe\b.{0,50}\bchạy ngang\b|\bcar\b.{0,50}\bpass(?:es|ed|ing)?\b",
            "passing vehicle",
        ),
        (
            r"\bmưa\b|\brain(?:ing)?\b",
            "rain ambience at the source-described intensity",
        ),
        (
            r"\bbấm\s+play\b|\bpress(?:es|ed)?\s+(?:the\s+)?play\b",
            "recorder PLAY control and playback start",
        ),
        (
            r"\btua\s+lại\b.{0,40}\b(?:bản ghi|máy ghi âm)\b|"
            r"\brew(?:ind|inds|ound)\b.{0,40}\b(?:recording|recorder)\b",
            "recorder rewind or scrub mechanism",
        ),
        (
            r"\bcửa\b.{0,20}\b(?:mở|đóng)\b|\bdoor\b.{0,20}\b(?:open|close)",
            "natural door movement when visible in the authored action",
        ),
    )
    return _unique([description for pattern, description in rules if re.search(pattern, raw)])


def _music_instruction(source_text: str) -> str:
    musical = [
        sentence
        for sentence in _source_sentences(source_text)
        if any(
            marker in sentence.casefold()
            for marker in ("nhạc", "giai điệu", "music", "score", "melody")
        )
    ]
    if musical:
        return "Source-mandated music cue(s): " + " | ".join(_unique(musical))
    return (
        "No source-mandated music. If the renderer adds score, keep it subtle, continuous "
        "with adjacent scenes, and subordinate to dialogue and diegetic sound."
    )


def _explicit_silence(source_text: str) -> bool:
    raw = _scene_body(source_text).casefold()
    return bool(
        re.search(
            r"\b(?:hoàn toàn im lặng|im lặng tuyệt đối|không có âm thanh|"
            r"absolute silence|completely silent|no sound)\b",
            raw,
        )
    )


def build_scene_audio_plan(
    project: Project,
    scene: Scene,
    *,
    dependency_mode: str = "canonical",
    previous_scene: Scene | None = None,
) -> dict[str, object]:
    location = next((item for item in project.locations if item.id == scene.location_id), None)
    if location is None:
        ambience = (
            "Maintain natural diegetic room tone appropriate to the source-defined location; "
            "do not introduce unrelated environmental audio."
        )
    else:
        ambience = (
            f"Natural diegetic ambience for {location.name}; preserve one stable acoustic "
            "identity for this location. "
            f"Architecture={location.architecture}; time={location.time_of_day}; "
            f"weather={location.weather}."
        )

    audio_continuous = (
        previous_scene is not None
        and is_direct_continuation(previous_scene, scene)
    )
    if dependency_mode == "direct" or audio_continuous:
        continuity = (
            "Direct audio continuation: carry the previous clip's ambience bed, acoustic "
            "perspective, background level and any still-active sound through the cut without "
            "an audible reset. Visual re-anchoring does not reset sound when time/location remain "
            "continuous."
        )
    else:
        continuity = (
            "New beat/cut: re-anchor audio to this scene's source location, time and weather. "
            "Do not carry unrelated sounds from the previous scene."
        )

    silence_required = _explicit_silence(scene.source_text)
    generation_instruction = (
        "Honor the source-mandated silence; do not invent speech, music, or effects."
        if silence_required
        else (
            "When the render provider supports audio, generate an audible natural sound bed "
            "for the scene instead of an unintentionally silent clip. Preserve exact dialogue "
            "delivery, add only source-grounded or physically motivated diegetic effects, "
            "and never invent speech."
        )
    )

    speech = [
        f"{item.character_id}|{item.delivery}|{item.text}"
        for item in scene.dialogues
    ]
    if scene.voiceover:
        speech.append(f"VOICEOVER|{scene.voiceover}")

    return {
        "ambience": ambience,
        "source_sound_cues": _source_sound_cues(scene.source_text),
        "diegetic_effects": _diegetic_events(scene.source_text),
        "music": _music_instruction(scene.source_text),
        "speech": speech,
        "silence_required": silence_required,
        "continuity": continuity,
        "generation_instruction": generation_instruction,
    }


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
            f"time={item.time_of_day}; weather={item.weather}; preserve one stable acoustic "
            "identity/room tone across every scene in this location and across direct cuts."
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
    *,
    dependency_mode: str = "canonical",
    previous_scene: Scene | None = None,
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
        "scene_audio_plan": build_scene_audio_plan(
            project,
            scene,
            dependency_mode=dependency_mode,
            previous_scene=previous_scene,
        ),
        "target_lufs": bible.target_lufs,
        "true_peak_db": bible.true_peak_db,
        "loudness_tolerance_lu": bible.loudness_tolerance_lu,
        "no_unrequested_speech": not bool(scene.dialogues or scene.voiceover),
    }
