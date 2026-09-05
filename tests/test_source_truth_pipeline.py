from __future__ import annotations

from flow_story_studio.analysis_providers.merging import merge_analysis
from flow_story_studio.analysis_providers.prompting import merge_world
from flow_story_studio.engines.analyzer import analyze_story
from flow_story_studio.models import AnalyzeRequest, VideoSettings

SCREENPLAY = """
TÊN PHIM: TEST
THỜI LƯỢNG MỤC TIÊU: khoảng 3 phút

NHÂN VẬT CHÍNH
- NAM, nam 32 tuổi.
- LINH, nữ 29 tuổi.

ĐẠO CỤ CẦN GIỮ NHẤT QUÁN
- Điện thoại màu đen của Nam.
- Phong bì vàng.
- Khăn quàng cổ đỏ của Linh.

CẢNH 1 — CĂN HỘ CỦA NAM — ĐÊM — HIỆN TẠI
Nam nhìn điện thoại. Anh đứng dậy. Linh gọi điện cho Nam. Cô nói anh đừng ra ngoài.
Nam mở phong bì vàng rồi đặt điện thoại xuống bàn. Nam nhìn chiếc khăn quàng cổ đỏ.

CẢNH 2 — HÀNH LANG VÀ THANG MÁY CHUNG CƯ — ĐÊM — LIÊN TỤC
Nam bước ra hành lang. Anh vào thang máy. Nam nhìn phong bì trong tay.

CẢNH 3 — QUÁN CÀ PHÊ — ĐÊM — HIỆN TẠI
Nam bước vào quán cà phê. Linh không có ở đó. Chiếc khăn quàng cổ đỏ nằm trên ghế.
Nam đọc tin nhắn trên điện thoại rồi nhìn ra cửa kính.

CẢNH 4 — ĐƯỜNG PHỐ TRƯỚC QUÁN CÀ PHÊ — CHIỀU — FLASHBACK
Nam và Linh đứng ngoài đường. Linh đưa phong bì cho Nam. Nam cất nó vào túi.

CẢNH 5 — CĂN HỘ CỦA NAM — BÌNH MINH — KẾT
Nam trở về căn hộ. Anh đặt phong bì và điện thoại lên bàn. Linh gọi lại cho Nam.
"""


def _draft():
    return analyze_story(
        AnalyzeRequest(
            name="source truth",
            original_text=SCREENPLAY,
            settings=VideoSettings(scene_duration=8),
        )
    )


def test_runtime_entity_and_heading_source_truth() -> None:
    project = _draft()

    assert sum(scene.duration for scene in project.scenes) == 180
    assert 5 <= len(project.scenes) <= 23
    assert [item.name for item in project.characters] == ["NAM", "LINH"]
    assert all(item.name.casefold() not in {"anh", "cô"} for item in project.characters)
    prop_names = [item.name.casefold() for item in project.props]
    assert any("điện thoại" in name for name in prop_names)
    assert any("phong bì" in name for name in prop_names)
    assert any("khăn quàng" in name for name in prop_names)
    location_names = [item.name.casefold() for item in project.locations]
    assert any("hành lang" in name and "thang máy" in name for name in location_names)
    assert any("quán cà phê" in name for name in location_names)
    assert any("đường phố" in name for name in location_names)


def test_world_merge_drops_pronoun_aliases_and_dedupes_semantics() -> None:
    draft = _draft()
    current = {
        "story_bible": draft.story_bible.model_dump(),
        "characters": [item.model_dump() for item in draft.characters],
        "locations": [item.model_dump() for item in draft.locations],
        "props": [item.model_dump() for item in draft.props],
    }
    update = {
        "characters": [
            {"id": "CHAR_900", "name": "Anh"},
            {"id": "CHAR_901", "name": "Cô"},
            {"id": "CHAR_999", "name": "Nam", "face": "refined"},
        ],
        "props": [{"id": "PROP_999", "name": "Điện thoại"}],
        "locations": [{"id": "LOC_999", "name": "Quán cà phê"}],
    }

    merged = merge_world(current, update)

    character_names = [item["name"].casefold() for item in merged["characters"]]
    assert "anh" not in character_names
    assert "cô" not in character_names
    assert sum(name == "nam" for name in character_names) == 1
    assert sum("điện thoại" in item["name"].casefold() for item in merged["props"]) == 1
    assert sum(item["name"].casefold() == "quán cà phê" for item in merged["locations"]) == 1


def test_explicit_scene_heading_location_cannot_be_overridden_by_ai() -> None:
    draft = _draft()
    hallway_scene = next(
        scene for scene in draft.scenes if "HÀNH LANG VÀ THANG MÁY" in scene.source_text
    )
    cafe = next(item for item in draft.locations if "quán cà phê" in item.name.casefold())
    expected_location = hallway_scene.location_id

    data = {
        "characters": [item.model_dump() for item in draft.characters],
        "locations": [item.model_dump() for item in draft.locations],
        "props": [item.model_dump() for item in draft.props],
        "scenes": [
            {
                "id": hallway_scene.id,
                "summary": "Nam đi trong hành lang và thang máy.",
                "characters": [draft.characters[0].id],
                "location_id": cafe.id,
                "action": "Nam bước qua hành lang vào thang máy.",
                "camera": "tracking shot",
                "lighting": "dim corridor light",
                "atmosphere": "tense",
            }
        ],
    }

    merged = merge_analysis(draft, data, "test-model")
    result = next(scene for scene in merged.scenes if scene.id == hallway_scene.id)

    assert result.location_id == expected_location


def test_standard_screenplay_without_character_section_discovers_speakers() -> None:
    screenplay = """
TARGET RUNTIME: 1 minute

INT. APARTMENT - NIGHT
JOHN
I heard the phone ring.

MARY (V.O.)
Do not leave the apartment.

EXT. STREET - NIGHT
JOHN runs into the rain and sees MARY waiting beside a taxi.
MARY
You came anyway.
"""
    project = analyze_story(
        AnalyzeRequest(
            name="standard screenplay",
            original_text=screenplay,
            settings=VideoSettings(scene_duration=8),
        )
    )

    names = {item.name.casefold() for item in project.characters}
    assert "john" in names
    assert "mary" in names
    assert "nhân vật chính" not in names
    assert sum(scene.duration for scene in project.scenes) == 60
    location_names = {item.name.casefold() for item in project.locations}
    assert any("apartment" in name for name in location_names)
    assert any("street" in name for name in location_names)
