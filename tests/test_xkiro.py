import json
import re
import sqlite3
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from flow_story_studio.analysis_providers.xkiro import XKiroClient, XKiroError
from flow_story_studio.main import create_app
from flow_story_studio.models import AnalyzeRequest, VideoSettings
from flow_story_studio.storage import ProjectStorage

TEXT = (
    "Người đàn ông bước vào cửa hàng và đặt điện thoại lên bàn. "
    "Sau đó người phụ nữ tiến đến cửa sổ rồi nói chuyện với anh."
)


def scene_result(scene_id: str) -> dict:
    return {
        "id": scene_id,
        "action": (
            "Người đàn ông bước vào và đặt điện thoại xuống có chủ đích."
            if scene_id == "SCENE_001"
            else f"Hành động điện ảnh cho {scene_id}."
        ),
        "summary": f"Nội dung {scene_id}",
        "characters": ["CHAR_001"],
        "location_id": "LOC_001",
        "camera": "Stable cinematic camera",
        "lighting": "Motivated natural light",
        "atmosphere": "Coherent dramatic atmosphere",
        "voiceover": "",
        "dialogues": [],
        "start_state": {
            "character_positions": {"CHAR_001": f"start {scene_id}"},
            "character_wardrobe": {"CHAR_001": "locked wardrobe"},
            "prop_positions": {},
            "time": f"start {scene_id}",
            "weather": "stable",
            "camera": "stable axis",
            "notes": "",
        },
        "end_state": {
            "character_positions": {"CHAR_001": f"end {scene_id}"},
            "character_wardrobe": {"CHAR_001": "locked wardrobe"},
            "prop_positions": {},
            "time": f"end {scene_id}",
            "weather": "stable",
            "camera": "stable axis",
            "notes": "approved",
        },
    }


def requested_scene_ids(prompt: str) -> list[str]:
    scene_section = prompt.split("SCENES TO RETURN", 1)[-1]
    return list(dict.fromkeys(re.findall(r"SCENE_\d+", scene_section)))


def transport(chat_hook=None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {
                            "id": "qwen/test-free",
                            "object": "model",
                            "display_name": "Test Free",
                            "owned_by": "qwen",
                            "access_tier": "free",
                            "context_length": 128000,
                            "max_output_tokens": 16000,
                            "pricing": {"input": 0, "output": 0},
                            "capabilities": {"reasoning": True, "vision": False},
                            "additive_future_field": "ignored",
                        },
                        {
                            "id": "openai/test-paid",
                            "display_name": "Test Paid",
                            "owned_by": "openai",
                            "access_tier": "paid",
                        },
                    ],
                },
            )
        if request.url.path == "/v1/usage":
            if request.headers.get("Authorization") == "Bearer sk-xt-valid":
                return httpx.Response(200, json={"free_tokens_remaining": 1000})
            return httpx.Response(401, json={"error": {"message": "Invalid API key."}})
        if request.url.path == "/v1/chat/completions":
            body = json.loads(request.content)
            assert body["model"] in {"qwen/test-free", "openai/test-paid"}
            if chat_hook:
                hooked = chat_hook(request, body)
                if hooked is not None:
                    return hooked
            if body["model"] == "openai/test-paid" and "response_format" in body:
                return httpx.Response(
                    400, json={"error": {"message": "response_format is not supported"}}
                )
            prompt = body["messages"][-1]["content"]
            scene_ids = requested_scene_ids(prompt) if "SCENES TO RETURN" in prompt else []
            result = {
                "story_bible": {
                    "main_theme": "Cuộc gặp gỡ bí ẩn trong cửa hàng",
                    "tone": "Trường mở rộng từ model tổng quát phải được bỏ qua",
                },
                "master_prompt": "A single coherent cinematic world.",
            }
            if scene_ids:
                result["scenes"] = [scene_result(scene_id) for scene_id in scene_ids]
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"role": "assistant", "content": json.dumps(result)}}]
                },
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_xkiro_connect_catalog_and_analysis() -> None:
    client = XKiroClient(transport=transport())
    disconnected = await client.status(include_models=True)
    assert not disconnected.configured
    assert disconnected.models == []
    connection = await client.connect("sk-xt-valid")
    assert connection.configured
    assert connection.free_model_count == 1
    assert connection.model_count == 2
    assert {item.id for item in connection.models} == {
        "qwen/test-free",
        "openai/test-paid",
    }

    project = await client.analyze(
        AnalyzeRequest(
            name="xKiro",
            original_text=TEXT,
            settings=VideoSettings(analysis_provider="xkiro", analysis_model="qwen/test-free"),
        )
    )
    assert project.story_bible.main_theme == "Cuộc gặp gỡ bí ẩn trong cửa hàng"
    assert "có chủ đích" in project.scenes[0].flow_prompt
    assert project.timeline[-1] == "Story analysis provider: xKiro · qwen/test-free"


@pytest.mark.asyncio
async def test_xkiro_accepts_paid_model_and_retries_compatible_payload() -> None:
    client = XKiroClient(transport=transport())
    await client.connect("sk-xt-valid")
    logs: list[tuple[str, str]] = []
    project = await client.analyze(
        AnalyzeRequest(
            name="Any catalog model",
            original_text=TEXT,
            settings=VideoSettings(analysis_provider="xkiro", analysis_model="openai/test-paid"),
        ),
        progress=lambda message, level: logs.append((level, message)),
    )
    assert project.settings.analysis_model == "openai/test-paid"
    assert any("giao thức tương thích" in message for _, message in logs)
    assert logs[-1][0] == "success"


@pytest.mark.asyncio
async def test_xkiro_batches_long_screenplay_and_locks_every_scene() -> None:
    client = XKiroClient(transport=transport())
    await client.connect("sk-xt-valid")
    long_story = " ".join(
        f"Sau đó cô gái thực hiện hành động thứ {index} trong căn phòng." for index in range(1, 34)
    )
    logs: list[tuple[str, str]] = []
    project = await client.analyze(
        AnalyzeRequest(
            name="Long screenplay",
            original_text=long_story,
            settings=VideoSettings(analysis_provider="xkiro", analysis_model="qwen/test-free"),
        ),
        progress=lambda message, level: logs.append((level, message)),
    )

    batch_logs = [message for _, message in logs if "phân cảnh lô" in message]
    assert len(project.scenes) > 24
    assert len(batch_logs) >= 3
    assert all(scene.ai_locked for scene in project.scenes)


@pytest.mark.asyncio
async def test_xkiro_accepts_minimax_top_level_scene_and_repairs_missing_sequentially() -> None:
    response_count = 0

    def minimax_single_scene(_request: httpx.Request, body: dict) -> httpx.Response | None:
        nonlocal response_count
        prompt = body["messages"][-1]["content"]
        if "SCENES TO RETURN" not in prompt:
            return None
        scene_ids = requested_scene_ids(prompt)
        response_count += 1
        payload = scene_result(scene_ids[0])
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": json.dumps(payload)}}]},
        )

    client = XKiroClient(transport=transport(minimax_single_scene))
    await client.connect("sk-xt-valid")
    logs: list[tuple[str, str]] = []
    project = await client.analyze(
        AnalyzeRequest(
            name="MiniMax singular schema",
            original_text=" ".join(
                f"Sau đó cô gái thực hiện hành động thứ {index} trong căn phòng."
                for index in range(1, 9)
            ),
            settings=VideoSettings(analysis_provider="xkiro", analysis_model="qwen/test-free"),
        ),
        progress=lambda message, level: logs.append((level, message)),
    )

    assert len(project.scenes) == 8
    assert response_count == 8
    assert any("object cảnh đơn ở top-level" in message for _, message in logs)
    assert any("sửa cấu trúc thành công" in message for _, message in logs)
    for previous, current in zip(project.scenes, project.scenes[1:], strict=False):
        assert current.start_state == previous.end_state


@pytest.mark.asyncio
async def test_xkiro_changes_repair_prompt_to_escape_cached_nested_object() -> None:
    repair_attempts: list[str] = []

    def cached_dialogue_then_scene(_request: httpx.Request, body: dict) -> httpx.Response | None:
        prompt = body["messages"][-1]["content"]
        if "SCENES TO RETURN" not in prompt:
            return None
        scene_ids = requested_scene_ids(prompt)
        if len(scene_ids) > 1:
            payload = scene_result(scene_ids[0])
        elif "STRICT SINGLE-SCENE REPAIR ATTEMPT" in prompt:
            repair_attempts.append(prompt)
            payload = (
                {"character_id": "CHAR_001", "text": "nested", "emotion": "tense"}
                if "ATTEMPT 1" in prompt
                else scene_result(scene_ids[0])
            )
        else:
            payload = scene_result(scene_ids[0])
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(payload)}}]},
        )

    client = XKiroClient(transport=transport(cached_dialogue_then_scene))
    await client.connect("sk-xt-valid")
    project = await client.analyze(
        AnalyzeRequest(
            name="Unique repair prompts",
            original_text=TEXT,
            settings=VideoSettings(analysis_provider="xkiro", analysis_model="qwen/test-free"),
        )
    )

    assert project.scenes
    assert len(repair_attempts) == 2
    assert "ATTEMPT 1" in repair_attempts[0]
    assert "ATTEMPT 2" in repair_attempts[1]


@pytest.mark.asyncio
async def test_xkiro_checkpoints_each_repaired_scene_before_later_failure(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("XKIRO_STRUCTURE_REPAIR_ATTEMPTS", "2")
    checkpoint_root = tmp_path / "analysis-checkpoints"

    def fail_fourth_scene(_request: httpx.Request, body: dict) -> httpx.Response | None:
        prompt = body["messages"][-1]["content"]
        if "SCENES TO RETURN" not in prompt:
            return None
        scene_ids = requested_scene_ids(prompt)
        payload = (
            {"character_id": "CHAR_001", "text": "wrong root", "emotion": "tense"}
            if scene_ids == ["SCENE_004"]
            else scene_result(scene_ids[0])
        )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(payload)}}]},
        )

    request = AnalyzeRequest(
        name="Per-scene checkpoint",
        original_text=" ".join(
            f"Sau đó cô gái thực hiện hành động thứ {index} trong căn phòng."
            for index in range(1, 7)
        ),
        settings=VideoSettings(analysis_provider="xkiro", analysis_model="qwen/test-free"),
    )
    first = XKiroClient(transport=transport(fail_fourth_scene), checkpoint_root=checkpoint_root)
    await first.connect("sk-xt-valid")
    with pytest.raises(XKiroError, match="SCENE_004"):
        await first.analyze(request)

    checkpoint_file = next(checkpoint_root.glob("*.sqlite3"))
    with sqlite3.connect(checkpoint_file) as connection:
        saved_ids = {
            row[0]
            for row in connection.execute(
                "SELECT scene_id FROM checkpoint_scenes ORDER BY scene_id"
            )
        }
    assert saved_ids == {"SCENE_001", "SCENE_002", "SCENE_003"}

    resumed = XKiroClient(transport=transport(), checkpoint_root=checkpoint_root)
    await resumed.connect("sk-xt-valid")
    logs: list[tuple[str, str]] = []
    project = await resumed.analyze(
        request, progress=lambda message, level: logs.append((level, message))
    )
    assert len(project.scenes) == 6
    assert any("checkpoint từng cảnh: 3/6" in message for _, message in logs)


@pytest.mark.asyncio
async def test_xkiro_rejects_bad_key() -> None:
    client = XKiroClient(transport=transport())
    with pytest.raises(XKiroError, match="không hợp lệ"):
        await client.connect("sk-xt-invalid")


def test_xkiro_api_routes(tmp_path: Path) -> None:
    client = XKiroClient(transport=transport())
    app = create_app(ProjectStorage(tmp_path / "projects"), xkiro_client=client)
    with TestClient(app) as test_client:
        models = test_client.get("/api/ai/xkiro/models")
        assert models.status_code == 200
        assert {item["id"] for item in models.json()} == {
            "qwen/test-free",
            "openai/test-paid",
        }

        connected = test_client.post("/api/ai/xkiro/connect", json={"api_key": "sk-xt-valid"})
        assert connected.status_code == 200
        assert connected.json()["key_hint"] == "••••alid"

        started = test_client.post(
            "/api/analysis/jobs",
            json={
                "name": "Logged analysis",
                "original_text": TEXT,
                "settings": {
                    "analysis_provider": "xkiro",
                    "analysis_model": "qwen/test-free",
                },
            },
        )
        assert started.status_code == 202
        job_id = started.json()["id"]
        for _ in range(100):
            job = test_client.get(f"/api/analysis/jobs/{job_id}").json()
            if job["status"] in {"completed", "failed"}:
                break
            time.sleep(0.01)
        assert job["status"] == "completed"
        assert job["project"]["scenes"]
        assert any(entry["level"] == "success" for entry in job["logs"])
        assert not list((tmp_path / "analysis-checkpoints").glob("*.sqlite3"))


def test_xkiro_wraps_top_level_json_array_as_scenes() -> None:
    parsed = XKiroClient._parse_json(json.dumps([scene_result("SCENE_001")]))
    assert parsed["scenes"][0]["id"] == "SCENE_001"


@pytest.mark.asyncio
async def test_xkiro_key_is_encrypted_and_remembered(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("XKIRO_API_KEY", raising=False)
    credential_path = tmp_path / "shared-credentials" / "xkiro-api-key.bin"
    first = XKiroClient(transport=transport(), credential_path=credential_path)

    connected = await first.connect("sk-xt-valid")
    assert connected.source == "stored"
    assert credential_path.is_file()
    assert b"sk-xt-valid" not in credential_path.read_bytes()

    restored = XKiroClient(transport=transport(), credential_path=credential_path)
    status = await restored.status()
    assert status.configured is True
    assert status.source == "stored"
    assert status.key_hint == "••••alid"

    restored.disconnect()
    assert not credential_path.exists()
    assert not (await restored.status()).configured


@pytest.mark.asyncio
async def test_xkiro_retries_transient_upstream_failure(monkeypatch) -> None:
    monkeypatch.setenv("XKIRO_REQUEST_RETRIES", "2")
    monkeypatch.setenv("XKIRO_RETRY_BACKOFF", "0")
    failures = 0

    def fail_once(_request: httpx.Request, body: dict) -> httpx.Response | None:
        nonlocal failures
        prompt = body["messages"][-1]["content"]
        if "SCENES TO RETURN" in prompt and failures == 0:
            failures += 1
            return httpx.Response(503, json={"error": {"message": "temporary overload"}})
        return None

    client = XKiroClient(transport=transport(fail_once))
    await client.connect("sk-xt-valid")
    logs: list[tuple[str, str]] = []
    project = await client.analyze(
        AnalyzeRequest(
            name="Transient retry",
            original_text=TEXT,
            settings=VideoSettings(analysis_provider="xkiro", analysis_model="qwen/test-free"),
        ),
        progress=lambda message, level: logs.append((level, message)),
    )

    assert project.scenes
    assert failures == 1
    assert any("tự thử lại" in message for _, message in logs)


@pytest.mark.asyncio
async def test_xkiro_waits_for_duplicate_in_progress_without_spending_retry(
    monkeypatch,
) -> None:
    monkeypatch.setenv("XKIRO_REQUEST_RETRIES", "2")
    monkeypatch.setenv("XKIRO_RETRY_BACKOFF", "0")
    monkeypatch.setenv("XKIRO_DUPLICATE_WAIT_LIMIT", "3")
    monkeypatch.setenv("XKIRO_DUPLICATE_WAIT_SECONDS", "0")
    scene_calls = 0

    def delayed_original(_request: httpx.Request, body: dict) -> httpx.Response | None:
        nonlocal scene_calls
        if "SCENES TO RETURN" not in body["messages"][-1]["content"]:
            return None
        scene_calls += 1
        if scene_calls == 1:
            return httpx.Response(502, json={"error": {"message": "gateway timeout"}})
        if scene_calls in {2, 3}:
            return httpx.Response(
                409,
                json={
                    "error": {
                        "message": (
                            "A duplicate request is already being processed — please try again."
                        )
                    }
                },
            )
        return None

    client = XKiroClient(transport=transport(delayed_original))
    await client.connect("sk-xt-valid")
    logs: list[tuple[str, str]] = []
    project = await client.analyze(
        AnalyzeRequest(
            name="Duplicate wait",
            original_text=TEXT,
            settings=VideoSettings(analysis_provider="xkiro", analysis_model="qwen/test-free"),
        ),
        progress=lambda message, level: logs.append((level, message)),
    )

    assert project.scenes
    assert scene_calls == 4
    assert any("yêu cầu gốc vẫn đang xử lý" in message for _, message in logs)


@pytest.mark.asyncio
async def test_xkiro_rotates_signature_after_stale_duplicate(monkeypatch) -> None:
    monkeypatch.setenv("XKIRO_REQUEST_RETRIES", "1")
    monkeypatch.setenv("XKIRO_DUPLICATE_WAIT_LIMIT", "2")
    monkeypatch.setenv("XKIRO_DUPLICATE_WAIT_SECONDS", "0")
    monkeypatch.setenv("XKIRO_DUPLICATE_RECOVERY_LIMIT", "1")
    calls = 0

    def stale_until_recovery(_request: httpx.Request, body: dict) -> httpx.Response | None:
        nonlocal calls
        prompt = body["messages"][-1]["content"]
        if "SCENES TO RETURN" not in prompt:
            return None
        calls += 1
        if "TRANSPORT RECOVERY TOKEN" not in prompt:
            return httpx.Response(
                409,
                json={
                    "error": {
                        "message": (
                            "A duplicate request is already being processed — please try again."
                        )
                    }
                },
            )
        return None

    client = XKiroClient(transport=transport(stale_until_recovery))
    await client.connect("sk-xt-valid")
    logs: list[tuple[str, str]] = []
    project = await client.analyze(
        AnalyzeRequest(
            name="Stale duplicate recovery",
            original_text=TEXT,
            settings=VideoSettings(analysis_provider="xkiro", analysis_model="qwen/test-free"),
        ),
        progress=lambda message, level: logs.append((level, message)),
    )

    assert project.scenes
    assert calls == 3
    assert any("chữ ký phục hồi" in message for _, message in logs)


@pytest.mark.asyncio
async def test_xkiro_adapts_to_max_completion_tokens_models() -> None:
    rejected_legacy_tokens = 0

    def require_modern_tokens(_request: httpx.Request, body: dict) -> httpx.Response | None:
        nonlocal rejected_legacy_tokens
        if "max_tokens" in body:
            rejected_legacy_tokens += 1
            return httpx.Response(400, json={"error": {"message": "use max_completion_tokens"}})
        return None

    client = XKiroClient(transport=transport(require_modern_tokens))
    await client.connect("sk-xt-valid")
    project = await client.analyze(
        AnalyzeRequest(
            name="Modern token protocol",
            original_text=TEXT,
            settings=VideoSettings(analysis_provider="xkiro", analysis_model="qwen/test-free"),
        )
    )

    assert project.scenes
    assert rejected_legacy_tokens >= 2


@pytest.mark.asyncio
async def test_xkiro_resumes_checkpoint_and_chains_real_ai_states(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("XKIRO_REQUEST_RETRIES", "1")
    monkeypatch.setenv("XKIRO_RETRY_BACKOFF", "0")
    monkeypatch.setenv("XKIRO_SCENE_BATCH_SIZE", "2")
    checkpoint_root = tmp_path / "analysis-checkpoints"
    first_scene_calls = 0

    def interrupt_second_batch(_request: httpx.Request, body: dict) -> httpx.Response | None:
        nonlocal first_scene_calls
        prompt = body["messages"][-1]["content"]
        if "SCENES TO RETURN" not in prompt:
            return None
        first_scene_calls += 1
        if first_scene_calls == 2:
            return httpx.Response(503, json={"error": {"message": "temporary outage"}})
        return None

    request = AnalyzeRequest(
        name="Durable checkpoint",
        original_text=" ".join(
            f"Sau đó cô gái thực hiện hành động thứ {index} trong căn phòng."
            for index in range(1, 9)
        ),
        settings=VideoSettings(analysis_provider="xkiro", analysis_model="qwen/test-free"),
    )
    first = XKiroClient(
        transport=transport(interrupt_second_batch), checkpoint_root=checkpoint_root
    )
    await first.connect("sk-xt-valid")
    with pytest.raises(XKiroError, match="temporary outage"):
        await first.analyze(request)

    checkpoint_files = list(checkpoint_root.glob("*.sqlite3"))
    assert len(checkpoint_files) == 1
    with sqlite3.connect(checkpoint_files[0]) as connection:
        saved_scene_count = connection.execute("SELECT COUNT(*) FROM checkpoint_scenes").fetchone()[
            0
        ]
    assert saved_scene_count == 2
    resumed_scene_calls = 0

    def count_resumed_batches(_request: httpx.Request, body: dict) -> None:
        nonlocal resumed_scene_calls
        if "SCENES TO RETURN" in body["messages"][-1]["content"]:
            resumed_scene_calls += 1
        return None

    second = XKiroClient(
        transport=transport(count_resumed_batches), checkpoint_root=checkpoint_root
    )
    await second.connect("sk-xt-valid")
    logs: list[tuple[str, str]] = []
    project = await second.analyze(
        request, progress=lambda message, level: logs.append((level, message))
    )

    assert len(project.scenes) == 8
    assert resumed_scene_calls == 3
    assert any("Đã khôi phục checkpoint" in message for _, message in logs)
    assert any("đã có checkpoint; bỏ qua" in message for _, message in logs)
    for previous, current in zip(project.scenes, project.scenes[1:], strict=False):
        assert current.start_state == previous.end_state


@pytest.mark.asyncio
async def test_xkiro_scales_to_a_multi_hour_thousand_scene_project(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("XKIRO_SCENE_BATCH_SIZE", "8")
    scene_calls = 0

    def count_scene_batches(_request: httpx.Request, body: dict) -> None:
        nonlocal scene_calls
        if "SCENES TO RETURN" in body["messages"][-1]["content"]:
            scene_calls += 1
        return None

    request = AnalyzeRequest(
        name="Multi-hour screenplay",
        original_text=" ".join(
            f"Sau đó cô gái thực hiện hành động thứ {index} trong căn phòng."
            for index in range(1, 1001)
        ),
        settings=VideoSettings(
            scene_duration=8,
            analysis_provider="xkiro",
            analysis_model="qwen/test-free",
        ),
    )
    client = XKiroClient(
        transport=transport(count_scene_batches),
        checkpoint_root=tmp_path / "analysis-checkpoints",
    )
    await client.connect("sk-xt-valid")
    project = await client.analyze(request)

    assert len(project.scenes) == 1000
    assert scene_calls == 125
    assert project.continuity_score == 100
    assert project.scenes[-1].start_state == project.scenes[-2].end_state
