"""Tests for flow_media module."""

from pathlib import Path
from unittest.mock import patch

import pytest

from flow_story_studio.flow_media import (
    VisualFrames,
    extract_last_frame,
    extract_qc_frames,
    extract_visual_frames,
    ffmpeg_path,
)


class TestFfmpegPath:
    def test_ffmpeg_path_returns_none_when_not_found(self) -> None:
        with patch("flow_story_studio.flow_media.shutil.which", return_value=None):
            with patch.object(Path, "is_file", return_value=False):
                result = ffmpeg_path()
                assert result is None

    def test_ffmpeg_path_finds_system_ffmpeg(self) -> None:
        ffmpeg_path_str = "C:\\ffmpeg\\bin\\ffmpeg.exe"
        with patch(
            "flow_story_studio.flow_media.shutil.which", return_value=ffmpeg_path_str
        ):
            result = ffmpeg_path()
            assert result == ffmpeg_path_str


class TestVisualFrames:
    def test_visual_frames_default(self) -> None:
        frames = VisualFrames()
        assert frames.first == ""
        assert frames.middle == ""
        assert frames.last == ""

    def test_visual_frames_with_values(self) -> None:
        frames = VisualFrames(first="first.jpg", middle="middle.jpg", last="last.jpg")
        assert frames.first == "first.jpg"
        assert frames.middle == "middle.jpg"
        assert frames.last == "last.jpg"


class TestExtractLastFrame:
    @pytest.mark.asyncio
    async def test_extract_last_frame_no_ffmpeg(self, tmp_path: Path) -> None:
        with patch("flow_story_studio.flow_media.ffmpeg_path", return_value=None):
            result = await extract_last_frame(
                tmp_path, "project-1", "scene-1", tmp_path / "video.mp4"
            )
            assert result == ""

    @pytest.mark.asyncio
    async def test_extract_last_frame_video_not_file(self, tmp_path: Path) -> None:
        with patch("flow_story_studio.flow_media.ffmpeg_path", return_value="ffmpeg"):
            result = await extract_last_frame(
                tmp_path, "project-1", "scene-1", tmp_path / "video.mp4"
            )
            assert result == ""


class TestExtractQcFrames:
    @pytest.mark.asyncio
    async def test_extract_qc_frames_no_ffmpeg(self, tmp_path: Path) -> None:
        with patch("flow_story_studio.flow_media.ffmpeg_path", return_value=None):
            result = await extract_qc_frames(
                tmp_path, "project-1", "scene-1", tmp_path / "video.mp4"
            )
            assert result == ("", "", "")

    @pytest.mark.asyncio
    async def test_extract_qc_frames_video_not_file(self, tmp_path: Path) -> None:
        with patch("flow_story_studio.flow_media.ffmpeg_path", return_value="ffmpeg"):
            result = await extract_qc_frames(
                tmp_path, "project-1", "scene-1", tmp_path / "video.mp4"
            )
            assert result == ("", "", "")


class TestExtractVisualFrames:
    @pytest.mark.asyncio
    async def test_extract_visual_frames_no_ffmpeg(self, tmp_path: Path) -> None:
        with patch("flow_story_studio.flow_media.ffmpeg_path", return_value=None):
            result = await extract_visual_frames(
                tmp_path, "project-1", "scene-1", tmp_path / "video.mp4"
            )
            assert result == VisualFrames()

    @pytest.mark.asyncio
    async def test_extract_visual_frames_video_not_file(self, tmp_path: Path) -> None:
        with patch("flow_story_studio.flow_media.ffmpeg_path", return_value="ffmpeg"):
            result = await extract_visual_frames(
                tmp_path, "project-1", "scene-1", tmp_path / "video.mp4"
            )
            assert result == VisualFrames()
