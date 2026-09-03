"""Atomic, local-first JSON project storage."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

from .models import Project, utc_now


class ProjectStorage:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _path(self, project_id: str) -> Path:
        if not project_id.replace("-", "").replace("_", "").isalnum():
            raise ValueError("project id không hợp lệ")
        return self.root / f"{project_id}.json"

    def save(self, project: Project) -> Project:
        project.updated_at = utc_now()
        target = self._path(project.id)
        payload = project.model_dump_json(indent=2)
        with self._lock:
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
            return Project.model_validate_json(path.read_text(encoding="utf-8"))

    def list(self) -> list[dict[str, object]]:
        projects: list[dict[str, object]] = []
        with self._lock:
            for path in self.root.glob("*.json"):
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    projects.append(
                        {
                            "id": raw["id"],
                            "name": raw["name"],
                            "updated_at": raw.get("updated_at", ""),
                            "scene_count": len(raw.get("scenes", [])),
                            "continuity_score": raw.get("continuity_score", 0),
                        }
                    )
                except (OSError, ValueError, KeyError):
                    continue
        return sorted(projects, key=lambda item: str(item["updated_at"]), reverse=True)

    def delete(self, project_id: str) -> bool:
        path = self._path(project_id)
        if not path.exists():
            return False
        path.unlink()
        return True
