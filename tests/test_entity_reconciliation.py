from flow_story_studio.analysis_providers.merging import merge_analysis
from flow_story_studio.engines.analyzer import analyze_story
from flow_story_studio.models import AnalyzeRequest, Character, Prop, VideoSettings

SCREENPLAY = """## KỊCH BẢN CHI TIẾT
### CẢNH 1 – CĂN HỘ – ĐÊM
Minh đứng trong căn hộ và nhìn điện thoại.

### CẢNH 2 – QUÁN CÀ PHÊ – CHIỀU – MỘT NĂM TRƯỚC
Lan ngồi đối diện Minh trong quán cà phê.

### FLASHBACK
Đường phố ban đêm. Minh chạy giữa mưa.
"""


def _draft():
    project = analyze_story(
        AnalyzeRequest(name="Entity mapping", original_text=SCREENPLAY, settings=VideoSettings())
    )
    project.characters = [
        Character(id="CHAR_001", name="Minh"),
        Character(id="CHAR_002", name="Lan"),
    ]
    project.props = [Prop(id="PROP_001", name="Điện thoại", description="Điện thoại của Minh")]
    return project


def _location_id(project, name: str) -> str:
    return next(item.id for item in project.locations if item.name == name)


def test_ai_location_ids_are_remapped_by_semantic_identity() -> None:
    draft = _draft()
    apartment_id = _location_id(draft, "Căn hộ")
    cafe_id = _location_id(draft, "Quán cà phê")
    street_id = _location_id(draft, "Đường phố")
    cafe_scene = next(scene for scene in draft.scenes if scene.location_id == cafe_id)

    locations_by_name = {item.name: item for item in draft.locations}
    ai_locations = [
        locations_by_name["Căn hộ"]
        .model_copy(update={"id": "LOC_001", "name": "Căn hộ của Minh"})
        .model_dump(),
        locations_by_name["Đường phố"]
        .model_copy(update={"id": "LOC_004", "name": "Đường phố hiện trường tai nạn"})
        .model_dump(),
        locations_by_name["Quán cà phê"]
        .model_copy(update={"id": "LOC_003", "name": "Quán cà phê (hồi tưởng)"})
        .model_dump(),
    ]
    data = {
        "locations": ai_locations,
        "scenes": [{"id": cafe_scene.id, "location_id": "LOC_003"}],
    }

    merged = merge_analysis(draft, data, "test-model")
    merged_names = {item.id: item.name for item in merged.locations}
    merged_scene = next(scene for scene in merged.scenes if scene.id == cafe_scene.id)

    assert merged_scene.location_id == cafe_id
    assert "Quán cà phê" in merged_names[cafe_id]
    assert "tai nạn" not in merged_names[cafe_id]
    assert apartment_id in merged_names
    assert street_id in merged_names


def test_character_prop_and_state_references_are_remapped() -> None:
    draft = _draft()
    scene = draft.scenes[0]
    location_id = scene.location_id
    ai_characters = [
        draft.characters[1].model_copy(update={"id": "CHAR_001"}).model_dump(),
        draft.characters[0].model_copy(update={"id": "CHAR_002"}).model_dump(),
    ]
    ai_props = [
        draft.props[0].model_copy(update={"id": "PROP_009"}).model_dump(),
    ]
    data = {
        "characters": ai_characters,
        "props": ai_props,
        "scenes": [
            {
                "id": scene.id,
                "characters": ["CHAR_002", "CHAR_001"],
                "location_id": location_id,
                "dialogues": [{"character_id": "Lan", "text": "Anh vẫn thức à?", "emotion": "nhẹ"}],
                "start_state": {
                    "character_positions": {"Minh": "bàn", "Lan": "đầu dây"},
                    "character_wardrobe": {"Minh": "áo tối", "Lan": "áo sáng"},
                    "prop_positions": {"Điện thoại": "trên bàn", "Bàn": "giữa phòng"},
                    "time": "đêm",
                    "weather": "mưa",
                    "camera": "wide",
                    "notes": "",
                },
                "end_state": {
                    "character_positions": {"Minh": "cửa sổ", "Lan": "đầu dây"},
                    "character_wardrobe": {"Minh": "áo tối", "Lan": "áo sáng"},
                    "prop_positions": {"Điện thoại": "trong tay Minh", "Bàn": "giữa phòng"},
                    "time": "đêm",
                    "weather": "mưa",
                    "camera": "medium",
                    "notes": "",
                },
            }
        ],
    }

    merged = merge_analysis(draft, data, "test-model")
    merged_scene = merged.scenes[0]

    assert merged_scene.characters == ["CHAR_001"]
    assert merged_scene.dialogues == []
    assert set(merged_scene.start_state.character_positions) == {"CHAR_001"}
    assert set(merged_scene.start_state.character_wardrobe) == {"CHAR_001"}
    assert set(merged_scene.start_state.prop_positions) == {"PROP_001"}
    assert set(merged_scene.end_state.prop_positions) == {"PROP_001"}
    assert "Điện thoại" in merged_scene.start_state.prop_positions["PROP_001"]
    assert "Điện thoại" in merged_scene.end_state.prop_positions["PROP_001"]


def test_colliding_location_id_cannot_hijack_original_semantic_identity() -> None:
    draft = _draft()
    cafe_id = _location_id(draft, "Quán cà phê")
    cafe_scene = next(scene for scene in draft.scenes if scene.location_id == cafe_id)
    cafe = next(item for item in draft.locations if item.id == cafe_id)

    hijacked = cafe.model_copy(
        update={
            "id": cafe_id,
            "name": "Hiện trường tai nạn",
            "place_type": "Ngã tư đường phố",
            "architecture": "Mặt đường, xe cứu hộ và rào chắn",
        }
    ).model_dump()
    data = {
        "locations": [hijacked],
        "scenes": [{"id": cafe_scene.id, "location_id": cafe_id}],
    }

    merged = merge_analysis(draft, data, "test-model")
    merged_cafe = next(item for item in merged.locations if item.id == cafe_id)
    merged_scene = next(scene for scene in merged.scenes if scene.id == cafe_scene.id)

    assert "Quán cà phê" in merged_cafe.name
    assert "tai nạn" not in merged_cafe.name.casefold()
    assert merged_scene.location_id == cafe_id
    assert all(item.name != "Hiện trường tai nạn" for item in merged.locations)


def test_colliding_character_and_prop_ids_cannot_hijack_original_semantics() -> None:
    draft = _draft()
    scene = draft.scenes[0]
    minh = next(item for item in draft.characters if item.id == "CHAR_001")
    phone = next(item for item in draft.props if item.id == "PROP_001")

    data = {
        "characters": [
            minh.model_copy(
                update={
                    "id": "CHAR_001",
                    "name": "Bác sĩ cấp cứu",
                    "identifying_features": "Áo blouse và bảng tên bệnh viện",
                }
            ).model_dump()
        ],
        "props": [
            phone.model_copy(
                update={
                    "id": "PROP_001",
                    "name": "Biển cảnh báo giao thông",
                    "description": "Biển cảnh báo đặt tại hiện trường",
                }
            ).model_dump()
        ],
        "scenes": [
            {
                "id": scene.id,
                "characters": ["CHAR_001"],
                "location_id": scene.location_id,
                "start_state": {
                    "character_positions": {"CHAR_001": "bên bàn"},
                    "prop_positions": {"PROP_001": "trên bàn"},
                },
            }
        ],
    }

    merged = merge_analysis(draft, data, "test-model")
    merged_minh = next(item for item in merged.characters if item.id == "CHAR_001")
    merged_phone = next(item for item in merged.props if item.id == "PROP_001")
    merged_scene = merged.scenes[0]

    assert merged_minh.name == "Minh"
    assert merged_phone.name == "Điện thoại"
    assert merged_scene.characters == ["CHAR_001"]
    assert merged_scene.start_state.character_positions == {"CHAR_001": "bên bàn"}
    assert set(merged_scene.start_state.prop_positions) == {"PROP_001"}
    assert "Điện thoại" in merged_scene.start_state.prop_positions["PROP_001"]
    assert all(item.name != "Bác sĩ cấp cứu" for item in merged.characters)
    assert all(item.name != "Biển cảnh báo giao thông" for item in merged.props)
