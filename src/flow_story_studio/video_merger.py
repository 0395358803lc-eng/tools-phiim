"""Join completed scene videos into one workspace-local MP4."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .film.qc import film_hard_blockers
from .models import Project, Scene
from .production_gate import is_scene_production_ready


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
        film_blockers = film_hard_blockers(project)
        if film_blockers:
            raise VideoMergeError(
                "Film-level validation failed: " + "; ".join(film_blockers)
            )
        clips: list[Path] = []
        incomplete: list[str] = []
        for scene in sorted(project.scenes, key=lambda item: item.order):
            if not is_scene_production_ready(project, scene):
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
        duplicates = self._duplicate_clip_hashes(clips)
        if duplicates:
            detail = ", ".join(
                f"{left.name}={right.name}" for left, right in duplicates
            )
            raise VideoMergeError(
                "Phát hiện scene video trùng byte-for-byte; từ chối final merge: " + detail
            )
        return clips

    @staticmethod
    def _duplicate_clip_hashes(clips: list[Path]) -> list[tuple[Path, Path]]:
        seen: dict[str, Path] = {}
        duplicates: list[tuple[Path, Path]] = []
        for clip in clips:
            digest = hashlib.sha256()
            with clip.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            hexdigest = digest.hexdigest()
            previous = seen.get(hexdigest)
            if previous is not None:
                duplicates.append((previous, clip))
            else:
                seen[hexdigest] = clip
        return duplicates

    @staticmethod
    def _final_audio_filter(project: Project) -> str:
        audio_bible = project.film_model.get("audio_bible", {})
        if not isinstance(audio_bible, dict):
            audio_bible = {}
        try:
            target_lufs = float(audio_bible.get("target_lufs", -16.0))
        except (TypeError, ValueError):
            target_lufs = -16.0
        try:
            true_peak = float(audio_bible.get("true_peak_db", -1.0))
        except (TypeError, ValueError):
            true_peak = -1.0
        return f"loudnorm=I={target_lufs:.1f}:TP={true_peak:.1f}:LRA=11"

    @staticmethod
    def ffprobe_path(ffmpeg: str | None = None) -> str | None:
        if ffmpeg:
            sibling = Path(ffmpeg).with_name(
                "ffprobe.exe" if sys.platform == "win32" else "ffprobe"
            )
            if sibling.is_file():
                return str(sibling)
        return shutil.which("ffprobe")

    @staticmethod
    async def _probe_duration(ffprobe: str, clip: Path) -> float | None:
        process = await asyncio.create_subprocess_exec(
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(clip),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        output, _error = await process.communicate()
        if process.returncode != 0:
            return None
        try:
            duration = float(output.decode("utf-8", errors="replace").strip())
        except ValueError:
            return None
        return duration if duration > 0 else None

    @staticmethod
    async def _frame_dhash(
        ffmpeg: str, clip: Path, timestamp: float
    ) -> tuple[int, int] | None:
        process = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{max(0.0, timestamp):.3f}",
            "-i",
            str(clip),
            "-frames:v",
            "1",
            "-vf",
            "scale=9:8,format=gray",
            "-pix_fmt",
            "gray",
            "-f",
            "rawvideo",
            "pipe:1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        raw, _error = await process.communicate()
        if process.returncode != 0 or len(raw) < 72:
            return None
        pixels = raw[:72]
        digest = 0
        for row_index in range(8):
            row = pixels[row_index * 9 : (row_index + 1) * 9]
            for column in range(8):
                digest = (digest << 1) | int(row[column] > row[column + 1])
        return digest, round(sum(pixels) / len(pixels))

    @staticmethod
    async def _clip_has_audio(ffmpeg: str, clip: Path) -> bool:
        process = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-hide_banner",
            "-i",
            str(clip),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, error = await process.communicate()
        text = error.decode("utf-8", errors="ignore")
        return bool(re.search(r"Stream #.*Audio:", text))

    @classmethod
    async def _all_clips_have_audio(cls, ffmpeg: str, clips: list[Path]) -> bool:
        if not clips:
            return False
        values = await asyncio.gather(
            *(cls._clip_has_audio(ffmpeg, clip) for clip in clips)
        )
        return all(values)

    @classmethod
    async def _near_duplicate_clip_hashes(
        cls,
        ffmpeg: str,
        clips: list[Path],
        scenes: list[Scene],
    ) -> list[tuple[str, str]]:
        ffprobe = cls.ffprobe_path(ffmpeg)
        if not ffprobe:
            return []

        signatures: list[list[tuple[int, int]] | None] = []
        for clip, _scene in zip(clips, scenes, strict=True):
            duration = await cls._probe_duration(ffprobe, clip)
            if duration is None or duration <= 0:
                signatures.append(None)
                continue
            points = (0.15, 0.50, 0.85)
            times = [
                min(max(0.0, duration * point), max(0.0, duration - 0.02))
                for point in points
            ]
            samples = [
                await cls._frame_dhash(ffmpeg, clip, timestamp)
                for timestamp in times
            ]
            if any(item is None for item in samples):
                signatures.append(None)
                continue
            signatures.append([item for item in samples if item is not None])

        duplicates: list[tuple[str, str]] = []
        for left in range(len(signatures)):
            if signatures[left] is None:
                continue
            for right in range(left + 1, len(signatures)):
                if signatures[right] is None:
                    continue
                if cls._visual_signatures_match(
                    signatures[left] or [],
                    signatures[right] or [],
                ):
                    duplicates.append((scenes[left].id, scenes[right].id))
        return duplicates

    @staticmethod
    def _visual_signatures_match(
        left: list[tuple[int, int]], right: list[tuple[int, int]]
    ) -> bool:
        if len(left) != len(right) or not left:
            return False
        for (left_hash, left_luma), (right_hash, right_luma) in zip(
            left, right, strict=True
        ):
            if (left_hash ^ right_hash).bit_count() > 4:
                return False
            if abs(left_luma - right_luma) > 6:
                return False
        return True

    async def merge(
        self, project: Project, progress: Callable[[int], None] | None = None
    ) -> VideoMergeResult:
        ffmpeg = self.ffmpeg_path()
        if not ffmpeg:
            raise VideoMergeError("Không tìm thấy FFmpeg để ghép video")
        clips = self.clips_for(project)
        ordered_scenes = sorted(project.scenes, key=lambda item: item.order)
        near_duplicates = await self._near_duplicate_clip_hashes(
            ffmpeg, clips, ordered_scenes
        )
        if near_duplicates:
            detail = ", ".join(
                f"{left}≈{right}" for left, right in near_duplicates
            )
            raise VideoMergeError(
                "Phát hiện scene video gần như trùng hình ảnh; từ chối final merge: "
                + detail
            )
        expected_duration = float(sum(scene.duration for scene in project.scenes))
        all_audio = await self._all_clips_have_audio(ffmpeg, clips)
        if project.settings.provider == "google-flow" and not all_audio:
            raise VideoMergeError(
                "Final merge bị chặn: ít nhất một scene Google Flow đã mất audio stream"
            )
        audio_filter = self._final_audio_filter(project)
        audio_filter_args = (
            [
                "-af",
                audio_filter,
                "-ar",
                "48000",
                "-ac",
                "2",
            ]
            if all_audio and audio_filter
            else []
        )
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
            *audio_filter_args,
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            "-progress",
            "pipe:1",
            "-nostats",
            str(temporary),
        ]
        try:
            return_code, error = await self._execute(command, progress, expected_duration)
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
    async def _execute(
        command: list[str],
        progress: Callable[[int], None] | None = None,
        expected_duration: float = 0.0,
    ) -> tuple[int, str]:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        if process.stdout is None or process.stderr is None:
            process.terminate()
            await process.wait()
            raise RuntimeError("FFmpeg stdout/stderr pipes were not created")
        stderr_task = asyncio.create_task(process.stderr.read())
        try:
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                if (
                    progress
                    and expected_duration > 0
                    and line.startswith((b"out_time_us=", b"out_time_ms="))
                ):
                    try:
                        elapsed = int(line.split(b"=", 1)[1]) / 1_000_000
                    except (ValueError, IndexError):
                        continue
                    progress(min(99, max(1, round(elapsed / expected_duration * 100))))
            await process.wait()
            stderr = await stderr_task
        except asyncio.CancelledError:
            process.terminate()
            await process.wait()
            stderr_task.cancel()
            await asyncio.gather(stderr_task, return_exceptions=True)
            raise
        return process.returncode or 0, stderr.decode("utf-8", errors="replace")
