from flow_story_studio.analysis_providers.merging import _entity_mentioned
from flow_story_studio.engines.analyzer import analyze_story
from flow_story_studio.models import AnalyzeRequest, VideoSettings


def test_short_vietnamese_name_does_not_match_accent_folded_common_word() -> None:
    assert not _entity_mentioned("Medium shot bàn ăn và Khải", "AN")
    assert _entity_mentioned("AN gọi đến qua điện thoại", "AN")


def test_explicit_cast_blocks_generic_roles_and_markdown_rules_from_entities() -> None:
    script = """
**TARGET RUNTIME:** 24 seconds

## CHARACTERS
- **KHẢI**, nam, 35 tuổi.
- **AN**, nữ, 31 tuổi.
- **ÔNG HẢI**, nam, khoảng 60 tuổi. Nhân viên nhà ga.

## PROPS
- **Chiếc vé xanh**, góc phải bị rách.
---
- **Máy ghi âm bạc**, có đèn đỏ.

## SCENE 1 — CĂN HỘ — ĐÊM
Khải đang ngồi bên bàn ăn. AN gọi qua điện thoại.

## SCENE 2 — NHÀ GA — ĐÊM
ÔNG HẢI, nhân viên nhà ga, đứng sau quầy. Khải bước vào.
"""
    project = analyze_story(
        AnalyzeRequest(
            name="short-name invariants",
            original_text=script,
            settings=VideoSettings(scene_duration=8),
        )
    )

    assert [item.name.casefold() for item in project.characters] == [
        "khải",
        "an",
        "ông hải",
    ]
    assert all(item.name not in {"Nhân viên", "đang", "--"} for item in project.characters)
    assert all(item.name != "--" for item in project.props)
