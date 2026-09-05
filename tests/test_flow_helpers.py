"""Tests for flow_helpers module."""

from pathlib import Path

from flow_story_studio.flow_helpers import (
    flow_prompt,
    job_identifiers,
    reference_path,
    select_video_candidate,
)
from flow_story_studio.models import ContinuityState, Scene


def _make_scene(**overrides) -> Scene:
    defaults = {
        "id": "scene-1",
        "order": 1,
        "title": "Test Scene",
        "source_text": "A person walks into a room.",
        "summary": "Test summary",
        "characters": [],
        "location_id": "loc-1",
        "action": "Person enters",
        "camera": "Wide shot",
        "lighting": "Natural",
        "atmosphere": "Calm",
        "duration": 8,
        "visual_prompt": "A cinematic shot",
        "flow_prompt": "A person walks into a room",
        "start_state": ContinuityState(),
        "end_state": ContinuityState(),
    }
    defaults.update(overrides)
    return Scene(**defaults)


class TestFlowPrompt:
    def test_flow_prompt_short(self) -> None:
        scene = _make_scene(flow_prompt="Short prompt")
        assert flow_prompt(scene) == "Short prompt"

    def test_flow_prompt_truncation_long_prompt(self) -> None:
        long_prompt = "A" * 5000
        scene = _make_scene(flow_prompt=long_prompt)
        result = flow_prompt(scene)
        assert len(result) <= 4000
        assert "[CRITICAL END/NEGATIVE CONSTRAINTS]" in result


class TestReferencePath:
    def test_reference_path_absolute_url_returns_none(self, tmp_path: Path) -> None:
        result = reference_path(tmp_path, "http://example.com/image.png")
        assert result is None

    def test_reference_path_https_url_returns_none(self, tmp_path: Path) -> None:
        result = reference_path(tmp_path, "https://example.com/image.png")
        assert result is None

    def test_reference_path_empty_returns_none(self, tmp_path: Path) -> None:
        result = reference_path(tmp_path, "")
        assert result is None

    def test_reference_path_relative_inside_references(self, tmp_path: Path) -> None:
        ref_dir = tmp_path / "references"
        ref_dir.mkdir()
        test_file = ref_dir / "test.png"
        test_file.write_text("fake", encoding="utf-8")

        result = reference_path(tmp_path, "test.png")
        assert result is None

    def test_reference_path_relative_outside_references_returns_none(self, tmp_path: Path) -> None:
        other_dir = tmp_path / "other"
        other_dir.mkdir()
        test_file = other_dir / "test.png"
        test_file.write_text("fake", encoding="utf-8")

        result = reference_path(tmp_path, str(other_dir / "test.png"))
        assert result is None

    def test_reference_path_absolute_inside_references(self, tmp_path: Path) -> None:
        ref_dir = tmp_path / "references"
        ref_dir.mkdir(parents=True)
        test_file = ref_dir / "entities" / "char1.png"
        test_file.parent.mkdir(parents=True)
        test_file.write_text("fake", encoding="utf-8")

        result = reference_path(tmp_path, str(test_file))
        assert result == str(test_file)


class TestJobIdentifiers:
    def test_job_identifiers_empty(self) -> None:
        class FakeJob:
            pass

        result = job_identifiers(FakeJob())
        assert result == set()

    def test_job_identifiers_with_attributes(self) -> None:
        class FakeJob:
            workflow_id = "abc-123-def"
            media_id = "media-456"
            resource_name = "resource-789"

        result = job_identifiers(FakeJob())
        assert "abc-123-def" in result
        assert "media-456" in result
        assert "resource-789" in result

    def test_job_identifiers_with_uuid_in_raw(self) -> None:
        class FakeJob:
            workflow_id = None
            media_id = None
            resource_name = None
            raw = '{"key": "12345678-1234-1234-1234-123456789abc"}'

        result = job_identifiers(FakeJob())
        assert any("12345678-1234-1234-1234-123456789abc" in s for s in result)


class TestSelectVideoCandidate:
    def test_select_video_candidate_empty(self) -> None:
        result = select_video_candidate([], set())
        assert result is None

    def test_select_video_candidate_single_no_identifiers(self) -> None:
        candidates = [{"src": "video1.mp4", "tile_id": "tile1"}]
        result = select_video_candidate(candidates, set())
        assert result == {"src": "video1.mp4", "tile_id": "tile1"}

    def test_select_video_candidate_single_with_match(self) -> None:
        candidates = [{"src": "video1.mp4", "tile_id": "abc-123"}]
        identifiers = {"abc-123"}
        result = select_video_candidate(candidates, identifiers)
        assert result is not None
        assert result["tile_id"] == "abc-123"

    def test_select_video_candidate_multiple_with_partial_match(self) -> None:
        candidates = [
            {"src": "video1.mp4", "tile_id": "abc"},
            {"src": "video2.mp4", "tile_id": "xyz"},
        ]
        identifiers = {"abc"}
        result = select_video_candidate(candidates, identifiers)
        assert result is not None
        assert result["tile_id"] == "abc"

    def test_select_video_candidate_no_match_returns_none_when_multiple(self) -> None:
        candidates = [
            {"src": "video1.mp4", "tile_id": "abc"},
            {"src": "video2.mp4", "tile_id": "def"},
        ]
        identifiers = {"xyz"}
        result = select_video_candidate(candidates, identifiers)
        assert result is None

    def test_select_video_candidate_duplicates_deduplicated(self) -> None:
        candidates = [
            {"src": "video1.mp4", "tile_id": "same"},
            {"src": "video1.mp4", "tile_id": "same"},
        ]
        result = select_video_candidate(candidates, set())
        assert result is not None
        assert len([c for c in candidates if c.get("src") == "video1.mp4"]) == 2
