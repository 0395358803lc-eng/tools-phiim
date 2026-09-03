"""Offline story analysis pipeline.

This engine intentionally uses deterministic rules so the application works without
credentials. The public service boundary can later be backed by an LLM analyzer.
"""

from __future__ import annotations

import re
import uuid
from collections import Counter
from copy import deepcopy

from ..models import (
    AnalyzeRequest,
    Character,
    ContinuityState,
    Dialogue,
    Location,
    Project,
    Prop,
    Scene,
    StoryBible,
)
from .continuity import check_project
from .prompt_generator import global_visual_style, make_flow_prompt, make_visual_prompt
from .segmenter import segment_story, speaking_duration

GENRE_HINTS = {
    "kinh dị": ("Kinh dị", "Căng thẳng, bí ẩn"),
    "ma ": ("Kinh dị", "U tối, hồi hộp"),
    "tình yêu": ("Tình cảm", "Ấm áp, giàu cảm xúc"),
    "quảng cáo": ("Quảng cáo", "Tinh gọn, thuyết phục"),
    "sản phẩm": ("Quảng cáo sản phẩm", "Hiện đại, giàu năng lượng"),
    "review": ("Review", "Tin cậy, trực quan"),
    "hài": ("Hài", "Vui nhộn, linh hoạt"),
    "documentary": ("Tài liệu", "Chân thực, quan sát"),
}

LOCATION_HINTS = {
    "cửa hàng": "Cửa hàng",
    "phòng khách": "Phòng khách",
    "phòng ngủ": "Phòng ngủ",
    "nhà bếp": "Nhà bếp",
    "văn phòng": "Văn phòng",
    "trường học": "Trường học",
    "bệnh viện": "Bệnh viện",
    "nhà ga": "Nhà ga",
    "sân bay": "Sân bay",
    "quán cà phê": "Quán cà phê",
    "quán cafe": "Quán cà phê",
    "nhà hàng": "Nhà hàng",
    "con đường": "Đường phố",
    "ngoài đường": "Đường phố",
    "khu rừng": "Khu rừng",
    "bãi biển": "Bãi biển",
    "ngôi nhà": "Ngôi nhà",
    "căn hộ": "Căn hộ",
}

PROP_HINTS = {
    "điện thoại": "Điện thoại",
    "iphone": "Điện thoại iPhone",
    "chìa khóa": "Chìa khóa",
    "chiếc cốc": "Chiếc cốc",
    "ly nước": "Ly nước",
    "cuốn sách": "Cuốn sách",
    "bức thư": "Bức thư",
    "máy tính": "Máy tính",
    "chiếc túi": "Chiếc túi",
    "sản phẩm": "Sản phẩm chính",
}

GENERIC_CHARACTERS = {
    "người đàn ông": ("Người đàn ông", "Nam"),
    "người phụ nữ": ("Người phụ nữ", "Nữ"),
    "cô gái": ("Cô gái", "Nữ"),
    "chàng trai": ("Chàng trai", "Nam"),
    "cậu bé": ("Cậu bé", "Nam"),
    "cô bé": ("Cô bé", "Nữ"),
    "nhân viên": ("Nhân viên", "Không xác định"),
    "khách hàng": ("Khách hàng", "Không xác định"),
}

CAMERA_SEQUENCE = (
    "Establishing wide shot, slow dolly forward, stable screen direction",
    "Medium tracking shot at eye level, natural parallax",
    "Over-the-shoulder medium close-up, subtle push-in",
    "Close-up reaction shot, restrained handheld micro-movement",
    "Wide master shot, smooth lateral tracking",
)


def _story_bible(text: str) -> StoryBible:
    lowered = text.lower()
    genre, mood = "Phim ngắn kể chuyện", "Điện ảnh, liền mạch, giàu cảm xúc"
    for hint, values in GENRE_HINTS.items():
        if hint in lowered:
            genre, mood = values
            break
    first = re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", text.strip()))[0]
    return StoryBible(
        main_theme=first[:220],
        genre=genre,
        purpose="Chuyển nội dung gốc thành chuỗi video ngắn nhất quán",
        audience="Người xem video ngắn và nội dung điện ảnh trực tuyến",
        mood=mood,
        synopsis=re.sub(r"\s+", " ", text.strip())[:650],
    )


def _characters(text: str) -> list[Character]:
    lowered = text.lower()
    found: list[tuple[str, str]] = []
    for hint, identity in GENERIC_CHARACTERS.items():
        if hint in lowered:
            found.append(identity)

    quoted_speakers = re.findall(
        r"\b([A-ZÀ-Ỹ][a-zà-ỹ]{1,24})\s+(?:nói|hỏi|đáp|thì thầm|kêu|said|asked)\b",
        text,
    )
    blocked = {"Sau", "Khi", "Trong", "Một", "Ngày", "Cuối", "Đột", "Nhưng", "Và"}
    for name in quoted_speakers:
        if name not in blocked:
            found.append((name, "Không xác định"))
    if not found:
        found.append(("Nhân vật chính", "Không xác định"))

    unique: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name, gender in found:
        key = name.casefold()
        if key not in seen:
            seen.add(key)
            unique.append((name, gender))
    return [
        Character(id=f"CHAR_{index:03d}", name=name, gender=gender)
        for index, (name, gender) in enumerate(unique[:12], 1)
    ]


def _locations(text: str) -> list[Location]:
    lowered = text.lower()
    names: list[str] = []
    for hint, name in LOCATION_HINTS.items():
        if hint in lowered and name not in names:
            names.append(name)
    if not names:
        names.append("Bối cảnh chính")
    return [
        Location(
            id=f"LOC_{index:03d}",
            name=name,
            place_type=name,
            architecture=f"Kiến trúc đặc trưng của {name}, giữ nguyên tuyệt đối",
            space=f"Bố cục {name} được thiết lập ở cảnh đầu và tái sử dụng",
            interior=f"Nội thất và vật liệu nhất quán của {name}",
        )
        for index, name in enumerate(names[:10], 1)
    ]


def _props(text: str) -> list[Prop]:
    lowered = text.lower()
    names: list[str] = []
    for hint, name in PROP_HINTS.items():
        if hint in lowered and name not in names:
            names.append(name)
    return [
        Prop(id=f"PROP_{index:03d}", name=name, description=f"{name} đúng như nội dung gốc")
        for index, name in enumerate(names[:12], 1)
    ]


def _ids_mentioned(text: str, characters: list[Character]) -> list[str]:
    lowered = text.lower()
    matches = [item.id for item in characters if item.name.lower() in lowered]
    return matches or ([characters[0].id] if characters else [])


def _location_for(text: str, locations: list[Location], previous: str | None) -> str:
    lowered = text.lower()
    by_name = {location.name: location.id for location in locations}
    for hint, name in LOCATION_HINTS.items():
        if hint in lowered and name in by_name:
            return by_name[name]
    for location in locations:
        if location.name.lower() in lowered:
            return location.id
    return previous or locations[0].id


def _dialogues(text: str, character_ids: list[str]) -> list[Dialogue]:
    quotes = re.findall(r"[\"“‘']([^\"”’']{2,220})[\"”’']", text)
    speaker = character_ids[0] if character_ids else "NARRATOR"
    return [Dialogue(character_id=speaker, text=item) for item in quotes]


def _action(text: str) -> str:
    cleaned = re.sub(r"[\"“”]", "", text).strip()
    return f"Diễn biến theo đúng trình tự: {cleaned}"


def analyze_story(request: AnalyzeRequest) -> Project:
    characters = _characters(request.original_text)
    locations = _locations(request.original_text)
    props = _props(request.original_text)
    story_bible = _story_bible(request.original_text)
    style = global_visual_style(request.settings)
    master_prompt = (
        f"STORY WORLD: {story_bible.synopsis}\nGENRE & MOOD: {story_bible.genre}; "
        f"{story_bible.mood}.\nVISUAL LANGUAGE: {style}\nCAMERA LANGUAGE: motivated cinematic "
        "coverage, preserve screen direction and spatial geography.\nCONTINUITY: exact "
        "identity, wardrobe, architecture, prop state, time, weather, lighting logic and "
        "causal action across every scene."
    )
    chunks = segment_story(request.original_text, request.settings.scene_duration)
    scenes: list[Scene] = []
    previous_location: str | None = None
    previous_end: ContinuityState | None = None
    prop_positions = {item.id: item.initial_location for item in props}

    for index, chunk in enumerate(chunks, 1):
        char_ids = _ids_mentioned(chunk, characters)
        location_id = _location_for(chunk, locations, previous_location)
        previous_location = location_id
        location = next(item for item in locations if item.id == location_id)
        duration = max(request.settings.scene_duration, speaking_duration(chunk))
        duration = min(30, duration)
        if previous_end:
            start_state = deepcopy(previous_end)
        else:
            start_state = ContinuityState(
                character_positions={item: f"trong {location.name}" for item in char_ids},
                character_wardrobe={
                    item.id: item.clothing for item in characters if item.id in char_ids
                },
                prop_positions=deepcopy(prop_positions),
                time="Mở đầu timeline",
                camera=CAMERA_SEQUENCE[0],
            )
        end_state = deepcopy(start_state)
        end_state.character_positions = {
            item: f"ở vị trí kết thúc hành động trong {location.name}" for item in char_ids
        }
        end_state.time = f"Sau {duration} giây kể từ đầu {index:03d}"
        end_state.camera = CAMERA_SEQUENCE[index % len(CAMERA_SEQUENCE)]
        end_state.notes = "Giữ làm tham chiếu trực tiếp cho cảnh kế tiếp."
        scene = Scene(
            id=f"SCENE_{index:03d}",
            order=index,
            title=f"Cảnh {index:03d}",
            source_text=chunk,
            summary=chunk[:160],
            characters=char_ids,
            location_id=location_id,
            action=_action(chunk),
            camera=CAMERA_SEQUENCE[(index - 1) % len(CAMERA_SEQUENCE)],
            lighting=location.lighting,
            atmosphere=story_bible.mood,
            duration=duration,
            visual_prompt="",
            flow_prompt="",
            voiceover=chunk,
            dialogues=_dialogues(chunk, char_ids),
            start_state=start_state,
            end_state=end_state,
            ai_locked=True,
            ai_lock_reason="Continuity Engine đã điền và khóa dữ liệu scene",
        )
        visible_characters = [item for item in characters if item.id in char_ids]
        scene.visual_prompt = make_visual_prompt(
            action=scene.action,
            characters=visible_characters,
            location=location,
            camera=scene.camera,
            lighting=scene.lighting,
            atmosphere=scene.atmosphere,
            style=style,
            start_state=scene.start_state,
            end_state=scene.end_state,
        )
        scene.flow_prompt = make_flow_prompt(
            scene,
            characters=visible_characters,
            location=location,
            visual_style=style,
            previous_scene_id=scenes[-1].id if scenes else None,
        )
        scenes.append(scene)
        previous_end = end_state

    word_counts = Counter(chunk.split()[0].lower() for chunk in chunks if chunk.split())
    timeline = [
        f"{scene.id}: {scene.summary} ({scene.duration}s, {scene.location_id})" for scene in scenes
    ]
    if word_counts:
        timeline.append(f"Nhịp kể được chia thành {len(scenes)} cảnh liên tục.")
    project = Project(
        id=uuid.uuid4().hex[:12],
        name=request.name,
        original_text=request.original_text,
        settings=request.settings,
        story_bible=story_bible,
        characters=characters,
        locations=locations,
        props=props,
        timeline=timeline,
        visual_style=style,
        master_prompt=master_prompt,
        scenes=scenes,
    )
    return check_project(project, auto_fix=request.settings.auto_continuity)
