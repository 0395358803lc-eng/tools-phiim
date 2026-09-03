"""Local media helpers for Google Flow rendering."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from pathlib import Path


def ffmpeg_path() -> str | None:
    if getattr(sys, "frozen", False):
        bundled = Path(getattr(sys, "_MEIPASS", "")) / "ffmpeg.exe"
        if bundled.is_file():
            return str(bundled)
    return shutil.which("ffmpeg")


async def extract_last_frame(data_root: Path, project_id: str, scene_id: str, video: Path) -> str:
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        return ""
    target = data_root / "references" / project_id / f"{scene_id}-last-frame.jpg"
    target.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg,
        "-y",
        "-sseof",
        "-0.08",
        "-i",
        str(video),
        "-frames:v",
        "1",
        "-q:v",
        "2",
        str(target),
    ]

    def run() -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(command, capture_output=True, timeout=60, check=False)

    result = await asyncio.to_thread(run)
    if result.returncode != 0 or not target.is_file():
        return ""
    return target.resolve().relative_to(data_root).as_posix()
