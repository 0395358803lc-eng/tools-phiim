from flow_story_studio.engines.analyzer import analyze_story
from flow_story_studio.models import AnalyzeRequest, VideoSettings


def test_declared_world_is_authoritative_and_location_qualifiers_stay_distinct() -> None:
    script = """
TARGET RUNTIME: 32 seconds

CHARACTERS
- ALEX, adult man.
- MAYA, adult woman.

PROPS
- Blue ticket.
- Yellow umbrella.

SCENE 1 — ALEX'S APARTMENT — NIGHT
Alex sits alone. MAYA (THROUGH THE PHONE) says, "Stay home."

SCENE 2 — STATION CONTROL ROOM — NIGHT
Alex enters the control room. A staff member is mentioned in narration but is not a character.

SCENE 3 — MAYA'S APARTMENT — NIGHT
Maya stands by the window with the yellow umbrella near the door.

SCENE 4 — OUTSIDE THE STATION — NIGHT
Alex walks into the rain.
"""
    project = analyze_story(
        AnalyzeRequest(
            name="declared world authority",
            original_text=script,
            settings=VideoSettings(scene_duration=8),
        )
    )

    assert [item.name.casefold() for item in project.characters] == ["alex", "maya"]
    assert [item.name.casefold() for item in project.props] == ["blue ticket", "yellow umbrella"]

    locations = [item.name.casefold() for item in project.locations]
    assert "alex's apartment" in locations
    assert "maya's apartment" in locations
    assert "station control room" in locations
    assert "outside the station" in locations
    assert len(locations) >= 4


def test_vietnamese_declared_profiles_preserve_visual_source_locks() -> None:
    script = """
TARGET RUNTIME: 16 seconds

NHÂN VẬT CHÍNH
- KHẢI, nam, 35 tuổi. Áo sơ mi xám đậm, áo khoác đen, quần tối màu, đồng hồ kim dây thép.
- AN, nữ, 31 tuổi. Áo len xanh rêu, áo khoác kem, tóc đen ngang vai.
- ÔNG HẢI, nam, khoảng 60 tuổi. Nhân viên nhà ga, áo sơ mi xanh nhạt, áo khoác đồng phục sẫm màu.

ĐẠO CỤ CẦN GIỮ NHẤT QUÁN
- Chiếc vé tàu giấy màu xanh nhạt, góc phải bị rách, in số ghế 17 và giờ 23:40.
- Đồng hồ đeo tay của Khải, mặt tròn màu đen, dây thép.
- Máy ghi âm nhỏ màu bạc, có một đèn LED đỏ.
- Chiếc ô màu vàng của An, cán gỗ cong.

CẢNH 1 — CĂN HỘ — ĐÊM — HIỆN TẠI
Khải nhìn chiếc vé và đồng hồ.

CẢNH 2 — NHÀ GA — ĐÊM — HIỆN TẠI
Khải gặp ông Hải. An gọi điện cho Khải.
"""
    project = analyze_story(
        AnalyzeRequest(
            name="vietnamese profile locks",
            original_text=script,
            settings=VideoSettings(scene_duration=8),
        )
    )

    khai = next(item for item in project.characters if item.name == "KHẢI")
    an = next(item for item in project.characters if item.name == "AN")
    hai = next(item for item in project.characters if item.name == "ÔNG HẢI")

    assert khai.gender == "Nam"
    assert khai.estimated_age == "35 tuổi"
    assert "sơ mi xám đậm" in khai.clothing.casefold()
    assert "áo khoác đen" in khai.clothing.casefold()
    assert "quần tối màu" in khai.clothing.casefold()
    assert "đồng hồ kim dây thép" in khai.accessories.casefold()
    assert "35 tuổi" in khai.identifying_features

    assert an.gender == "Nữ"
    assert an.estimated_age == "31 tuổi"
    assert "áo len xanh rêu" in an.clothing.casefold()
    assert "áo khoác kem" in an.clothing.casefold()
    assert "tóc đen ngang vai" in an.hairstyle.casefold()

    assert hai.gender == "Nam"
    assert "60 tuổi" in hai.identifying_features
    assert "nhân viên nhà ga" in hai.identifying_features.casefold()
    assert "áo sơ mi xanh nhạt" in hai.clothing.casefold()

    prop_text = " ".join(
        f"{item.name} {item.description} {item.state}" for item in project.props
    ).casefold()
    for expected in (
        "góc phải bị rách",
        "ghế 17",
        "23:40",
        "mặt tròn màu đen",
        "dây thép",
        "led đỏ",
        "cán gỗ cong",
    ):
        assert expected in prop_text
