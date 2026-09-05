"""Durable SQLite checkpoint storage for long-running xKiro analysis."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from ..models import AnalyzeRequest, Project, utc_now

CHECKPOINT_VERSION = 13


class CheckpointError(RuntimeError):
    """Raised when durable checkpoint state cannot be persisted."""


class AnalysisCheckpointStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root.resolve() if root else None

    def set_root(self, root: Path) -> None:
        self.root = root.resolve()

    def path_for(self, request: AnalyzeRequest) -> Path | None:
        if not self.root:
            return None
        identity = json.dumps(
            {
                "version": CHECKPOINT_VERSION,
                "text": request.original_text,
                "settings": request.settings.model_dump(mode="json"),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256(identity).hexdigest()
        return self.root / f"{digest}.sqlite3"

    async def clear(self, request: AnalyzeRequest) -> None:
        path = self.path_for(request)
        if not path:
            return
        try:
            for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
                await asyncio.to_thread(candidate.unlink, missing_ok=True)
        except OSError:
            pass

    async def load(
        self,
        request: AnalyzeRequest,
        draft: Project,
        model: str,
        emit: Any,
    ) -> dict[str, Any]:
        blank: dict[str, Any] = {
            "version": CHECKPOINT_VERSION,
            "model": model,
            "scene_ids": [scene.id for scene in draft.scenes],
            "world_chunks_completed": 0,
            "world": None,
            "scenes": {},
            "updated_at": utc_now(),
        }
        path = self.path_for(request)
        if not path or not path.is_file():
            return blank

        try:

            def read_checkpoint() -> dict[str, Any]:
                connection = sqlite3.connect(path)
                try:
                    meta_row = connection.execute(
                        "SELECT value FROM checkpoint_meta WHERE key = 'state'"
                    ).fetchone()
                    if not meta_row:
                        raise ValueError("checkpoint metadata is missing")
                    state = json.loads(meta_row[0])
                    state["scenes"] = {
                        scene_id: json.loads(payload)
                        for scene_id, payload in connection.execute(
                            "SELECT scene_id, payload FROM checkpoint_scenes ORDER BY scene_id"
                        )
                    }
                    return state
                finally:
                    connection.close()

            loaded = await asyncio.to_thread(read_checkpoint)
            if (
                not isinstance(loaded, dict)
                or loaded.get("version") != CHECKPOINT_VERSION
                or loaded.get("model") != model
                or loaded.get("scene_ids") != blank["scene_ids"]
                or not isinstance(loaded.get("scenes"), dict)
            ):
                emit("Checkpoint cũ không tương thích; tạo tiến độ mới", "warning")
                await asyncio.to_thread(path.unlink, missing_ok=True)
                return blank
            completed = len(loaded.get("scenes", {}))
            emit(
                f"Đã khôi phục checkpoint: {completed}/{len(draft.scenes)} cảnh đã duyệt",
                "success",
            )
            return loaded
        except (OSError, sqlite3.Error, ValueError, TypeError):
            emit("Checkpoint không đọc được; tạo tiến độ mới", "warning")
            try:
                await asyncio.to_thread(path.unlink, missing_ok=True)
            except OSError:
                pass
            return blank

    async def save(
        self,
        request: AnalyzeRequest,
        checkpoint: dict[str, Any],
        scene_ids: list[str] | None = None,
    ) -> None:
        path = self.path_for(request)
        if not path:
            return
        checkpoint["updated_at"] = utc_now()
        state_payload = json.dumps(
            {key: value for key, value in checkpoint.items() if key != "scenes"},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        scenes = checkpoint.get("scenes", {})
        selected = scene_ids or []

        def transactional_write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(path)
            try:
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS checkpoint_meta "
                    "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS checkpoint_scenes "
                    "(scene_id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT OR REPLACE INTO checkpoint_meta(key, value) VALUES('state', ?)",
                    (state_payload,),
                )
                connection.executemany(
                    "INSERT OR REPLACE INTO checkpoint_scenes(scene_id, payload) VALUES(?, ?)",
                    [
                        (
                            scene_id,
                            json.dumps(
                                scenes[scene_id],
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        )
                        for scene_id in selected
                        if scene_id in scenes
                    ],
                )
                connection.commit()
            finally:
                connection.close()

        try:
            await asyncio.to_thread(transactional_write)
        except (OSError, sqlite3.Error) as exc:
            raise CheckpointError(
                "Không thể lưu checkpoint phân tích vào thư mục làm việc"
            ) from exc
