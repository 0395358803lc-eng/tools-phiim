"""Atomic, versioned local-first JSON project storage with bounded backups."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from uuid import uuid4

from .migrations import CURRENT_PROJECT_SCHEMA_VERSION, migrate_project_payload
from .models import Project, utc_now


class ProjectStorage:
    def __init__(
        self,
        root: Path,
        *,
        backup_root: Path | None = None,
        backup_retention: int = 20,
        backup_interval_seconds: int = 60,
    ) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.backup_root = backup_root or self.root.parent / "backups"
        self.backup_root.mkdir(parents=True, exist_ok=True)
        self.backup_retention = max(1, backup_retention)
        self.backup_interval_seconds = max(0, backup_interval_seconds)
        self._lock = threading.RLock()
        self._last_backup_at: dict[str, float] = {}

    def _path(self, project_id: str) -> Path:
        if not project_id.replace("-", "").replace("_", "").isalnum():
            raise ValueError("project id khÃ´ng há»£p lá»‡")
        return self.root / f"{project_id}.json"

    def _backup_dir(self, project_id: str) -> Path:
        return self.backup_root / project_id

    def _backup_existing(self, project_id: str, *, force: bool = False) -> Path | None:
        target = self._path(project_id)
        if not target.is_file():
            return None
        now = time.time()
        if (
            not force
            and now - self._last_backup_at.get(project_id, 0.0) < self.backup_interval_seconds
        ):
            return None
        backup_dir = self._backup_dir(project_id)
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(now))
        backup = backup_dir / f"{stamp}-{uuid4().hex}.json"
        shutil.copy2(target, backup)
        self._last_backup_at[project_id] = now
        backups = sorted(
            backup_dir.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True
        )
        for old in backups[self.backup_retention :]:
            old.unlink(missing_ok=True)
        return backup

    def save(self, project: Project) -> Project:
        project.schema_version = CURRENT_PROJECT_SCHEMA_VERSION
        project.updated_at = utc_now()
        target = self._path(project.id)
        payload = project.model_dump_json(indent=2)
        with self._lock:
            self._backup_existing(project.id)
            fd, tmp_name = tempfile.mkstemp(prefix=f".{project.id}-", suffix=".tmp", dir=self.root)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_name, target)
            finally:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
        return project

    def get(self, project_id: str) -> Project | None:
        path = self._path(project_id)
        if not path.is_file():
            return None
        with self._lock:
            raw = json.loads(path.read_text(encoding="utf-8"))
            migrated = migrate_project_payload(raw)
            return Project.model_validate(migrated)

    def list(self) -> list[dict[str, object]]:
        projects: list[dict[str, object]] = []
        with self._lock:
            for path in self.root.glob("*.json"):
                try:
                    raw = migrate_project_payload(json.loads(path.read_text(encoding="utf-8")))
                    projects.append(
                        {
                            "id": raw["id"],
                            "name": raw["name"],
                            "updated_at": raw.get("updated_at", ""),
                            "scene_count": len(raw.get("scenes", [])),
                            "continuity_score": raw.get("continuity_score", 0),
                            "schema_version": raw.get("schema_version", 1),
                        }
                    )
                except (OSError, ValueError, KeyError, TypeError):
                    continue
        return sorted(projects, key=lambda item: str(item["updated_at"]), reverse=True)

    def backups(self, project_id: str) -> list[Path]:
        backup_dir = self._backup_dir(project_id)
        if not backup_dir.is_dir():
            return []
        return sorted(
            backup_dir.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True
        )

    def backup_metadata(self, project_id: str) -> list[dict[str, object]]:
        return [
            {
                "name": path.name,
                "size": path.stat().st_size,
                "modified_at": path.stat().st_mtime,
            }
            for path in self.backups(project_id)
        ]

    def restore_backup(self, project_id: str, backup_name: str) -> Project:
        if Path(backup_name).name != backup_name or not backup_name.endswith(".json"):
            raise ValueError("backup name khÃ´ng há»£p lá»‡")
        backup = self._backup_dir(project_id) / backup_name
        if not backup.is_file():
            raise FileNotFoundError(backup_name)
        with self._lock:
            raw = json.loads(backup.read_text(encoding="utf-8"))
            restored = Project.model_validate(migrate_project_payload(raw))
            if restored.id != project_id:
                raise ValueError("backup project id khÃ´ng khá»›p")
            self._backup_existing(project_id, force=True)
            restored.updated_at = utc_now()
            target = self._path(project_id)
            payload = restored.model_dump_json(indent=2)
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{project_id}-restore-", suffix=".tmp", dir=self.root
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_name, target)
            finally:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
            return restored

    def delete(self, project_id: str) -> bool:
        path = self._path(project_id)
        if not path.exists():
            return False
        with self._lock:
            self._backup_existing(project_id, force=True)
            path.unlink()
        return True
