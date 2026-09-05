"""Technical audio QC for rendered scene MP4 files.

This module measures the actual media file with FFmpeg. It intentionally does not
pretend to verify speaker identity or dialogue semantics without an audio-capable
model; those semantics remain locked by the canonical Audio Bible/render contract.
"""
from __future__ import annotations

import asyncio
import math
import re
from pathlib import Path

from .flow_media import ffmpeg_path
from .models import AudioQCReport, Project, Scene, VisualIssue


def _safe_media_path(data_root: Path, relative: str) -> Path | None:
    if not relative:
        return None
    candidate = (data_root / relative).resolve()
    try:
        candidate.relative_to(data_root.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def _audio_targets(scene: Scene) -> tuple[float, float, float]:
    locks = scene.orchestration.get("audio_locks", {})
    if not isinstance(locks, dict):
        return -16.0, -1.0, 4.0

    def number(key: str, default: float) -> float:
        try:
            value = float(locks.get(key, default))
        except (TypeError, ValueError):
            return default
        return value if math.isfinite(value) else default

    return (
        number("target_lufs", -16.0),
        number("true_peak_db", -1.0),
        max(0.0, number("loudness_tolerance_lu", 4.0)),
    )


def _parse_audio_metadata(text: str) -> tuple[bool, int, int]:
    audio_lines = [
        line.strip()
        for line in text.splitlines()
        if re.search(r"Stream #.*Audio:", line)
    ]
    if not audio_lines:
        return False, 0, 0
    line = audio_lines[0]
    rate_match = re.search(r"(\d{4,6})\s*Hz", line)
    sample_rate = int(rate_match.group(1)) if rate_match else 0

    lowered = line.casefold()
    if "mono" in lowered:
        channels = 1
    elif "stereo" in lowered:
        channels = 2
    elif "5.1" in lowered:
        channels = 6
    elif "7.1" in lowered:
        channels = 8
    else:
        channel_match = re.search(r"(\d+)\s+channels?", lowered)
        channels = int(channel_match.group(1)) if channel_match else 0
    return True, sample_rate, channels


def _last_finite(pattern: str, text: str) -> float | None:
    matches = re.findall(pattern, text, flags=re.IGNORECASE)
    for raw in reversed(matches):
        value = str(raw).strip().lower()
        if "inf" in value:
            continue
        try:
            number = float(value)
        except ValueError:
            continue
        if math.isfinite(number):
            return number
    return None


async def _measure_audio(ffmpeg: str, video: Path) -> tuple[str, int]:
    process = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-hide_banner",
        "-i",
        str(video),
        "-vn",
        "-af",
        "ebur128=peak=true",
        "-f",
        "null",
        "-",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, error = await process.communicate()
    return error.decode("utf-8", errors="replace"), process.returncode or 0


class AudioQCAnalyzer:
    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root.resolve()

    async def inspect_scene(self, project: Project, scene: Scene) -> AudioQCReport:
        video = _safe_media_path(self.data_root, scene.result_file)
        if video is None:
            return AudioQCReport(
                status="Unavailable",
                issues=[
                    VisualIssue(
                        code="AUDIO_VIDEO_MISSING",
                        message="Rendered video file is missing for audio QC.",
                    )
                ],
            )
        ffmpeg = ffmpeg_path()
        if not ffmpeg:
            return AudioQCReport(
                status="Unavailable",
                issues=[
                    VisualIssue(
                        code="FFMPEG_UNAVAILABLE",
                        message="FFmpeg is unavailable for audio QC.",
                    )
                ],
            )

        output, return_code = await _measure_audio(ffmpeg, video)
        present, sample_rate, channels = _parse_audio_metadata(output)
        if not present:
            return AudioQCReport(
                status="Failed",
                score=0,
                audio_present=False,
                issues=[
                    VisualIssue(
                        code="AUDIO_STREAM_MISSING",
                        message="Rendered Google Flow scene has no audio stream.",
                    )
                ],
            )
        if return_code != 0:
            return AudioQCReport(
                status="Unavailable",
                audio_present=True,
                sample_rate_hz=sample_rate,
                channels=channels,
                issues=[
                    VisualIssue(
                        code="AUDIO_MEASUREMENT_FAILED",
                        message="FFmpeg could not complete loudness analysis.",
                    )
                ],
            )

        integrated = _last_finite(r"\bI:\s*([-+]?\w+(?:\.\d+)?)\s*LUFS", output)
        true_peak = _last_finite(r"\bPeak:\s*([-+]?\w+(?:\.\d+)?)\s*dBFS", output)
        target_lufs, max_peak, tolerance = _audio_targets(scene)
        issues: list[VisualIssue] = []
        if integrated is None:
            issues.append(
                VisualIssue(
                    code="LOUDNESS_UNAVAILABLE",
                    message="Integrated loudness could not be measured.",
                )
            )
        elif abs(integrated - target_lufs) > tolerance:
            issues.append(
                VisualIssue(
                    code="LOUDNESS_OUT_OF_RANGE",
                    message=(
                        f"Integrated loudness {integrated:.1f} LUFS differs from "
                        f"target {target_lufs:.1f} LUFS by more than {tolerance:.1f} LU."
                    ),
                )
            )

        clipping = true_peak is not None and true_peak > max_peak
        if clipping:
            issues.append(
                VisualIssue(
                    code="TRUE_PEAK_TOO_HIGH",
                    message=(
                        f"True peak {true_peak:.1f} dBFS exceeds maximum "
                        f"{max_peak:.1f} dBFS."
                    ),
                )
            )
        if sample_rate and sample_rate < 44_100:
            issues.append(
                VisualIssue(
                    code="AUDIO_SAMPLE_RATE_LOW",
                    message=f"Audio sample rate {sample_rate} Hz is below 44.1 kHz.",
                )
            )

        loudness_penalty = (
            min(50, round(abs(integrated - target_lufs) * 6))
            if integrated is not None
            else 50
        )
        peak_penalty = 30 if clipping else 0
        rate_penalty = 20 if sample_rate and sample_rate < 44_100 else 0
        score = max(0, 100 - loudness_penalty - peak_penalty - rate_penalty)
        failed = any(issue.severity == "error" for issue in issues)
        return AudioQCReport(
            status="Failed" if failed else "Passed",
            score=score,
            audio_present=True,
            sample_rate_hz=sample_rate,
            channels=channels,
            integrated_lufs=integrated,
            true_peak_db=true_peak,
            clipping_detected=clipping,
            model_id="ffmpeg-ebur128",
            issues=issues,
        )
