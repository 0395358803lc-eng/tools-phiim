import shutil
from pathlib import Path

import flow_story_studio.storage as storage_module
from flow_story_studio.engines.analyzer import analyze_story
from flow_story_studio.models import AnalyzeRequest
from flow_story_studio.storage import ProjectStorage

TEXT = (
    "Một người đàn ông bước vào nhà ga lúc nửa đêm. "
    "Anh cầm một chiếc vé cũ trong tay và nhìn đồng hồ. "
    "Sau đó anh quay lại cửa ra vào khi nghe tiếng gọi phía sau."
)


def _project():
    return analyze_story(AnalyzeRequest(name="Storage resilience", original_text=TEXT))


def test_save_recreates_project_root_deleted_during_long_job(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    storage = ProjectStorage(root)
    shutil.rmtree(root)

    project = storage.save(_project())

    assert root.is_dir()
    assert (root / f"{project.id}.json").is_file()
    assert storage.get(project.id) is not None


def test_save_retries_when_atomic_temp_path_disappears_before_replace(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "projects"
    storage = ProjectStorage(root)
    project = _project()
    real_replace = storage_module.os.replace
    calls = 0

    def flaky_replace(src, dst):
        nonlocal calls
        calls += 1
        if calls == 1:
            Path(src).unlink(missing_ok=True)
            shutil.rmtree(root, ignore_errors=True)
            raise FileNotFoundError(src)
        return real_replace(src, dst)

    monkeypatch.setattr(storage_module.os, "replace", flaky_replace)
    storage.save(project)

    assert calls == 2
    assert (root / f"{project.id}.json").is_file()
    loaded = storage.get(project.id)
    assert loaded is not None
    assert loaded.id == project.id


def test_save_retries_windows_share_violation_during_atomic_replace(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "projects"
    storage = ProjectStorage(root)
    project = _project()
    real_replace = storage_module.os.replace
    calls = 0

    def locked_replace(src, dst):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise PermissionError(13, "sharing violation", str(dst), 32)
        return real_replace(src, dst)

    monkeypatch.setattr(storage_module.os, "replace", locked_replace)
    monkeypatch.setattr(storage_module.time, "sleep", lambda _seconds: None)

    storage.save(project)

    assert calls == 3
    assert storage.get(project.id) is not None
    assert list(root.glob(f".{project.id}-*.tmp")) == []


def test_save_uses_pending_sidecar_when_windows_lock_persists(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "projects"
    storage = ProjectStorage(root)
    project = storage.save(_project())
    target = root / f"{project.id}.json"
    pending = storage._pending_path(target)
    project.name = "Updated while locked"
    real_replace = storage_module.os.replace

    def locked_target_replace(src, dst):
        if Path(dst) == target:
            raise PermissionError(13, "sharing violation", str(dst), 32)
        return real_replace(src, dst)

    monkeypatch.setattr(storage_module.os, "replace", locked_target_replace)
    monkeypatch.setattr(storage_module.time, "sleep", lambda _seconds: None)

    storage.save(project)

    assert pending.is_file()
    assert storage.get(project.id).name == "Updated while locked"
    assert any(item["name"] == "Updated while locked" for item in storage.list())


def test_pending_sidecar_is_promoted_after_windows_lock_releases(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "projects"
    storage = ProjectStorage(root)
    project = storage.save(_project())
    target = root / f"{project.id}.json"
    pending = storage._pending_path(target)
    project.name = "Promote me"
    real_replace = storage_module.os.replace
    locked = True

    def maybe_locked_replace(src, dst):
        if locked and Path(dst) == target:
            raise PermissionError(13, "sharing violation", str(dst), 32)
        return real_replace(src, dst)

    monkeypatch.setattr(storage_module.os, "replace", maybe_locked_replace)
    monkeypatch.setattr(storage_module.time, "sleep", lambda _seconds: None)
    storage.save(project)
    assert pending.is_file()

    locked = False
    loaded = storage.get(project.id)

    assert loaded is not None
    assert loaded.name == "Promote me"
    assert target.is_file()
    assert not pending.exists()
