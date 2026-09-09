from flow_story_studio.engines.analyzer import analyze_story
from flow_story_studio.models import AnalyzeRequest, VideoSettings


def test_location_master_uses_source_grounded_fixed_topology():
    script = """
TARGET RUNTIME: 8 seconds

CẢNH 1 — SẢNH NHÀ GA — ĐÊM
Cửa kính chính nằm đối diện quầy vé. Khải bước qua cửa rồi dừng cạnh quầy.
"""
    project = analyze_story(
        AnalyzeRequest(
            name="spatial bible",
            original_text=script,
            settings=VideoSettings(scene_duration=8),
        )
    )
    location = next(item for item in project.locations if "SẢNH NHÀ GA" in item.name.upper())
    assert "fixed glass entrance/door" in location.spatial_anchors
    assert "fixed ticket counter" in location.spatial_anchors

    reference = next(
        item
        for item in project.visual_bible.references
        if item.entity_type == "location" and item.entity_id == location.id
    )
    assert "SOURCE-GROUNDED FIXED TOPOLOGY" in reference.lock_text
    assert "fixed ticket counter" in reference.lock_text
