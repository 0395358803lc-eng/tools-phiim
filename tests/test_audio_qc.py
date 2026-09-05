import pytest

from flow_story_studio import audio_qc
from flow_story_studio.audio_qc import AudioQCAnalyzer
from flow_story_studio.engines.analyzer import analyze_story
from flow_story_studio.models import AnalyzeRequest

SCRIPT = (
    "Alex bước vào phòng, đặt vé lên bàn và nói một câu ngắn. "
    "Sau đó anh bước tới cửa sổ."
)


def _project_and_scene(tmp_path):
    project = analyze_story(AnalyzeRequest(name="audio-qc", original_text=SCRIPT))
    scene = project.scenes[0]
    relative = f"renders/{project.id}/{scene.id}/scene.mp4"
    target = tmp_path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"stub-media")
    scene.result_file = relative
    scene.orchestration["audio_locks"] = {
        "target_lufs": -16.0,
        "true_peak_db": -1.0,
        "loudness_tolerance_lu": 4.0,
    }
    return project, scene


@pytest.mark.asyncio
async def test_audio_qc_passes_canonical_loudness(monkeypatch, tmp_path) -> None:
    project, scene = _project_and_scene(tmp_path)

    async def fake_measure(_ffmpeg, _video):
        return (
            "Stream #0:1: Audio: aac, 48000 Hz, stereo\n"
            "I: -16.2 LUFS\n"
            "Peak: -1.4 dBFS\n",
            0,
        )

    monkeypatch.setattr(audio_qc, "ffmpeg_path", lambda: "ffmpeg")
    monkeypatch.setattr(audio_qc, "_measure_audio", fake_measure)

    report = await AudioQCAnalyzer(tmp_path).inspect_scene(project, scene)

    assert report.status == "Passed"
    assert report.audio_present is True
    assert report.sample_rate_hz == 48_000
    assert report.channels == 2
    assert report.integrated_lufs == pytest.approx(-16.2)
    assert report.true_peak_db == pytest.approx(-1.4)
    assert report.clipping_detected is False


@pytest.mark.asyncio
async def test_audio_qc_rejects_missing_audio(monkeypatch, tmp_path) -> None:
    project, scene = _project_and_scene(tmp_path)

    async def fake_measure(_ffmpeg, _video):
        return ("Stream #0:0: Video: h264\n", 0)

    monkeypatch.setattr(audio_qc, "ffmpeg_path", lambda: "ffmpeg")
    monkeypatch.setattr(audio_qc, "_measure_audio", fake_measure)

    report = await AudioQCAnalyzer(tmp_path).inspect_scene(project, scene)

    assert report.status == "Failed"
    assert report.audio_present is False
    assert any(issue.code == "AUDIO_STREAM_MISSING" for issue in report.issues)


@pytest.mark.asyncio
async def test_audio_qc_rejects_loudness_and_peak_drift(monkeypatch, tmp_path) -> None:
    project, scene = _project_and_scene(tmp_path)

    async def fake_measure(_ffmpeg, _video):
        return (
            "Stream #0:1: Audio: aac, 48000 Hz, stereo\n"
            "I: -28.0 LUFS\n"
            "Peak: 0.0 dBFS\n",
            0,
        )

    monkeypatch.setattr(audio_qc, "ffmpeg_path", lambda: "ffmpeg")
    monkeypatch.setattr(audio_qc, "_measure_audio", fake_measure)

    report = await AudioQCAnalyzer(tmp_path).inspect_scene(project, scene)

    assert report.status == "Failed"
    assert report.clipping_detected is True
    codes = {issue.code for issue in report.issues}
    assert "LOUDNESS_OUT_OF_RANGE" in codes
    assert "TRUE_PEAK_TOO_HIGH" in codes


@pytest.mark.asyncio
async def test_audio_qc_rejects_missing_or_unsafe_video_path(tmp_path) -> None:
    project, scene = _project_and_scene(tmp_path)
    scene.result_file = "missing.mp4"
    report = await AudioQCAnalyzer(tmp_path).inspect_scene(project, scene)
    assert report.status == "Unavailable"
    assert report.issues[0].code == "AUDIO_VIDEO_MISSING"

    outside = tmp_path.parent / "outside-audio-qc.mp4"
    outside.write_bytes(b"outside")
    scene.result_file = "../outside-audio-qc.mp4"
    report = await AudioQCAnalyzer(tmp_path).inspect_scene(project, scene)
    assert report.status == "Unavailable"
    assert report.issues[0].code == "AUDIO_VIDEO_MISSING"


@pytest.mark.asyncio
async def test_audio_qc_fails_closed_without_ffmpeg(monkeypatch, tmp_path) -> None:
    project, scene = _project_and_scene(tmp_path)
    monkeypatch.setattr(audio_qc, "ffmpeg_path", lambda: None)

    report = await AudioQCAnalyzer(tmp_path).inspect_scene(project, scene)

    assert report.status == "Unavailable"
    assert report.issues[0].code == "FFMPEG_UNAVAILABLE"


@pytest.mark.asyncio
async def test_audio_qc_reports_measurement_failure(monkeypatch, tmp_path) -> None:
    project, scene = _project_and_scene(tmp_path)

    async def fake_measure(_ffmpeg, _video):
        return "Stream #0:1: Audio: aac, 48000 Hz, stereo\n", 1

    monkeypatch.setattr(audio_qc, "ffmpeg_path", lambda: "ffmpeg")
    monkeypatch.setattr(audio_qc, "_measure_audio", fake_measure)

    report = await AudioQCAnalyzer(tmp_path).inspect_scene(project, scene)

    assert report.status == "Unavailable"
    assert report.audio_present is True
    assert report.sample_rate_hz == 48_000
    assert report.channels == 2
    assert report.issues[0].code == "AUDIO_MEASUREMENT_FAILED"


@pytest.mark.asyncio
async def test_audio_qc_rejects_missing_loudness_and_low_sample_rate(
    monkeypatch, tmp_path
) -> None:
    project, scene = _project_and_scene(tmp_path)

    async def fake_measure(_ffmpeg, _video):
        return (
            "Stream #0:1: Audio: aac, 32000 Hz, mono\n"
            "Peak: -2.0 dBFS\n",
            0,
        )

    monkeypatch.setattr(audio_qc, "ffmpeg_path", lambda: "ffmpeg")
    monkeypatch.setattr(audio_qc, "_measure_audio", fake_measure)

    report = await AudioQCAnalyzer(tmp_path).inspect_scene(project, scene)

    assert report.status == "Failed"
    assert report.sample_rate_hz == 32_000
    assert report.channels == 1
    codes = {issue.code for issue in report.issues}
    assert "LOUDNESS_UNAVAILABLE" in codes
    assert "AUDIO_SAMPLE_RATE_LOW" in codes


def test_audio_qc_parsers_and_invalid_targets_fall_back(tmp_path) -> None:
    project, scene = _project_and_scene(tmp_path)
    scene.orchestration["audio_locks"] = {
        "target_lufs": "invalid",
        "true_peak_db": float("inf"),
        "loudness_tolerance_lu": -5,
    }
    assert audio_qc._audio_targets(scene) == (-16.0, -1.0, 0.0)

    assert audio_qc._parse_audio_metadata(
        "Stream #0:1: Audio: aac, 48000 Hz, 5.1, fltp"
    ) == (True, 48_000, 6)
    assert audio_qc._parse_audio_metadata(
        "Stream #0:1: Audio: pcm_s16le, 96000 Hz, 4 channels"
    ) == (True, 96_000, 4)
    assert audio_qc._parse_audio_metadata("Stream #0:0: Video: h264") == (False, 0, 0)

    assert audio_qc._last_finite(r"I:\s*([-+]?\w+(?:\.\d+)?)", "I: -inf\nI: -16.5") == -16.5
    assert audio_qc._last_finite(r"I:\s*([-+]?\w+(?:\.\d+)?)", "I: nope") is None
