"""Join completed scene videos into one workspace-local MP4."""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from .models import Project


class VideoMergeError(RuntimeError):
    """A safe error produced by the final-video pipeline."""


@dataclass(slots=True)
class VideoMergeResult:
    result_file: str
    scene_count: int


class VideoMerger:
    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root.resolve()

    @staticmethod
    def ffmpeg_path() -> str | None:
        if getattr(sys, "frozen", False):
            bundled = Path(getattr(sys, "_MEIPASS", "")) / "ffmpeg.exe"
            if bundled.is_file():
                return str(bundled)
        return shutil.which("ffmpeg")

    def clips_for(self, project: Project) -> list[Path]:
        if not project.scenes:
            raise VideoMergeError("Dự án chưa có scene để ghép")
        clips: list[Path] = []
        incomplete: list[str] = []
        for scene in sorted(project.scenes, key=lambda item: item.order):
            if scene.status != "Completed" or not scene.result_file:
                incomplete.append(scene.id)
                continue
            clip = (self.data_root / scene.result_file).resolve()
            try:
                clip.relative_to(self.data_root)
            except ValueError as exc:
                raise VideoMergeError(f"Đường dẫn video của {scene.id} không hợp lệ") from exc
            if not clip.is_file() or clip.stat().st_size <= 0:
                incomplete.append(scene.id)
                continue
            clips.append(clip)
        if incomplete:
            raise VideoMergeError(
                "Chưa thể ghép; các scene chưa có tệp video hoàn chỉnh: " + ", ".join(incomplete)
            )
        return clips

    async def merge(self, project: Project) -> VideoMergeResult:
        ffmpeg = self.ffmpeg_path()
        if not ffmpeg:
            raise VideoMergeError("Không tìm thấy FFmpeg để ghép video")
        clips = self.clips_for(project)
        output_dir = self.data_root / "renders" / project.id / "final"
        output_dir.mkdir(parents=True, exist_ok=True)
        concat_file = output_dir / ".concat-list.txt"
        temporary = output_dir / ".final-video.tmp.mp4"
        target = output_dir / "final-video.mp4"
        concat_file.write_text(
            "".join(f"file '{self._escape_concat_path(clip)}'\n" for clip in clips),
            encoding="utf-8",
        )
        temporary.unlink(missing_ok=True)
        command = [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(temporary),
        ]
        try:
            return_code, error = await self._execute(command)
            if return_code != 0 or not temporary.is_file() or temporary.stat().st_size <= 0:
                detail = error.strip().splitlines()[-1][:500] if error.strip() else "lỗi không rõ"
                raise VideoMergeError(f"FFmpeg không thể ghép video: {detail}")
            os.replace(temporary, target)
        finally:
            concat_file.unlink(missing_ok=True)
            temporary.unlink(missing_ok=True)
        return VideoMergeResult(
            result_file=target.relative_to(self.data_root).as_posix(),
            scene_count=len(clips),
        )

    @staticmethod
    def _escape_concat_path(path: Path) -> str:
        return path.resolve().as_posix().replace("'", "'\\''")

    @staticmethod
    async def _execute(command: list[str]) -> tuple[int, str]:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await process.communicate()
        except asyncio.CancelledError:
            process.terminate()
            await process.wait()
            raise
        return process.returncode or 0, stderr.decode("utf-8", errors="replace")
