"""xKiro-backed story analysis with encrypted local credentials."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from ..credentials import CredentialVaultError, EncryptedCredentialVault
from ..engines.analyzer import analyze_story
from ..models import (
    AnalyzeRequest,
    Project,
    XKiroConnection,
    XKiroModel,
)
from .checkpoint import AnalysisCheckpointStore, CheckpointError
from .merging import merge_analysis
from .normalization import (
    chain_scene_states,
    extract_scene_items,
    looks_like_scene,
    normalize_scene_result,
    scene_payload_shape,
    valid_scene_result,
)
from .parsing import message_content, parse_json_object
from .prompting import (
    analysis_prompt,
    draft_world,
    merge_world,
    scene_prompt,
    split_source,
    world_prompt,
)
from .transport_helpers import (
    build_completion_variants,
    inject_recovery_token,
    is_duplicate_in_progress,
    retry_delay,
)

BASE_URL = "https://api.xkiro.com"
CATALOG_TTL_SECONDS = 300
SCENE_SCHEMA_VERSION = 2
TRANSIENT_HTTP_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
ProgressCallback = Callable[[str, str], None]
SceneCheckpointCallback = Callable[[str, dict[str, Any]], Awaitable[None]]


SYSTEM_PROMPT = """You are a senior film story analyst and continuity supervisor.
Analyze the complete source before editing any scene. Return valid JSON only. Preserve the source
meaning and chronology. Build a single coherent story world: stable character identity and
wardrobe, stable locations, object permanence, motivated camera coverage, and start/end states
that chain between scenes. Never treat Markdown headings, production metadata, camera notes,
voice labels or section labels as characters or visual scenes. Do not include Markdown fences."""


class XKiroError(RuntimeError):
    """Safe public xKiro integration error."""


class XKiroClient:
    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        credential_path: Path | None = None,
        checkpoint_root: Path | None = None,
    ) -> None:
        env_key = os.getenv("XKIRO_API_KEY", "").strip()
        self._vault = EncryptedCredentialVault(credential_path) if credential_path else None
        stored_key = ""
        if self._vault:
            try:
                stored_key = str(self._vault.load().get("api_key", "")).strip()
            except CredentialVaultError:
                # A damaged old vault must not prevent the desktop from starting;
                # connecting a new key will atomically replace it.
                stored_key = ""
        self._api_key = stored_key or env_key or None
        self._source = "stored" if stored_key else "environment" if env_key else "none"
        self._transport = transport
        self._checkpoint_store = AnalysisCheckpointStore(checkpoint_root)
        self._catalog: list[XKiroModel] = []
        self._catalog_at = 0.0

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    def _client(self, timeout: float | httpx.Timeout | None = 30) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=timeout,
            transport=self._transport,
            headers={"Accept": "application/json"},
        )

    def set_checkpoint_root(self, root: Path) -> None:
        """Attach workspace-local durable analysis state to this client."""
        self._checkpoint_store.set_root(root)

    @staticmethod
    def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(os.getenv(name, str(default)))
        except ValueError:
            value = default
        return max(minimum, min(maximum, value))

    @staticmethod
    def _safe_error(response: httpx.Response) -> str:
        try:
            payload = response.json()
            message = payload.get("error", {}).get("message") or payload.get("message")
            if message:
                return str(message)[:500]
        except (ValueError, AttributeError):
            pass
        return f"xKiro trả về HTTP {response.status_code}"

    async def list_models(self, free_only: bool = True, refresh: bool = False) -> list[XKiroModel]:
        now = time.monotonic()
        if refresh or not self._catalog or now - self._catalog_at > CATALOG_TTL_SECONDS:
            try:
                async with self._client(timeout=15) as client:
                    response = await client.get("/v1/models")
                response.raise_for_status()
                data = response.json().get("data", [])
                self._catalog = [
                    XKiroModel.model_validate(
                        {
                            "id": item["id"],
                            "display_name": item.get("display_name") or item["id"],
                            "owned_by": item.get("owned_by") or item["id"].split("/", 1)[0],
                            "access_tier": item.get("access_tier", "paid"),
                            "context_length": item.get("context_length"),
                            "max_output_tokens": item.get("max_output_tokens"),
                            "pricing": item.get("pricing") or {},
                            "capabilities": item.get("capabilities") or {},
                        }
                    )
                    for item in data
                    if isinstance(item, dict) and item.get("id")
                ]
                self._catalog_at = now
            except (httpx.HTTPError, ValueError) as exc:
                raise XKiroError("Không thể tải catalog model từ xKiro") from exc
        models = [item for item in self._catalog if not free_only or item.access_tier == "free"]
        return sorted(
            models,
            key=lambda item: (item.owned_by.casefold(), item.display_name.casefold()),
        )

    async def connect(self, api_key: str) -> XKiroConnection:
        candidate = api_key.strip()
        if not candidate:
            raise XKiroError("API key xKiro đang trống")
        try:
            async with self._client(timeout=20) as client:
                response = await client.get(
                    "/v1/usage", headers={"Authorization": f"Bearer {candidate}"}
                )
            if response.status_code == 401:
                raise XKiroError("API key xKiro không hợp lệ hoặc đã bị thu hồi")
            if response.is_error:
                raise XKiroError(self._safe_error(response))
        except httpx.HTTPError as exc:
            raise XKiroError("Không thể xác thực API key với xKiro") from exc
        if self._vault:
            try:
                self._vault.save({"api_key": candidate})
            except CredentialVaultError as exc:
                raise XKiroError("API key hợp lệ nhưng không thể lưu an toàn trên máy") from exc
            self._api_key = candidate
            self._source = "stored"
        else:
            self._api_key = candidate
            self._source = "session"
        return await self.status(include_models=True, refresh=True)

    def disconnect(self) -> None:
        if self._vault:
            try:
                self._vault.clear()
            except CredentialVaultError as exc:
                raise XKiroError("Không thể xóa API key xKiro đã lưu") from exc
        self._api_key = None
        self._source = "none"

    async def status(
        self, *, include_models: bool = False, refresh: bool = False
    ) -> XKiroConnection:
        models = (
            await self.list_models(free_only=False, refresh=refresh)
            if include_models and self.configured
            else []
        )
        return XKiroConnection(
            configured=self.configured,
            key_hint=(f"••••{self._api_key[-4:]}" if self._api_key else ""),
            source=self._source,  # type: ignore[arg-type]
            free_model_count=sum(item.access_tier == "free" for item in models),
            model_count=len(models),
            models=models,
        )

    async def analyze(
        self, request: AnalyzeRequest, progress: ProgressCallback | None = None
    ) -> Project:
        def emit(message: str, level: str = "info") -> None:
            if progress:
                progress(message, level)

        if not self._api_key:
            raise XKiroError("Chưa kết nối API key xKiro")
        emit("Đang đồng bộ catalog model từ xKiro")
        models = await self.list_models(free_only=False)
        available = {item.id: item for item in models}
        model = request.settings.analysis_model
        if not model:
            raise XKiroError("Chưa chọn model phân tích xKiro")
        if model not in available:
            raise XKiroError("Model đã chọn không còn tồn tại trong catalog xKiro")

        model_info = available[model]
        emit(f"Đã chọn {model_info.display_name} ({model_info.access_tier})")
        draft = analyze_story(request)
        emit(
            f"Đã tạo khung: {len(draft.scenes)} cảnh, "
            f"{len(draft.characters)} nhân vật, {len(draft.locations)} bối cảnh"
        )
        configured_output = self._env_int("XKIRO_MAX_OUTPUT_TOKENS", 12000, 512, 65536)
        max_tokens = min(model_info.max_output_tokens or 16000, configured_output)
        request_timeout = self._env_int("XKIRO_REQUEST_TIMEOUT", 900, 120, 3600)
        checkpoint = await self._load_checkpoint(request, draft, model, emit)
        try:
            emit(
                "Pipeline dài hạn đã bật: không giới hạn thời gian tổng; "
                f"mỗi yêu cầu tối đa {request_timeout} giây"
            )
            analysis = await self._batched_analysis(
                request,
                draft,
                model_info,
                max_tokens,
                request_timeout,
                checkpoint,
                emit,
            )
            emit("JSON hợp lệ; đang hợp nhất continuity và tạo Flow prompt")
        except XKiroError:
            emit("Đã giữ checkpoint; lần chạy sau sẽ tiếp tục từ phần hoàn tất gần nhất", "warning")
            raise
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            emit("Phản hồi của model không thể chuyển thành JSON hợp lệ", "error")
            raise XKiroError("xKiro không trả về kết quả phân tích JSON hợp lệ") from exc
        project = self._merge_analysis(draft, analysis, model)
        emit(
            f"Hoàn tất: {len(project.scenes)} cảnh, continuity {project.continuity_score}%",
            "success",
        )
        return project

    async def clear_checkpoint(self, request: AnalyzeRequest) -> None:
        await self._checkpoint_store.clear(request)

    def _checkpoint_path(self, request: AnalyzeRequest) -> Path | None:
        return self._checkpoint_store.path_for(request)

    async def _load_checkpoint(
        self,
        request: AnalyzeRequest,
        draft: Project,
        model: str,
        emit: ProgressCallback,
    ) -> dict[str, Any]:
        return await self._checkpoint_store.load(request, draft, model, emit)

    async def _save_checkpoint(
        self,
        request: AnalyzeRequest,
        checkpoint: dict[str, Any],
        scene_ids: list[str] | None = None,
    ) -> None:
        try:
            await self._checkpoint_store.save(request, checkpoint, scene_ids)
        except CheckpointError as exc:
            raise XKiroError(str(exc)) from exc

    async def _request_json(
        self,
        model: str,
        prompt: str,
        max_tokens: int,
        request_timeout: int,
        emit: ProgressCallback,
        label: str,
    ) -> dict[str, Any]:
        json_attempts = self._env_int("XKIRO_JSON_REPAIR_ATTEMPTS", 2, 1, 4)
        current_prompt = prompt
        for attempt in range(1, json_attempts + 1):
            started = time.monotonic()
            emit(
                f"{label}: đang chờ model (lần JSON {attempt}/{json_attempts}, "
                f"tối đa {request_timeout} giây)"
            )
            response = await self._completion_request(
                {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": current_prompt},
                    ],
                    "temperature": 0.2,
                    "max_tokens": max_tokens,
                },
                request_timeout,
                emit,
                label,
            )
            try:
                result = response.json()
                content = message_content(result["choices"][0]["message"])
                parsed = parse_json_object(content)
                emit(f"{label}: nhận JSON hợp lệ sau {int(time.monotonic() - started)} giây")
                return parsed
            except (ValueError, KeyError, IndexError, TypeError):
                if attempt >= json_attempts:
                    raise
                emit(f"{label}: JSON chưa hợp lệ; yêu cầu model tự sửa", "warning")
                current_prompt = (
                    prompt
                    + "\n\nIMPORTANT RETRY: The previous answer was not a parseable JSON object. "
                    "Return the requested object only, with double-quoted keys and strings, no "
                    "Markdown, comments, prose or trailing commas."
                )
        raise ValueError("model response is not valid JSON")

    async def _batched_analysis(
        self,
        request: AnalyzeRequest,
        draft: Project,
        model_info: XKiroModel,
        max_tokens: int,
        request_timeout: int,
        checkpoint: dict[str, Any],
        emit: ProgressCallback,
    ) -> dict[str, Any]:
        model = model_info.id
        emit(
            f"Kịch bản {len(draft.scenes)} cảnh: đang xây Story Bible theo từng phần có checkpoint",
        )
        context_length = model_info.context_length or 32000
        world_tokens = min(max_tokens, max(1800, min(6000, context_length // 8)))
        source_chunk_chars = self._env_int(
            "XKIRO_WORLD_CHUNK_CHARS",
            max(4000, min(60000, (context_length - world_tokens - 2500) * 2)),
            4000,
            100000,
        )
        chunks = self._split_source(request.original_text, source_chunk_chars)
        world = checkpoint.get("world")
        if not isinstance(world, dict):
            world = self._draft_world(draft)
        completed_chunks = min(int(checkpoint.get("world_chunks_completed") or 0), len(chunks))
        if completed_chunks:
            emit(f"Bỏ qua {completed_chunks}/{len(chunks)} phần Story Bible đã lưu")
        for chunk_index in range(completed_chunks, len(chunks)):
            label = f"Story Bible {chunk_index + 1}/{len(chunks)}"
            update = await self._request_json(
                model,
                self._world_prompt(world, chunks[chunk_index], chunk_index + 1, len(chunks)),
                world_tokens,
                request_timeout,
                emit,
                label,
            )
            world = self._merge_world(world, update)
            checkpoint["world"] = world
            checkpoint["world_chunks_completed"] = chunk_index + 1
            await self._save_checkpoint(request, checkpoint)
            emit(f"{label}: đã lưu checkpoint", "success")

        configured_batch = self._env_int("XKIRO_SCENE_BATCH_SIZE", 6, 1, 8)
        output_batch_limit = max(1, (max_tokens - 800) // 1100)
        context_batch_limit = max(1, (context_length - 5000) // 2200)
        batch_size = min(configured_batch, output_batch_limit, context_batch_limit)
        enriched_scenes: list[dict[str, Any]] = []
        batches = [
            draft.scenes[index : index + batch_size]
            for index in range(0, len(draft.scenes), batch_size)
        ]
        saved_scenes = checkpoint.get("scenes")
        if not isinstance(saved_scenes, dict):
            saved_scenes = {}
            checkpoint["scenes"] = saved_scenes
        previous_end_state: dict[str, Any] | None = None
        for batch_index, scenes in enumerate(batches, start=1):
            ids = [scene.id for scene in scenes]
            if all(scene_id in saved_scenes for scene_id in ids):
                returned = {scene_id: deepcopy(saved_scenes[scene_id]) for scene_id in ids}
                emit(f"Lô {batch_index}/{len(batches)} đã có checkpoint; bỏ qua")
            else:
                emit(
                    f"Đang AI duyệt phân cảnh lô {batch_index}/{len(batches)} ({ids[0]}–{ids[-1]})"
                )
                existing = {
                    scene_id: deepcopy(saved_scenes[scene_id])
                    for scene_id in ids
                    if scene_id in saved_scenes
                }
                if existing:
                    emit(
                        f"Lô {batch_index}/{len(batches)} có checkpoint từng cảnh: "
                        f"{len(existing)}/{len(ids)} đã xong"
                    )

                async def save_scene_checkpoint(scene_id: str, value: dict[str, Any]) -> None:
                    saved_scenes[scene_id] = deepcopy(value)
                    await self._save_checkpoint(request, checkpoint, [scene_id])

                scene_tokens = min(max_tokens, max(2800, 800 + len(scenes) * 1100))
                returned = await self._analyze_scene_batch(
                    model,
                    world,
                    scenes,
                    previous_end_state,
                    scene_tokens,
                    request_timeout,
                    emit,
                    batch_index,
                    len(batches),
                    existing,
                    save_scene_checkpoint,
                )
                saved_scenes.update(returned)
                await self._save_checkpoint(request, checkpoint, ids)
                emit(
                    f"Lô {batch_index}/{len(batches)} đã kiểm tra đủ {len(scenes)} cảnh "
                    "và lưu checkpoint",
                    "success",
                )
            ordered = [deepcopy(returned[scene.id]) for scene in scenes]
            previous_end_state = self._chain_scene_states(ordered, previous_end_state)
            enriched_scenes.extend(ordered)
        return {**world, "scenes": enriched_scenes}

    async def _analyze_scene_batch(
        self,
        model: str,
        world: dict[str, Any],
        scenes: list[Any],
        previous_end_state: dict[str, Any] | None,
        max_tokens: int,
        request_timeout: int,
        emit: ProgressCallback,
        batch_index: int,
        batch_count: int,
        existing: dict[str, dict[str, Any]] | None = None,
        checkpoint_scene: SceneCheckpointCallback | None = None,
    ) -> dict[str, dict[str, Any]]:
        collected = deepcopy(existing or {})
        structure_attempts = self._env_int("XKIRO_STRUCTURE_REPAIR_ATTEMPTS", 3, 1, 4)
        pending_scenes = [scene for scene in scenes if scene.id not in collected]
        if not pending_scenes:
            return collected
        request_anchor = deepcopy(previous_end_state)
        for scene in scenes:
            if scene.id not in collected:
                break
            request_anchor = deepcopy(collected[scene.id].get("end_state"))
        pending_ids = [scene.id for scene in pending_scenes]
        required_set = set(pending_ids)
        data = await self._request_json(
            model,
            self._scene_prompt(world, pending_scenes, request_anchor),
            max_tokens,
            request_timeout,
            emit,
            f"Lô cảnh {batch_index}/{batch_count}",
        )
        for item in self._extract_scene_items(data):
            normalized = self._normalize_scene_result(item, world)
            if self._valid_scene_result(normalized, required_set):
                collected[str(normalized["id"])] = normalized
                if checkpoint_scene:
                    await checkpoint_scene(str(normalized["id"]), normalized)

        missing = [scene_id for scene_id in pending_ids if scene_id not in collected]
        if not missing:
            return collected
        emit(
            f"Model trả {len(pending_ids) - len(missing)}/{len(pending_ids)} cảnh cần xử lý "
            f"({self._scene_payload_shape(data)}); chuyển {len(missing)} cảnh thiếu "
            "sang chế độ sửa tuần tự",
            "warning",
        )

        continuity_anchor = deepcopy(previous_end_state)
        for scene in scenes:
            if scene.id in collected:
                continuity_anchor = deepcopy(collected[scene.id].get("end_state"))
                continue
            repaired: dict[str, Any] | None = None
            for attempt in range(1, structure_attempts + 1):
                repair_prompt = self._scene_prompt(world, [scene], continuity_anchor)
                repair_prompt += (
                    f"\n\nSTRICT SINGLE-SCENE REPAIR ATTEMPT {attempt}: Return exactly one "
                    f"root object for {scene.id}. The root object's first key must be "
                    f'"id": "{scene.id}". Required root keys: id, summary, characters, '
                    "location_id, action, camera, lighting, atmosphere, voiceover, dialogues, "
                    "start_state, end_state. dialogues must be an array nested inside the root "
                    "scene; NEVER return a dialogue object (character_id/text/emotion) as the "
                    "root. start_state and end_state must each be objects containing "
                    "character_positions, character_wardrobe, prop_positions, time, weather, "
                    "camera and notes. Output the one complete scene object only."
                )
                repair_data = await self._request_json(
                    model,
                    repair_prompt,
                    min(max_tokens, 3200),
                    request_timeout,
                    emit,
                    f"Sửa {scene.id} (lần cấu trúc {attempt}/{structure_attempts})",
                )
                repair_items = self._extract_scene_items(repair_data)
                for item in repair_items:
                    normalized = self._normalize_scene_result(item, world)
                    if len(repair_items) == 1 and normalized.get("id") != scene.id:
                        normalized["id"] = scene.id
                        emit(
                            f"{scene.id}: đã chuẩn hóa ID của phản hồi một-cảnh",
                            "warning",
                        )
                    if self._valid_scene_result(normalized, {scene.id}):
                        repaired = normalized
                        break
                if repaired is not None:
                    break
                if attempt < structure_attempts:
                    emit(
                        f"{scene.id}: phản hồi {self._scene_payload_shape(repair_data)} "
                        "chưa đủ trường; đang yêu cầu sửa lại",
                        "warning",
                    )
            if repaired is None:
                raise XKiroError(
                    f"Model không trả đủ dữ liệu bắt buộc cho {scene.id}. "
                    "Checkpoint trước cảnh này vẫn được giữ."
                )
            if continuity_anchor is not None:
                repaired["start_state"] = deepcopy(continuity_anchor)
            collected[scene.id] = repaired
            if checkpoint_scene:
                await checkpoint_scene(scene.id, repaired)
            continuity_anchor = deepcopy(repaired.get("end_state"))
            emit(f"{scene.id}: sửa cấu trúc thành công", "success")
        return collected

    @classmethod
    def _extract_scene_items(cls, data: dict[str, Any]) -> list[dict[str, Any]]:
        return extract_scene_items(data)

    @staticmethod
    def _looks_like_scene(item: object) -> bool:
        return looks_like_scene(item)

    @staticmethod
    def _scene_payload_shape(data: dict[str, Any]) -> str:
        return scene_payload_shape(data)

    @staticmethod
    def _normalize_scene_result(item: dict[str, Any], world: dict[str, Any]) -> dict[str, Any]:
        return normalize_scene_result(item, world)

    @staticmethod
    def _valid_scene_result(item: object, required_ids: set[str]) -> bool:
        return valid_scene_result(item, required_ids)

    @staticmethod
    def _chain_scene_states(
        ordered: list[dict[str, Any]], previous: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        return chain_scene_states(ordered, previous)

    async def _completion_request(
        self,
        base_payload: dict[str, Any],
        request_timeout: int,
        emit: ProgressCallback,
        label: str,
    ) -> httpx.Response:
        variants = build_completion_variants(base_payload)
        retries = self._env_int("XKIRO_REQUEST_RETRIES", 4, 1, 8)
        backoff = self._env_int("XKIRO_RETRY_BACKOFF", 2, 0, 30)
        duplicate_wait_limit = self._env_int("XKIRO_DUPLICATE_WAIT_LIMIT", 10, 1, 30)
        duplicate_wait_seconds = self._env_int("XKIRO_DUPLICATE_WAIT_SECONDS", 30, 0, 120)
        duplicate_recovery_limit = self._env_int("XKIRO_DUPLICATE_RECOVERY_LIMIT", 2, 0, 5)
        timeout = httpx.Timeout(request_timeout, connect=min(30, request_timeout))
        last_error = "xKiro không trả về phản hồi"
        async with self._client(timeout=timeout) as client:
            attempt = 1
            duplicate_waits = 0
            duplicate_recoveries = 0
            while attempt <= retries:
                transient = False
                duplicate_in_progress = False
                last_response: httpx.Response | None = None
                for index, payload in enumerate(variants, start=1):
                    if index > 1:
                        emit(
                            f"{label}: thử giao thức tương thích model {index}/{len(variants)}",
                            "warning",
                        )
                    try:
                        response = await client.post(
                            "/v1/chat/completions",
                            json=payload,
                            headers={"Authorization": f"Bearer {self._api_key}"},
                        )
                        last_response = response
                    except httpx.TimeoutException:
                        last_error = f"{label}: model quá thời gian {request_timeout} giây"
                        transient = True
                        break
                    except (httpx.ConnectError, httpx.NetworkError) as exc:
                        last_error = f"{label}: lỗi mạng tạm thời ({type(exc).__name__})"
                        transient = True
                        break
                    if not response.is_error:
                        return response
                    if response.status_code in {400, 422} and index < len(variants):
                        continue
                    last_error = self._safe_error(response)
                    duplicate_in_progress = is_duplicate_in_progress(last_error)
                    transient = (
                        response.status_code in TRANSIENT_HTTP_STATUS or duplicate_in_progress
                    )
                    if not transient:
                        raise XKiroError(last_error)
                    break
                if duplicate_in_progress:
                    duplicate_waits += 1
                    if duplicate_waits >= duplicate_wait_limit:
                        if duplicate_recoveries >= duplicate_recovery_limit:
                            last_error = (
                                f"{label}: trạng thái xử lý trùng của xKiro không được giải phóng "
                                f"sau {duplicate_recoveries} lần phục hồi"
                            )
                            break
                        duplicate_recoveries += 1
                        duplicate_waits = 0
                        recovery_token = uuid4().hex
                        variants = inject_recovery_token(variants, recovery_token)
                        emit(
                            f"{label}: trạng thái duplicate đã quá thời gian; chuyển sang "
                            f"chữ ký phục hồi {duplicate_recoveries}/{duplicate_recovery_limit}",
                            "warning",
                        )
                        continue
                    retry_after = (
                        last_response.headers.get("retry-after") if last_response else None
                    )
                    delay = retry_delay(
                        duplicate_wait_seconds, retry_after=retry_after, maximum=120
                    )
                    emit(
                        f"{label}: yêu cầu gốc vẫn đang xử lý trên xKiro; chờ {delay} giây "
                        f"rồi kiểm tra lại ({duplicate_waits}/{duplicate_wait_limit}), "
                        "không tạo yêu cầu trùng",
                        "warning",
                    )
                    if delay:
                        await asyncio.sleep(delay)
                    continue
                if not transient or attempt >= retries:
                    break
                retry_after = last_response.headers.get("retry-after") if last_response else None
                delay = retry_delay(
                    backoff * (2 ** (attempt - 1)), retry_after=retry_after, maximum=60
                )
                emit(
                    f"{last_error}; tự thử lại {attempt + 1}/{retries} sau {delay} giây",
                    "warning",
                )
                if delay:
                    await asyncio.sleep(delay)
                attempt += 1
        raise XKiroError(last_error)

    @staticmethod
    def _split_source(text: str, max_chars: int) -> list[str]:
        return split_source(text, max_chars)

    @staticmethod
    def _draft_world(draft: Project) -> dict[str, Any]:
        return draft_world(draft)

    @staticmethod
    def _merge_world(current: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
        return merge_world(current, update)

    @staticmethod
    def _world_prompt(current: dict[str, Any], source_chunk: str, index: int, total: int) -> str:
        return world_prompt(current, source_chunk, index, total)

    @staticmethod
    def _scene_prompt(
        world: dict[str, Any], scenes: list[Any], previous_end_state: dict[str, Any] | None
    ) -> str:
        return scene_prompt(world, scenes, previous_end_state, schema_version=SCENE_SCHEMA_VERSION)

    @staticmethod
    def _message_content(message: object) -> str:
        return message_content(message)

    @staticmethod
    def _parse_json(content: object) -> dict[str, Any]:
        return parse_json_object(content)

    @staticmethod
    def _analysis_prompt(request: AnalyzeRequest, draft: Project) -> str:
        return analysis_prompt(request, draft)

    @staticmethod
    def _merge_analysis(draft: Project, data: dict[str, Any], model: str) -> Project:
        return merge_analysis(draft, data, model)
