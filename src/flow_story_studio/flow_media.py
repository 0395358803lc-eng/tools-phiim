"""Local media helpers for Google Flow rendering and post-render QC."""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess  # nosec B404
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class VisualFrames:
    first: str = ""
    middle: str = ""
    last: str = ""


def ffmpeg_path() -> str | None:
    if getattr(sys, "frozen", False):
        bundled = Path(getattr(sys, "_MEIPASS", "")) / "ffmpeg.exe"
        if bundled.is_file():
            return str(bundled)
    return shutil.which("ffmpeg")


def _duration_seconds(ffmpeg: str, video: Path) -> float | None:
    result = subprocess.run(  # nosec B603
        [ffmpeg, "-i", str(video)],
        capture_output=True,
        timeout=30,
        check=False,
    )
    text = result.stderr.decode("utf-8", errors="ignore")
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _relative(data_root: Path, target: Path) -> str:
    return target.resolve().relative_to(data_root.resolve()).as_posix()


async def _extract_relative_at(
    data_root: Path,
    ffmpeg: str,
    video: Path,
    target: Path,
    second: float,
) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg,
        "-y",
        "-ss",
        f"{max(0.0, second):.3f}",
        "-i",
        str(video),
        "-frames:v",
        "1",
        "-q:v",
        "2",
        str(target),
    ]

    def run() -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(command, capture_output=True, timeout=60, check=False)  # nosec B603

    result = await asyncio.to_thread(run)
    if result.returncode != 0 or not target.is_file():
        return ""
    return _relative(data_root, target)


async def _extract_relative_from_end(
    data_root: Path,
    ffmpeg: str,
    video: Path,
    target: Path,
    seconds_from_end: float = 0.08,
) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)

    async def attempt(offset: float) -> subprocess.CompletedProcess[bytes]:
        command = [
            ffmpeg,
            "-y",
            "-sseof",
            f"-{max(0.01, offset):.3f}",
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(target),
        ]

        def run() -> subprocess.CompletedProcess[bytes]:
            return subprocess.run(command, capture_output=True, timeout=60, check=False)  # nosec B603

        return await asyncio.to_thread(run)

    result = await attempt(seconds_from_end)
    if result.returncode != 0 or not target.is_file():
        result = await attempt(max(0.5, seconds_from_end))
    if result.returncode != 0 or not target.is_file():
        return ""
    return _relative(data_root, target)


async def extract_last_frame(
    data_root: Path,
    project_id: str,
    scene_id: str,
    video: Path,
) -> str:
    ffmpeg = ffmpeg_path()
    if not ffmpeg or not video.is_file():
        return ""
    target = data_root / "references" / project_id / f"{scene_id}-last-frame.jpg"
    return await _extract_relative_from_end(data_root, ffmpeg, video, target)


async def extract_visual_frames(
    data_root: Path,
    project_id: str,
    scene_id: str,
    video: Path,
) -> VisualFrames:
    ffmpeg = ffmpeg_path()
    if not ffmpeg or not video.is_file():
        return VisualFrames()
    duration = await asyncio.to_thread(_duration_seconds, ffmpeg, video)
    middle_at = max(0.05, (duration or 0.2) / 2.0)
    root = data_root / "references" / project_id / "qc"
    first_target = root / f"{scene_id}-first.jpg"
    middle_target = root / f"{scene_id}-middle.jpg"
    last_target = root / f"{scene_id}-last.jpg"
    first, middle, last = await asyncio.gather(
        _extract_relative_at(data_root, ffmpeg, video, first_target, 0.05),
        _extract_relative_at(data_root, ffmpeg, video, middle_target, middle_at),
        _extract_relative_from_end(data_root, ffmpeg, video, last_target),
    )
    return VisualFrames(first=first, middle=middle, last=last)


async def extract_qc_frames(
    data_root: Path,
    project_id: str,
    scene_id: str,
    video: Path,
) -> tuple[str, str, str]:
    """Backward-compatible tuple wrapper around the single QC frame extractor."""
    frames = await extract_visual_frames(data_root, project_id, scene_id, video)
    return frames.first, frames.middle, frames.last
