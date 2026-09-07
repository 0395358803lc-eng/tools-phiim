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
    assert 'id="analysisModelInput"' in index.text
    assert 'id="visionModelInput"' in index.text
    assert 'id="visionModelCount"' in index.text
    assert "capabilities?.vision" in script.text
    assert "vision_model:" in script.text
    assert '"Chọn model Vision..."' in script.text
    assert '"Vision QC"' in script.text
    assert 'id="renderConnection"' in index.text
    assert 'id="googleFlowStatus"' in index.text
    assert 'id="googleFlowSessionModal"' in index.text
    assert 'id="newGoogleFlowSessionBtn"' in index.text
    assert 'id="activateGoogleFlowPendingBtn"' in index.text
    assert "function renderGoogleFlowSessionStatus(status)" in script.text
    assert "async function createGoogleFlowSession()" in script.text
    assert "/api/google-flow/session/new" in script.text
    assert ".flow-session-meta" in styles.text
    assert "Thêm mới / Thay đổi" in script.text
    assert "function setXKiroCredentialEditor(open)" in script.text
    assert "function loadRenderStatus()" in script.text
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
    assert 'data-editor-tab="image"' in index.text
    assert 'data-editor-panel="image"' in index.text
    assert 'id="imagePlanStatus"' in index.text
    assert 'id="startImagePrompt"' in index.text
    assert 'id="targetImagePrompt"' in index.text
    assert 'id="generateImageBtn"' in index.text
    assert "function renderImagePlan(scene)" in script.text
    assert "previous_accepted_end_frame" in script.text
    assert ".image-plan-grid" in styles.text
    assert "MASTER REFERENCE LIBRARY" in index.text
    assert "Scene start-frame override / last-frame anchor" in index.text
    assert "Master References được REUSE" in index.text
    assert "function renderBible()" in script.text
    assert "async function uploadMasterReference(referenceId)" in script.text
    assert "✓ REUSE MASTER" in script.text
    assert "Scene này chỉ reuse, không tạo lại entity" in script.text
    assert ".master-reference" in styles.text
    assert ".master-library-intro" in styles.text
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
