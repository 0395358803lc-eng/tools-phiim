from pathlib import Path

from fastapi.testclient import TestClient

from flow_story_studio.desktop import WORKSPACE_GATE_HTML
from flow_story_studio.main import create_app
from flow_story_studio.storage import ProjectStorage


def test_responsive_workspace_assets_are_embedded(tmp_path: Path) -> None:
    app = create_app(ProjectStorage(tmp_path / "projects"))
    with TestClient(app) as client:
        index = client.get("/")
        styles = client.get("/assets/styles.css")
        script = client.get("/assets/app.js")

    assert index.status_code == 200
    assert 'id="workspaceSwitcher"' in index.text
    assert 'data-workspace-view="storyboard"' in index.text
    assert "max-width: 1199px" in styles.text
    assert '.workspace[data-workspace-view="editor"]' in styles.text
    assert "function setWorkspaceView(view)" in script.text
    assert 'setWorkspaceView("editor")' in script.text
    assert 'id="workspacePath"' in index.text
    assert 'id="videoSetupModal"' in index.text
    assert 'id="xkiroCredentialEditor"' in index.text
    assert 'id="flowCredentialEditor"' in index.text
    assert "Thêm mới / Thay đổi" in script.text
    assert "function setXKiroCredentialEditor(open)" in script.text
    assert "function setFlowCredentialEditor(open)" in script.text
    assert ".credential-overview" in styles.text
    assert 'id="mergeAllBtn"' in index.text
    assert 'id="finalVideoModal"' in index.text
    assert "function renderFinalVideo()" in script.text
    assert "function startFinalVideoMerge" in script.text
    assert "function syncVideoElement(stage, url, mediaKey" in script.text
    assert "current?.dataset.mediaKey === mediaKey" in script.text
    assert 'video.preload = "metadata"' in script.text
    assert "stage.replaceChildren(video)" in script.text
    assert ".final-video-stage" in styles.text
    assert 'id="sceneLockBtn"' in index.text
    assert 'id="aiLockBanner"' in index.text
    assert "async function toggleSceneLock()" in script.text
    assert ".scene-lock-btn" in styles.text
    assert "await loadSession()" in script.text
    assert "const projects = await listProjects()" not in script.text
    assert "openAnalysisStep();" in script.text
    assert "Chọn thư mục làm việc" in WORKSPACE_GATE_HTML
    assert "pywebviewready" in WORKSPACE_GATE_HTML
    assert "bridgeReady()" in WORKSPACE_GATE_HTML
    assert '<option value="Cinematic">Điện ảnh</option>' in index.text
    assert '<option value="Photorealistic">Ảnh chân thực</option>' in index.text
    assert '<option value="Product Advertising">Quảng cáo sản phẩm</option>' in index.text
    assert "Thời lượng mỗi cảnh" in index.text
    assert "Phong cách hình ảnh" in index.text
    assert "Khóa nhân vật" in index.text
    assert "Khóa bối cảnh" in index.text
    assert "Tự động giữ nhất quán" in index.text
    assert "function displayStyle(style)" in script.text
    assert "const project = await api(`/api/projects/${finished.project.id}`);" in script.text
