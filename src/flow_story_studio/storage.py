"""Atomic, versioned local-first JSON project storage with bounded backups."""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from uuid import uuid4

from .migrations import CURRENT_PROJECT_SCHEMA_VERSION, migrate_project_payload
from .models import Project, utc_now

LOGGER = logging.getLogger(__name__)


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
        self.backup_root = backup_root or self.root.parent / "backups"
        self.backup_retention = max(1, backup_retention)
        self.backup_interval_seconds = max(0, backup_interval_seconds)
        self._lock = threading.RLock()
        self._last_backup_at: dict[str, float] = {}
        self._ensure_roots()

    def _ensure_roots(self) -> None:
        """Recreate workspace storage directories if they disappear during a long job."""
        self.root.mkdir(parents=True, exist_ok=True)
        self.backup_root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _pending_path(target: Path) -> Path:
        return target.with_name(f".{target.name}.pending")

    def _write_pending_text(self, target: Path, payload: str, *, prefix: str) -> None:
        pending = self._pending_path(target)
        fd, tmp_name = tempfile.mkstemp(prefix=prefix, suffix=".pending.tmp", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, pending)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def _try_promote_pending(self, target: Path) -> bool:
        pending = self._pending_path(target)
        if not pending.is_file():
            return False
        try:
            os.replace(pending, target)
            return True
        except (PermissionError, FileNotFoundError):
            return False

    def _logical_path(self, target: Path) -> Path:
        self._try_promote_pending(target)
        pending = self._pending_path(target)
        return pending if pending.is_file() else target

    def _atomic_write_text(self, target: Path, payload: str, *, prefix: str) -> None:
        """Persist atomically, including when Windows temporarily locks the target."""
        last_error: OSError | None = None
        max_attempts = 6
        for attempt in range(max_attempts):
            tmp_name = ""
            try:
                self._ensure_roots()
                fd, tmp_name = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=self.root)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                # A long xKiro job can run for tens of minutes. If external cleanup
                # removes an empty projects directory meanwhile, rebuild it and retry.
                self._ensure_roots()
                os.replace(tmp_name, target)
                self._pending_path(target).unlink(missing_ok=True)
                return
            except FileNotFoundError as exc:
                last_error = exc
                self._ensure_roots()
                if attempt >= max_attempts - 1:
                    raise
                time.sleep(min(0.05 * (attempt + 1), 0.5))
            except PermissionError as exc:
                # Windows may deny replace while another process is reading the
                # project without FILE_SHARE_DELETE. Retry briefly; if the lock
                # persists, atomically persist the same payload to a sidecar.
                last_error = exc
                if attempt >= max_attempts - 1:
                    self._write_pending_text(target, payload, prefix=prefix)
                    LOGGER.warning(
                        "Project target locked; persisted pending sidecar target=%s error=%s",
                        target,
                        exc,
                    )
                    return
                time.sleep(min(0.05 * (2 ** min(attempt, 5)), 0.8))
            finally:
                if tmp_name and os.path.exists(tmp_name):
                    os.unlink(tmp_name)
        if last_error is not None:
            raise last_error

    def _path(self, project_id: str) -> Path:
        if not project_id.replace("-", "").replace("_", "").isalnum():
            raise ValueError("project id không hợp lệ")
        return self.root / f"{project_id}.json"

    def _backup_dir(self, project_id: str) -> Path:
        return self.backup_root / project_id

    def _backup_existing(self, project_id: str, *, force: bool = False) -> Path | None:
        target = self._path(project_id)
        source = self._logical_path(target)
        if not source.is_file():
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
        shutil.copy2(source, backup)
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
            self._atomic_write_text(target, payload, prefix=f".{project.id}-")
        return project

    def get(self, project_id: str) -> Project | None:
        target = self._path(project_id)
        with self._lock:
            path = self._logical_path(target)
            if not path.is_file():
                return None
            raw = json.loads(path.read_text(encoding="utf-8"))
            migrated = migrate_project_payload(raw)
            return Project.model_validate(migrated)

    def list(self) -> list[dict[str, object]]:
        projects: list[dict[str, object]] = []
        with self._lock:
            project_ids = {path.stem for path in self.root.glob("*.json")}
            for pending in self.root.glob(".*.json.pending"):
                name = pending.name
                project_ids.add(name[1 : -len(".json.pending")])

            for project_id in project_ids:
                try:
                    path = self._logical_path(self._path(project_id))
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
            raise ValueError("backup name không hợp lệ")
        backup = self._backup_dir(project_id) / backup_name
        if not backup.is_file():
            raise FileNotFoundError(backup_name)
        with self._lock:
            raw = json.loads(backup.read_text(encoding="utf-8"))
            restored = Project.model_validate(migrate_project_payload(raw))
            if restored.id != project_id:
                raise ValueError("backup project id không khớp")
            self._backup_existing(project_id, force=True)
            restored.updated_at = utc_now()
            target = self._path(project_id)
            payload = restored.model_dump_json(indent=2)
            self._atomic_write_text(target, payload, prefix=f".{project_id}-restore-")
            return restored

    def delete(self, project_id: str) -> bool:
        path = self._path(project_id)
        pending = self._pending_path(path)
        if not path.exists() and not pending.exists():
            return False
        with self._lock:
            self._backup_existing(project_id, force=True)
            pending.unlink(missing_ok=True)
            path.unlink(missing_ok=True)
        return True
