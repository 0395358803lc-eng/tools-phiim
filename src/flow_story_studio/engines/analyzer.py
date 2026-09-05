"""Offline story analysis pipeline.

This engine intentionally uses deterministic rules so the application works without
credentials. The public service boundary can later be backed by an LLM analyzer.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from collections import Counter
from copy import deepcopy

from ..analysis_providers.audio_finalization import finalize_audio
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
from ..visual_bible import build_visual_bible
from .continuity import check_project
from .prompt_generator import global_visual_style, make_flow_prompt, make_visual_prompt
from .segmenter import (
    SCENE_CONTEXT_PREFIX,
    SCENE_CONTEXT_SUFFIX,
    allocate_scene_durations,
    segment_story,
    target_runtime_seconds,
)

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
    "đường phố": "Đường phố",
    "khu rừng": "Khu rừng",
    "bãi biển": "Bãi biển",
    "ngôi nhà": "Ngôi nhà",
    "căn hộ": "Căn hộ",
    "căn phòng": "Căn hộ",
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

GENERIC_REFERENCE_NAMES = {
    "anh",
    "chị",
    "cô",
    "chú",
    "bác",
    "ông",
    "bà",
    "em",
    "hắn",
    "cậu",
    "nàng",
    "tôi",
    "ta",
    "mình",
    "người đàn ông",
    "người phụ nữ",
    "the man",
    "the woman",
    "he",
    "she",
    "him",
    "her",
    "they",
    "them",
    "you",
    "i",
    "we",
    "narrator",
}

CHARACTER_SECTION_RE = re.compile(r"\b(nhân\s*vật|characters?|cast)\b", re.IGNORECASE)
PROP_SECTION_RE = re.compile(r"\b(đạo\s*cụ|props?|objects?)\b", re.IGNORECASE)
SCENE_LOCATION_RE = re.compile(
    r"^(?:#+\s*)?(?:cảnh|scene)\s*\d+\s*[—–:-]+\s*(?P<location>.+?)(?:\s*[—–|-]+\s*(?:đêm|ngày|sáng|chiều|tối|night|day|morning|evening|dawn|dusk)\b|$)",
    re.IGNORECASE,
)
INT_EXT_LOCATION_RE = re.compile(
    r"^(?:#+\s*)?(?:INT\.?|EXT\.?|INT\./EXT\.?)\s+(?P<location>.+?)(?:\s+-\s+(?:DAY|NIGHT|MORNING|EVENING|DAWN|DUSK)\b|$)",
    re.IGNORECASE,
)


def _plain_line(raw: str) -> str:
    value = re.sub(r"^#{1,6}\s+", "", raw.strip())
    return re.sub(r"\*\*|__|`", "", value).strip()


def _declared_records(
    text: str,
    section_re: re.Pattern[str],
) -> list[tuple[str, str]]:
    lines = re.sub(r"\r\n?", "\n", text).splitlines()
    active = False
    values: list[tuple[str, str]] = []
    section_headers = re.compile(
        r"^(?:nhân\s*vật(?:\s*chính)?|characters?|cast|đạo\s*cụ(?:\s*continuity|\s*cần.*)?|props?|objects?)\s*:?.*$",
        re.IGNORECASE,
    )
    for raw in lines:
        plain = _plain_line(raw)
        if not plain:
            continue
        is_heading = bool(re.match(r"^#{1,6}\s+", raw.strip()))
        if section_headers.match(plain):
            active = bool(section_re.search(plain))
            continue
        if active and (
            is_heading
            or re.match(r"^(?:cảnh|scene)\s*\d+", plain, re.I)
            or re.match(r"^(?:thể\s*loại|genre|thời\s*lượng|runtime|duration)\s*:", plain, re.I)
        ):
            active = False
        if not active:
            continue
        match = re.match(r"^[-*+]\s*(.+)$", raw.strip())
        candidate = _plain_line(match.group(1)) if match else plain
        if not candidate or not re.search(r"[\wÀ-ỹ]", candidate, re.UNICODE):
            continue
        name = re.split(r"[,;:–—]", candidate, maxsplit=1)[0].strip().rstrip(".。")
        if not name or not re.search(r"[\wÀ-ỹ]", name, re.UNICODE):
            continue
        if not 1 <= len(name.split()) <= 8:
            continue
        details = candidate[len(name) :].lstrip(" ,;:–—-").strip()
        if all(existing_name != name for existing_name, _ in values):
            values.append((name, details))
    return values


def _declared_entries(
    text: str,
    section_re: re.Pattern[str],
) -> list[tuple[str, str]]:
    """Return declared entity names together with authored source details."""
    lines = re.sub(r"\r\n?", "\n", text).splitlines()
    active = False
    values: list[tuple[str, str]] = []
    seen: set[str] = set()
    section_headers = re.compile(
        r"^(?:nhân\s*vật(?:\s*chính)?|characters?|cast|"
        r"đạo\s*cụ(?:\s*continuity|\s*cần.*)?|props?|objects?)\s*:?.*$",
        re.IGNORECASE,
    )
    for raw in lines:
        plain = _plain_line(raw)
        if not plain:
            continue
        is_heading = bool(re.match(r"^#{1,6}\s+", raw.strip()))
        if section_headers.match(plain):
            active = bool(section_re.search(plain))
            continue
        if active and (
            is_heading
            or re.match(r"^(?:cảnh|scene)\s*\d+", plain, re.I)
            or re.match(
                r"^(?:thể\s*loại|genre|thời\s*lượng|runtime|duration)\s*:",
                plain,
                re.I,
            )
        ):
            active = False
        if not active:
            continue
        match = re.match(r"^[-*+]\s*(.+)$", raw.strip())
        authored = _plain_line(match.group(1)) if match else plain
        if not authored or not re.search(r"[\wÀ-ỹ]", authored, re.UNICODE):
            continue
        parts = re.split(r"[,;:–—]", authored, maxsplit=1)
        name = parts[0].strip().rstrip(".。")
        details = parts[1].strip() if len(parts) > 1 else ""
        if not name or not re.search(r"[\wÀ-ỹ]", name, re.UNICODE):
            continue
        key = name.casefold()
        if 1 <= len(name.split()) <= 8 and key not in seen:
            values.append((name, details))
            seen.add(key)
    return values


def _declared_items(text: str, section_re: re.Pattern[str]) -> list[str]:
    return [name for name, _ in _declared_entries(text, section_re)]


def _declared_characters(text: str) -> list[str]:
    return _declared_items(text, CHARACTER_SECTION_RE)

def _standalone_speakers(text: str) -> list[str]:
    """Recognize conventional screenplay dialogue labels such as JOHN or MARIA (V.O.)."""
    lines = re.sub(r"\r\n?", "\n", text).splitlines()
    blocked = {
        "day",
        "night",
        "morning",
        "evening",
        "dawn",
        "dusk",
        "continuous",
        "present",
        "flashback",
        "end",
        "fade in",
        "fade out",
        "cut to",
        "ngày",
        "đêm",
        "sáng",
        "chiều",
        "tối",
        "liên tục",
        "hiện tại",
        "trở lại hiện tại",
        "kết",
        "nhân vật",
        "nhân vật chính",
        "đạo cụ",
        "characters",
        "character",
        "cast",
        "props",
        "prop",
        "objects",
        "object",
        "đang",
        "dang",
        "nhân viên",
        "nhan vien",
        "employee",
        "staff",
    }
    values: list[str] = []
    for index, raw in enumerate(lines):
        plain = _plain_line(raw)
        if not plain or len(plain) > 40 or ":" in plain:
            continue
        label = re.sub(r"\s*\((?:V\.?O\.?|O\.?S\.?|OFF)\)\s*$", "", plain, flags=re.I)
        if not re.fullmatch(r"[A-ZÀ-Ỹ][A-ZÀ-Ỹ .'-]{1,30}", label):
            continue
        key = label.casefold().strip()
        if key in blocked or key in GENERIC_REFERENCE_NAMES:
            continue
        if re.match(r"^(?:INT\.?|EXT\.?|INT\./EXT\.?|SCENE\b|CẢNH\b)", label, re.I):
            continue
        if len(label.split()) > 4:
            continue
        next_line = ""
        for future in lines[index + 1 :]:
            next_line = _plain_line(future)
            if next_line:
                break
        if not next_line:
            continue
        if re.match(r"^(?:INT\.?|EXT\.?|INT\./EXT\.?|SCENE\b|CẢNH\b)", next_line, re.I):
            continue
        if key not in {item.casefold() for item in values}:
            values.append(label.strip())
    return values


def _declared_props(text: str) -> list[str]:
    return _declared_items(text, PROP_SECTION_RE)


def _heading_locations(text: str) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for raw in re.sub(r"\r\n?", "\n", text).splitlines():
        plain = _plain_line(raw)
        name = ""
        scene_match = re.match(r"^(?:cảnh|scene)\s*\d+\s*[—–:-]+\s*(.+)$", plain, re.IGNORECASE)
        if scene_match:
            remainder = scene_match.group(1).strip()
            parts = [
                part.strip() for part in re.split(r"\s+[—–]\s+|\s+-\s+", remainder) if part.strip()
            ]
            if parts:
                name = parts[0]
        else:
            match = INT_EXT_LOCATION_RE.match(plain)
            if match:
                name = match.group("location").strip(" -—–|")
        if name:
            lowered_name = name.casefold()
            context_only = {
                "liên tục",
                "continuous",
                "hiện tại",
                "present",
                "flashback",
                "trở lại hiện tại",
                "return to present",
                "kết",
                "end",
            }
            if lowered_name in context_only:
                continue
            exact_match = next(
                (
                    canonical
                    for hint, canonical in LOCATION_HINTS.items()
                    if lowered_name == hint.casefold()
                ),
                None,
            )
            if exact_match is not None:
                name = exact_match
            else:
                prefix = next(
                    (
                        (hint, canonical)
                        for hint, canonical in LOCATION_HINTS.items()
                        if lowered_name.startswith(hint.casefold() + " ")
                    ),
                    None,
                )
                if prefix is not None:
                    hint, canonical = prefix
                    suffix = name[len(hint) :].strip()
                    name = f"{canonical} {suffix}".strip()
                # For contained hints such as "PHÒNG TRỰC NHÀ GA" or
                # "BÊN NGOÀI NHÀ GA", preserve the authored heading verbatim.
        key = name.casefold()
        if name and key not in seen:
            values.append(name)
            seen.add(key)
    return values


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


def _character_from_declared(index: int, name: str, details: str) -> Character:
    folded = details.casefold()
    gender = (
        "Nữ"
        if re.search(r"(?<!\w)(?:nữ|female|woman)(?!\w)", folded)
        else "Nam"
        if re.search(r"(?<!\w)(?:nam|male|man)(?!\w)", folded)
        else "Không xác định"
    )
    age_match = re.search(r"\b(\d{1,3})\s*(?:tuổi|years?\s+old)\b", details, re.IGNORECASE)
    age = f"{age_match.group(1)} tuổi" if age_match else "Không xác định"

    fragments = [
        item.strip().rstrip(".")
        for item in re.split(r"[,;]", details)
        if item.strip()
    ]
    clothing_markers = (
        "áo ",
        "quần ",
        "váy ",
        "đầm ",
        "shirt",
        "jacket",
        "coat",
        "pants",
        "trousers",
        "sweater",
        "dress",
        "uniform",
    )
    hair_markers = ("tóc", "hair")
    accessory_markers = ("đồng hồ", "kính", "mũ", "watch", "glasses", "hat", "necklace")
    clothing = ", ".join(
        item for item in fragments if any(marker in item.casefold() for marker in clothing_markers)
    )
    hairstyle = next(
        (item for item in fragments if any(marker in item.casefold() for marker in hair_markers)),
        "Theo mô tả gốc",
    )
    accessories = ", ".join(
        item for item in fragments if any(marker in item.casefold() for marker in accessory_markers)
    )
    source_lock = details.strip() or "Theo nội dung gốc"

    return Character(
        id=f"CHAR_{index:03d}",
        name=name,
        gender=gender,
        estimated_age=age,
        clothing=clothing or "Trang phục phù hợp bối cảnh, giữ nguyên cho đến khi có thay đổi",
        hairstyle=hairstyle,
        accessories=accessories or "Không có nếu không được nêu",
        identifying_features=f"Source profile lock: {source_lock}",
    )


def _characters(text: str) -> list[Character]:
    entries = _declared_entries(text, CHARACTER_SECTION_RE)
    declared = [name for name, _ in entries]
    source_details = {name.casefold(): details for name, details in entries}
    speakers = _standalone_speakers(text) if not declared else []
    declared_keys = {name.casefold() for name in declared}
    found: list[tuple[str, str]] = [(name, "Không xác định") for name in declared]
    found.extend(
        (name, "Không xác định") for name in speakers if name.casefold() not in declared_keys
    )
    lowered = text.lower()
    if not declared:
        for hint, identity in GENERIC_CHARACTERS.items():
            if hint in lowered and identity[0].casefold() not in GENERIC_REFERENCE_NAMES:
                found.append(identity)

    quoted_speakers = re.findall(
        r"\b([A-ZÀ-Ỹ][a-zà-ỹ]{1,24})\s+(?:nói|hỏi|đáp|thì thầm|kêu|said|asked)\b",
        text,
    )
    blocked = {"sau", "khi", "trong", "một", "ngày", "cuối", "đột", "nhưng", "và"}
    for name in quoted_speakers if not declared else []:
        key = name.casefold()
        if key in blocked:
            continue
        if key in GENERIC_REFERENCE_NAMES and key not in declared_keys:
            continue
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

    characters: list[Character] = []
    for index, (name, fallback_gender) in enumerate(unique[:24], 1):
        details = source_details.get(name.casefold(), "").strip()
        folded = details.casefold()
        gender = fallback_gender
        if re.search(r"(?<!\w)(?:nam|male|man)(?!\w)", folded, re.UNICODE):
            gender = "Nam"
        elif re.search(r"(?<!\w)(?:nữ|nu|female|woman)(?!\w)", folded, re.UNICODE):
            gender = "Nữ"

        age_match = re.search(r"\b(\d{1,3})\s*(?:tuổi|years?\s*old|yo)\b", folded)
        estimated_age = (
            f"{age_match.group(1)} tuổi" if age_match else "Không xác định"
        )

        authored_appearance = details
        authored_appearance = re.sub(
            r"^(?:nam|nữ|nu|male|female|adult\s+man|adult\s+woman)\s*,?\s*",
            "",
            authored_appearance,
            flags=re.IGNORECASE,
        )
        authored_appearance = re.sub(
            r"^\d{1,3}\s*(?:tuổi|years?\s*old|yo)\s*[.,]?\s*",
            "",
            authored_appearance,
            flags=re.IGNORECASE,
        )
        authored_appearance = authored_appearance.strip(" .,;")
        clothing = (
            authored_appearance
            or "Trang phục phù hợp bối cảnh, giữ nguyên cho đến khi có thay đổi"
        )
        hair_match = re.search(
            r"(?<!\w)(tóc\s+[^,.;]+)",
            details,
            flags=re.IGNORECASE | re.UNICODE,
        )
        hairstyle = hair_match.group(1).strip() if hair_match else "Theo mô tả gốc"

        characters.append(
            Character(
                id=f"CHAR_{index:03d}",
                name=name,
                gender=gender,
                estimated_age=estimated_age,
                clothing=clothing,
                hairstyle=hairstyle,
            )
        )
    return characters

def _locations(text: str) -> list[Location]:
    lowered = text.lower()
    names = _heading_locations(text)
    matches: list[tuple[int, str]] = []
    for hint, name in LOCATION_HINTS.items():
        position = lowered.find(hint)
        if position >= 0:
            matches.append((position, name))
    for _, name in sorted(matches, key=lambda item: item[0]):
        key = name.casefold()
        existing = [item.casefold() for item in names]
        if any(key in item or item in key for item in existing):
            continue
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
        for index, name in enumerate(names[:24], 1)
    ]


def _props(text: str) -> list[Prop]:
    entries = _declared_entries(text, PROP_SECTION_RE)
    if not entries:
        lowered = text.lower()
        names: list[str] = []
        for hint, name in PROP_HINTS.items():
            if hint not in lowered:
                continue
            key = name.casefold()
            existing = [item.casefold() for item in names]
            if any(key in item or item in key for item in existing):
                continue
            names.append(name)
        entries = [(name, "") for name in names]

    props: list[Prop] = []
    for index, (name, details) in enumerate(entries[:24], 1):
        source_details = details.strip(" .")
        description = (
            f"{name}: {source_details}"
            if source_details
            else f"{name} xuất hiện trong nội dung gốc"
        )
        state = f"Theo source: {source_details}" if source_details else "Nguyên vẹn"
        props.append(
            Prop(
                id=f"PROP_{index:03d}",
                name=name,
                description=description,
                state=state,
            )
        )
    return props

def _semantic_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value).casefold().replace("đ", "d"))
    ascii_text = "".join(char for char in normalized if not unicodedata.combining(char))
    return " ".join(re.findall(r"[a-z0-9]+", ascii_text))


def _entity_mentioned(source_text: str, entity_name: str) -> bool:
    source_tokens = set(_semantic_key(source_text).split())
    name_tokens = [
        token
        for token in _semantic_key(entity_name).split()
        if token not in {"cua", "the", "and", "mau", "color", "colour", "giay", "paper"}
        and len(token) >= 2
    ]
    if not source_tokens or not name_tokens:
        return False
    if " ".join(name_tokens) in " ".join(source_tokens):
        return True
    overlap = sum(token in source_tokens for token in name_tokens)
    return overlap >= (1 if len(name_tokens) == 1 else 2)


def _ids_mentioned(text: str, characters: list[Character]) -> list[str]:
    lowered = text.lower()
    matches = [item.id for item in characters if item.name.lower() in lowered]
    return matches or ([characters[0].id] if characters else [])


def _explicit_location_for(text: str, locations: list[Location]) -> str | None:
    lowered = text.lower()
    by_name = {location.name: location.id for location in locations}
    candidates: list[tuple[int, str]] = []
    for hint, name in LOCATION_HINTS.items():
        position = lowered.find(hint)
        if position >= 0 and name in by_name:
            candidates.append((position, by_name[name]))
    for location in locations:
        position = lowered.find(location.name.lower())
        if position >= 0:
            candidates.append((position, location.id))
    if not candidates:
        return None
    # At the same textual position prefer the most specific canonical location name.
    names_by_id = {location.id: location.name for location in locations}
    return min(candidates, key=lambda item: (item[0], -len(names_by_id.get(item[1], ""))))[1]


def _location_for(text: str, locations: list[Location], previous: str | None) -> str:
    return _explicit_location_for(text, locations) or previous or locations[0].id


def _scene_context_heading(text: str) -> str:
    stripped = text.lstrip()
    if not stripped.startswith(SCENE_CONTEXT_PREFIX):
        return ""
    context = stripped[len(SCENE_CONTEXT_PREFIX) :]
    if SCENE_CONTEXT_SUFFIX in context:
        context = context.split(SCENE_CONTEXT_SUFFIX, 1)[0]
    return context.strip()


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
    runtime_seconds = target_runtime_seconds(request.original_text)
    chunks = segment_story(request.original_text, request.settings.scene_duration)
    scene_durations = allocate_scene_durations(
        chunks, runtime_seconds, request.settings.scene_duration
    )
    scenes: list[Scene] = []
    previous_location: str | None = None
    previous_end: ContinuityState | None = None
    has_scene_context = any(chunk.lstrip().startswith(SCENE_CONTEXT_PREFIX) for chunk in chunks)
    return_location_stack: list[str] = []

    for index, chunk in enumerate(chunks, 1):
        char_ids = _ids_mentioned(chunk, characters)
        prop_positions = {
            item.id: item.initial_location for item in props if _entity_mentioned(chunk, item.name)
        }
        is_scene_cut = chunk.lstrip().startswith(SCENE_CONTEXT_PREFIX)
        if has_scene_context:
            if is_scene_cut:
                context_heading = _scene_context_heading(chunk)
                context_lower = context_heading.casefold()
                context_location = _explicit_location_for(context_heading, locations)
                if "trở lại hiện tại" in context_lower and return_location_stack:
                    location_id = return_location_stack.pop()
                    context_location = _explicit_location_for(context_heading, locations)
                    if context_location is not None:
                        location_id = context_location
                elif "flashback" in context_lower:
                    if previous_location is not None:
                        return_location_stack.append(previous_location)
                    body = (
                        chunk.split(SCENE_CONTEXT_SUFFIX, 1)[1]
                        if SCENE_CONTEXT_SUFFIX in chunk
                        else ""
                    )
                    location_id = (
                        context_location
                        or _explicit_location_for(body, locations)
                        or previous_location
                        or locations[0].id
                    )
                else:
                    location_id = context_location or previous_location or locations[0].id
            else:
                location_id = previous_location or locations[0].id
        else:
            location_id = _location_for(chunk, locations, previous_location)
        can_chain = (
            previous_end is not None and previous_location == location_id and not is_scene_cut
        )
        previous_location = location_id
        location = next(item for item in locations if item.id == location_id)
        duration = scene_durations[index - 1]
        if can_chain:
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
            voiceover="",
            dialogues=[],
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
    project = check_project(project, auto_fix=request.settings.auto_continuity)
    project = finalize_audio(project)
    project = build_visual_bible(project)
    location_by_id = {item.id: item for item in project.locations}
    for index, scene in enumerate(project.scenes):
        visible_characters = [item for item in project.characters if item.id in scene.characters]
        scene.flow_prompt = make_flow_prompt(
            scene,
            characters=visible_characters,
            location=location_by_id[scene.location_id],
            visual_style=project.visual_style,
            all_characters=project.characters,
            previous_scene_id=project.scenes[index - 1].id if index else None,
        )
    return project
