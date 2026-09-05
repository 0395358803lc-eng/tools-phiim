import asyncio
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from flow_story_studio.main import create_app
from flow_story_studio.models import (
    AnalyzeRequest,
    ContinuityQCReport,
    FinalVideo,
    QualityReport,
    VisualQCReport,
)
from flow_story_studio.service import StudioService
from flow_story_studio.storage import ProjectStorage
from flow_story_studio.video_merger import (
    VideoMergeError,
    VideoMerger,
    VideoMergeResult,
)

TEXT = (
    "Người phụ nữ mở cửa căn phòng rồi bước tới chiếc bàn. "
    "Cô đặt quyển sách xuống, nhìn qua cửa sổ và mỉm cười. "
    "Sau đó cô quay lại, cầm quyển sách lên và rời khỏi phòng."
)


def completed_project(storage: ProjectStorage, data_root: Path):
    project = StudioService(storage).analyze(AnalyzeRequest(name="Final movie", original_text=TEXT))
    for index, scene in enumerate(project.scenes, start=1):
        clip = data_root / "renders" / project.id / scene.id / f"clip-{index}.mp4"
        clip.parent.mkdir(parents=True, exist_ok=True)
        clip.write_bytes(f"video-{index}".encode())
        scene.status = "Accepted"
        scene.progress = 100
        scene.acceptance.status = "Accepted"
        scene.acceptance.score = 100
        scene.quality = QualityReport()
        scene.visual_qc = VisualQCReport(
            status="Passed",
            score=100,
            character_identity=100,
            location_identity=100,
            prop_consistency=100,
            wardrobe_consistency=100,
            lighting_consistency=100,
            action_consistency=100,
            composition_consistency=100,
            model_id="mock",
        )
        scene.continuity_qc = (
            ContinuityQCReport(status="Passed", score=100, model_id="mock")
            if scene.visual_plan.dependency_mode == "direct"
            else ContinuityQCReport(status="NotApplicable", score=100, model_id="mock")
        )
        scene.result_file = clip.relative_to(data_root).as_posix()
    return storage.save(project)


def test_merger_uses_storyboard_order_and_atomic_output(tmp_path: Path, monkeypatch) -> None:
    storage = ProjectStorage(tmp_path / "projects")
    project = completed_project(storage, tmp_path)
    project.scenes = list(reversed(project.scenes))
    merger = VideoMerger(tmp_path)
    monkeypatch.setattr(merger, "ffmpeg_path", lambda: "ffmpeg")
    captured: dict[str, str] = {}

    async def fake_execute(
        command: list[str], progress=None, expected_duration=0.0
    ) -> tuple[int, str]:
        concat_path = Path(command[command.index("-i") + 1])
        captured["concat"] = concat_path.read_text(encoding="utf-8")
        if progress:
            progress(50)
        Path(command[-1]).write_bytes(b"joined-video")
        return 0, ""

    monkeypatch.setattr(merger, "_execute", fake_execute)
    progress_values: list[int] = []
    result = asyncio.run(merger.merge(project, progress=progress_values.append))

    assert progress_values == [50]
    ordered = sorted(project.scenes, key=lambda scene: scene.order)
    positions = [captured["concat"].index(scene.result_file.split("/")[-1]) for scene in ordered]
    assert positions == sorted(positions)
    assert result.scene_count == len(project.scenes)
    assert (tmp_path / result.result_file).read_bytes() == b"joined-video"
    assert not (tmp_path / "renders" / project.id / "final" / ".concat-list.txt").exists()


def test_merger_rejects_incomplete_scene(tmp_path: Path) -> None:
    storage = ProjectStorage(tmp_path / "projects")
    project = StudioService(storage).analyze(AnalyzeRequest(name="Incomplete", original_text=TEXT))
    with pytest.raises(VideoMergeError, match="chưa có tệp video"):
        VideoMerger(tmp_path).clips_for(project)


@pytest.mark.skipif(VideoMerger.ffmpeg_path() is None, reason="FFmpeg is not installed")
def test_merger_produces_playable_mp4_with_real_ffmpeg(tmp_path: Path) -> None:
    storage = ProjectStorage(tmp_path / "projects")
    project = completed_project(storage, tmp_path)
    ffmpeg = VideoMerger.ffmpeg_path()
    assert ffmpeg
    colors = ("red", "blue", "green", "yellow")
    for index, scene in enumerate(project.scenes):
        clip = tmp_path / scene.result_file
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"color=c={colors[index % len(colors)]}:s=320x180:d=0.2:r=24",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(clip),
            ],
            check=True,
        )
    storage.save(project)

    result = asyncio.run(VideoMerger(tmp_path).merge(project))
    output = tmp_path / result.result_file
    assert output.is_file()
    assert output.stat().st_size > 1_000
    assert output.read_bytes()[4:8] == b"ftyp"


def test_final_video_api_runs_in_background_and_serves_mp4(tmp_path: Path, monkeypatch) -> None:
    storage = ProjectStorage(tmp_path / "projects")
    project = completed_project(storage, tmp_path)
    app = create_app(storage)
    merger = app.state.merger
    monkeypatch.setattr(merger, "ffmpeg_path", lambda: "ffmpeg")

    async def fake_merge(current, progress=None) -> VideoMergeResult:
        if progress:
            progress(60)
        await asyncio.sleep(0)
        target = tmp_path / "renders" / current.id / "final" / "final-video.mp4"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"complete-mp4")
        return VideoMergeResult(
            result_file=target.relative_to(tmp_path).as_posix(),
            scene_count=len(current.scenes),
        )

    monkeypatch.setattr(merger, "merge", fake_merge)
    with TestClient(app) as client:
        started = client.post(f"/api/projects/{project.id}/final-video")
        assert started.status_code == 202
        assert started.json()["final_video"]["status"] == "Merging"

        for _ in range(20):
            current = client.get(f"/api/projects/{project.id}").json()
            if current["final_video"]["status"] != "Merging":
                break
        assert current["final_video"]["status"] == "Completed"
        assert current["final_video"]["scene_count"] == len(project.scenes)
        downloaded = client.get(f"/api/projects/{project.id}/final-video/file")
        assert downloaded.status_code == 200
        assert downloaded.content == b"complete-mp4"


def test_interrupted_merge_is_restored_to_ready_on_restart(tmp_path: Path) -> None:
    storage = ProjectStorage(tmp_path / "projects")
    project = completed_project(storage, tmp_path)
    project.final_video = FinalVideo(status="Merging", progress=10, scene_count=len(project.scenes))
    storage.save(project)

    with TestClient(create_app(storage)) as client:
        restored = client.get(f"/api/projects/{project.id}")
    assert restored.status_code == 200
    assert restored.json()["final_video"]["status"] == "Ready"

def test_merger_rejects_tampered_acceptance_without_qc_evidence(tmp_path: Path) -> None:
    storage = ProjectStorage(tmp_path / "projects")
    project = completed_project(storage, tmp_path)
    scene = project.scenes[0]
    scene.visual_qc.status = "Failed"
    scene.acceptance.status = "Accepted"
    scene.status = "Accepted"

    with pytest.raises(VideoMergeError, match="chưa có tệp video hoàn chỉnh"):
        VideoMerger(tmp_path).clips_for(project)


def test_merger_rejects_low_visual_component_even_when_acceptance_flag_is_accepted(
    tmp_path: Path,
) -> None:
    storage = ProjectStorage(tmp_path / "projects")
    project = completed_project(storage, tmp_path)
    scene = project.scenes[0]
    scene.visual_qc.prop_consistency = 40
    scene.visual_qc.score = 96
    scene.acceptance.status = "Accepted"
    scene.acceptance.score = 96

    with pytest.raises(VideoMergeError, match="chưa có tệp video hoàn chỉnh"):
        VideoMerger(tmp_path).clips_for(project)
