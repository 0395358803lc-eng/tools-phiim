const initialSessionToken = new URLSearchParams(window.location.hash.slice(1)).get("session") || "";
if (initialSessionToken) history.replaceState(null, "", window.location.pathname + window.location.search);

const state = {
  sessionToken: initialSessionToken,
  project: null,
  activeSceneId: null,
  sceneFilter: "all",
  search: "",
  poller: null,
  queueActive: false,
  draggedSceneId: null,
  xkiroConnected: false,
  xkiroModels: [],
  flowConfigured: false,
  flowAuthenticated: false,
  flowModels: [],
  analysisJobId: null,
  analysisLogs: [],
  workspaceView: "storyboard",
  sessionId: "",
  workspacePath: "",
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(state.sessionToken ? { "X-Flow-Studio-Session": state.sessionToken } : {}), ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    let message = `Lỗi ${response.status}`;
    try {
      const data = await response.json();
      message = typeof data.detail === "string" ? data.detail : message;
    } catch (_) {}
    throw new Error(message);
  }
  if (response.status === 204) return null;
  return response.json();
}

function toast(message, isError = false) {
  const node = $("#toast");
  node.textContent = message;
  node.style.background = isError ? "#ff9d8f" : "#efffa2";
  node.classList.add("show");
  clearTimeout(node._timer);
  node._timer = setTimeout(() => node.classList.remove("show"), 2800);
}

function escapeHtml(value = "") {
  return String(value).replace(/[&<>'"]/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[char]);
}

function displayStyle(style) {
  return ({
    Cinematic: "Điện ảnh",
    Photorealistic: "Ảnh chân thực",
    Commercial: "Quảng cáo thương mại",
    Documentary: "Phim tài liệu",
    Anime: "Hoạt hình Anime",
    "3D Animation": "Hoạt hình 3D",
    "Product Advertising": "Quảng cáo sản phẩm",
  })[style] || style;
}

function activeScene() {
  return state.project?.scenes.find((scene) => scene.id === state.activeSceneId) || null;
}

function setWorkspaceView(view) {
  if (!["project", "storyboard", "editor"].includes(view)) return;
  state.workspaceView = view;
  $("#workspace").dataset.workspaceView = view;
  $$("#workspaceSwitcher button").forEach((button) => {
    const active = button.dataset.workspaceView === view;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
}

async function loadSession() {
  const session = await api("/api/session");
  state.sessionId = session.id;
  state.workspacePath = session.workspace;
  const folderName = session.workspace.split(/[\\/]/).filter(Boolean).pop() || session.workspace;
  $("#workspacePath").textContent = session.workspace;
  $("#workspacePath").title = session.workspace;
  $("#workspaceLabel").textContent = `THƯ MỤC · ${folderName}`;
  $("#workspaceLabel").title = session.workspace;
}

function openAnalysisStep() {
  const required = !state.project;
  document.body.classList.toggle("fresh-session", required);
  $("#newProjectCloseBtn").classList.toggle("hidden", required);
  $("#newProjectCancelBtn").classList.toggle("hidden", required);
  if (!$("#newProjectModal").open) $("#newProjectModal").showModal();
  window.setTimeout(() => $("#storyInput").focus(), 50);
}

function renderProject(project, preserveActive = true) {
  const oldActive = preserveActive ? state.activeSceneId : null;
  state.project = project;
  document.body.classList.remove("fresh-session");
  state.activeSceneId = project.scenes.some((scene) => scene.id === oldActive)
    ? oldActive
    : project.scenes[0]?.id || null;
  $("#workspace").classList.remove("empty");
  $("#projectTitle").textContent = project.name;
  $("#sourcePreview").textContent = project.original_text;
  $("#sourcePreview").classList.remove("empty-copy");
  $("#wordCount").textContent = project.original_text.trim().split(/\s+/).length;
  $("#sceneCount").textContent = project.scenes.length;
  const seconds = project.scenes.reduce((sum, scene) => sum + scene.duration, 0);
  $("#totalDuration").textContent = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
  $("#masterPrompt").textContent = project.master_prompt;
  $("#visualStyle").textContent = project.visual_style;
  $("#continuityScore").textContent = `${project.continuity_score}%`;
  $("#continuityBar").style.width = `${project.continuity_score}%`;
  $("#providerInput").value = project.settings.provider || "google-flow";
  $("#videoModelInput").value = project.settings.video_model || "veo-3.1-lite-lower-priority";
  toggleFlowConfig();
  $$("#exportBtn, #analyzeAgainBtn, #continuityBtn, #generateSelectedBtn, #generateAllBtn").forEach((button) => button.disabled = false);
  renderBible();
  renderSettings();
  renderScenes();
  renderEditor();
  renderQueue();
  renderFinalVideo();
  updatePolling();
}

function renderBible() {
  const project = state.project;
  const bible = project.story_bible;
  $("#storyBible").innerHTML = `<div class="story-card"><strong>${escapeHtml(bible.main_theme)}</strong><p>${escapeHtml(bible.synopsis)}</p><small>${escapeHtml(bible.genre)} · ${escapeHtml(bible.mood)}</small></div>`;
  $("#characterCount").textContent = project.characters.length;
  $("#characterBible").innerHTML = project.characters.map((item) => `<article class="bible-card"><header><strong>${escapeHtml(item.name)}</strong><code>${item.id}</code></header><p>${escapeHtml(item.gender)}, ${escapeHtml(item.estimated_age)} · ${escapeHtml(item.clothing)} · ${escapeHtml(item.identifying_features)}</p></article>`).join("") || `<div class="empty-copy">Không có nhân vật.</div>`;
  $("#locationCount").textContent = project.locations.length;
  $("#locationBible").innerHTML = project.locations.map((item) => `<article class="bible-card"><header><strong>${escapeHtml(item.name)}</strong><code>${item.id}</code></header><p>${escapeHtml(item.architecture)} · ${escapeHtml(item.space)} · ${escapeHtml(item.lighting)}</p></article>`).join("");
  $("#propCount").textContent = project.props.length;
  $("#propBible").innerHTML = project.props.map((item) => `<article class="bible-card"><header><strong>${escapeHtml(item.name)}</strong><code>${item.id}</code></header><p>${escapeHtml(item.description)} · ${escapeHtml(item.initial_location)}</p></article>`).join("") || `<div class="empty-copy">Chưa phát hiện đạo cụ quan trọng.</div>`;
}

function renderSettings() {
  const settings = state.project.settings;
  const entries = [
    ["Aspect", settings.aspect_ratio], ["Resolution", settings.resolution],
    ["Phong cách hình ảnh", displayStyle(settings.style)], ["Thời lượng cảnh mặc định", `${settings.scene_duration} giây`],
    ["Character lock", settings.character_lock ? "Bật" : "Tắt"],
    ["Location lock", settings.location_lock ? "Bật" : "Tắt"],
    ["Tự động giữ nhất quán", settings.auto_continuity ? "Bật" : "Tắt"],
    ["AI analysis", settings.analysis_provider || "offline"],
    ["Analysis model", settings.analysis_model || "Rule-based local"],
    ["Provider", settings.provider],
    ["Video model", settings.video_model || "veo-3.1-lite-lower-priority"],
  ];
  $("#settingsView").innerHTML = entries.map(([label, value]) => `<div class="setting"><span>${label}</span><strong>${escapeHtml(value)}</strong></div>`).join("") + `<button class="secondary-btn full" id="exportPromptsBtn" style="grid-column:1/-1">Export all Flow prompts (.zip)</button>`;
  $("#exportPromptsBtn").onclick = () => download(`/api/projects/${state.project.id}/flow-prompts.zip`);
  $("#timeline").innerHTML = state.project.timeline.map((item) => `<li>${escapeHtml(item)}</li>`).join("");
}

function filteredScenes() {
  if (!state.project) return [];
  const term = state.search.toLowerCase();
  return state.project.scenes.filter((scene) => {
    const matchesSearch = !term || `${scene.id} ${scene.summary} ${scene.action}`.toLowerCase().includes(term);
    const matchesFilter = state.sceneFilter === "all" || scene.warnings.length > 0;
    return matchesSearch && matchesFilter;
  });
}

function renderScenes() {
  const container = $("#sceneList");
  const scenes = filteredScenes();
  container.classList.toggle("empty-copy", scenes.length === 0);
  container.innerHTML = scenes.length ? scenes.map((scene) => `
    <article class="scene-card ${scene.id === state.activeSceneId ? "active" : ""}" draggable="true" data-scene-id="${scene.id}">
      <input type="checkbox" class="scene-select" ${scene.selected ? "checked" : ""} aria-label="Chọn ${scene.id}">
      <div class="card-main"><header><code>${scene.id}</code><span>${escapeHtml(scene.location_id)}</span>${scene.warnings.length ? `<b class="warning-dot" title="${scene.warnings.length} cảnh báo">▲</b>` : ""}${scene.ai_locked ? `<b class="ai-lock-dot" title="AI Continuity Lock">🔒</b>` : ""}</header><p>${escapeHtml(scene.summary)}</p><div class="scene-meta"><span>${scene.duration}s</span><span>${escapeHtml(scene.camera.split(",")[0])}</span><span>${scene.progress}%</span></div></div>
      <i class="scene-status ${scene.status}" title="${scene.status}"></i>
    </article>`).join("") : "Không có scene phù hợp.";

  $$(".scene-card").forEach((card) => {
    card.addEventListener("click", (event) => {
      if (event.target.classList.contains("scene-select")) return;
      state.activeSceneId = card.dataset.sceneId;
      renderScenes(); renderEditor();
      setWorkspaceView("editor");
    });
    const checkbox = card.querySelector(".scene-select");
    checkbox.addEventListener("change", async () => {
      const scene = state.project.scenes.find((item) => item.id === card.dataset.sceneId);
      scene.selected = checkbox.checked;
      try { renderProject(await api(`/api/projects/${state.project.id}/scenes/${scene.id}`, { method: "PATCH", body: JSON.stringify({ selected: checkbox.checked }) })); }
      catch (error) { toast(error.message, true); }
    });
    card.addEventListener("dragstart", () => { state.draggedSceneId = card.dataset.sceneId; card.classList.add("dragging"); });
    card.addEventListener("dragend", () => card.classList.remove("dragging"));
    card.addEventListener("dragover", (event) => event.preventDefault());
    card.addEventListener("drop", async (event) => {
      event.preventDefault();
      const targetId = card.dataset.sceneId;
      if (!state.draggedSceneId || targetId === state.draggedSceneId) return;
      const ids = state.project.scenes.map((item) => item.id);
      const [moved] = ids.splice(ids.indexOf(state.draggedSceneId), 1);
      ids.splice(ids.indexOf(targetId), 0, moved);
      try {
        renderProject(await api(`/api/projects/${state.project.id}/reorder`, { method: "POST", body: JSON.stringify({ scene_ids: ids }) }), false);
        toast("Đã cập nhật thứ tự và continuity");
      } catch (error) { toast(error.message, true); }
    });
  });
}

function renderEditor() {
  const scene = activeScene();
  $("#editorEmpty").classList.toggle("hidden", !!scene);
  $("#sceneForm").classList.toggle("hidden", !scene);
  $("#activeSceneId").textContent = scene?.id || "—";
  $("#sceneLockBtn").disabled = !scene;
  if (!scene) return;
  $("#editLocation").innerHTML = state.project.locations.map((item) => `<option value="${item.id}" ${item.id === scene.location_id ? "selected" : ""}>${item.id} · ${escapeHtml(item.name)}</option>`).join("");
  $("#editCharacters").innerHTML = state.project.characters.map((item) => `<option value="${item.id}" ${scene.characters.includes(item.id) ? "selected" : ""}>${item.id} · ${escapeHtml(item.name)}</option>`).join("");
  $("#editDuration").value = scene.duration;
  $("#editSource").value = scene.source_text;
  $("#editAction").value = scene.action;
  $("#editCamera").value = scene.camera;
  $("#editLighting").value = scene.lighting;
  $("#editAtmosphere").value = scene.atmosphere;
  $("#editVoiceover").value = scene.voiceover;
  $("#editDialogues").value = JSON.stringify(scene.dialogues, null, 2);
  $("#editReference").value = scene.reference_image || "";
  $("#editVisualPrompt").value = scene.visual_prompt;
  $("#editFlowPrompt").value = scene.flow_prompt;
  $("#startState").value = JSON.stringify(scene.start_state, null, 2);
  $("#endState").value = JSON.stringify(scene.end_state, null, 2);
  const protectedControls = [
    "editLocation", "editDuration", "editCharacters", "editSource", "editAction",
    "editCamera", "editLighting", "editAtmosphere", "editVoiceover", "editDialogues",
    "editVisualPrompt", "editFlowPrompt", "startState", "endState", "saveSceneBtn",
    "saveContinuityBtn", "savePromptBtn",
  ];
  protectedControls.forEach((id) => { $(`#${id}`).disabled = Boolean(scene.ai_locked); });
  $("#sceneLockBtn").textContent = scene.ai_locked ? "🔒 AI đã khóa" : "🔓 Khóa lại";
  $("#sceneLockBtn").classList.toggle("unlocked", !scene.ai_locked);
  $("#aiLockBanner").classList.toggle("unlocked", !scene.ai_locked);
  $("#aiLockBanner").querySelector("strong").textContent = scene.ai_locked
    ? "🔒 AI CONTINUITY LOCK" : "🔓 CHẾ ĐỘ CHỈNH SỬA THỦ CÔNG";
  $("#aiLockBanner").querySelector("span").textContent = scene.ai_locked
    ? (scene.ai_lock_reason || "Nhận dạng, bối cảnh và thông số hình ảnh đang được bảo vệ.")
    : "Mọi thay đổi có thể làm giảm tính nhất quán giữa các video.";
  $("#sceneWarnings").innerHTML = scene.warnings.length
    ? scene.warnings.map((warning) => `<div class="warning-item">▲ ${escapeHtml(warning)}</div>`).join("")
    : `<div class="warning-item ok">✓ Không phát hiện mâu thuẫn continuity</div>`;
  renderResult(scene);
}

async function toggleSceneLock() {
  const scene = activeScene();
  if (!scene) return;
  try {
    const project = await api(`/api/projects/${state.project.id}/scenes/${scene.id}/lock`, {
      method: "PATCH", body: JSON.stringify({ locked: !scene.ai_locked }),
    });
    renderProject(project);
    toast(scene.ai_locked ? "Đã mở khóa scene để chỉnh sửa" : "Đã khóa lại dữ liệu AI");
  } catch (error) { toast(error.message, true); }
}

function renderResult(scene) {
  const stage = $("#resultStage");
  if (scene.result_url && !scene.result_url.startsWith("mock:")) {
    syncVideoElement(stage, scene.result_url, `scene:${scene.id}:${scene.result_url}`, 360);
  } else if (scene.status === "Accepted") {
    stage.innerHTML = `<div class="film-placeholder"><span>✓</span><p>Đã hoàn tất (${escapeHtml(scene.provider_job_id)})</p><small>${scene.result_url.startsWith("mock:") ? "Kết quả mô phỏng — chuyển provider sang Google Flow để render thật." : escapeHtml(scene.result_url)}</small></div>`;
  } else {
    stage.innerHTML = `<div class="film-placeholder"><span>${scene.status === "Generating" ? "…" : "▶"}</span><p>${escapeHtml(scene.status)} · ${scene.progress}%</p></div>`;
  }
  const quality = scene.quality;
  $("#qualityCard").innerHTML = quality ? [
    ["Readiness", quality.score], ["Nhân vật", quality.character], ["Bối cảnh", quality.location], ["Timeline", quality.temporal]
  ].map(([label, value]) => `<div class="quality-metric"><strong>${value}</strong><span>${label}</span></div>`).join("") : "";
}

function syncVideoElement(stage, url, mediaKey, maxHeight = null) {
  const current = stage.querySelector("video[data-media-key]");
  if (current?.dataset.mediaKey === mediaKey) return current;

  const video = document.createElement("video");
  video.controls = true;
  video.preload = "metadata";
  video.playsInline = true;
  video.src = url;
  video.dataset.mediaKey = mediaKey;
  video.style.width = "100%";
  if (maxHeight) video.style.maxHeight = `${maxHeight}px`;
  stage.replaceChildren(video);
  return video;
}

function renderQueue() {
  const scenes = state.project?.scenes || [];
  $("#queueList").innerHTML = scenes.map((scene) => `<article class="queue-item"><header><code>${scene.id}</code><span>${scene.status}</span></header><div class="progress"><i style="width:${scene.progress}%"></i></div><p>${escapeHtml(scene.summary)}</p>${["Failed", "FailedQC"].includes(scene.status) ? `<button class="secondary-btn full retry-btn" data-retry="${scene.id}" data-force="${scene.status === "FailedQC" ? "true" : "false"}" style="margin-top:9px">${scene.status === "FailedQC" ? "Force rerender" : "Recover / retry"}</button>` : ""}</article>`).join("") || `<div class="empty-copy">Hàng đợi trống.</div>`;
  $(".retry-btn").forEach((button) => button.onclick = () => generate([button.dataset.retry], button.dataset.force === "true"));
}

function finalVideoReady() {
  return Boolean(state.project?.scenes?.length)
    && state.project.scenes.every((scene) => scene.status === "Accepted" && scene.result_file);
}

function renderFinalVideo() {
  if (!state.project) return;
  const finalVideo = state.project.final_video || { status: "NotReady", progress: 0 };
  const ready = finalVideoReady();
  const merging = finalVideo.status === "Merging";
  const completed = finalVideo.status === "Completed" && finalVideo.result_url;
  const status = $("#finalVideoStatus");
  status.className = `final-video-status ${String(finalVideo.status || "NotReady").toLowerCase()}`;
  status.querySelector("span").textContent = completed
    ? `Đã ghép ${finalVideo.scene_count} scene thành video tổng`
    : merging
      ? `Đang ghép ${finalVideo.scene_count || state.project.scenes.length} scene bằng FFmpeg...`
      : finalVideo.status === "Failed"
        ? "Ghép video thất bại"
        : ready ? `Sẵn sàng ghép ${state.project.scenes.length} scene` : "Chưa đủ video scene để ghép";
  $("#finalVideoProgress").style.width = `${finalVideo.progress || (ready ? 100 : 0)}%`;
  $("#finalVideoError").classList.toggle("hidden", !finalVideo.error);
  $("#finalVideoError").textContent = finalVideo.error || "";
  const stage = $("#finalVideoStage");
  if (completed) {
    const version = encodeURIComponent(finalVideo.generated_at || "latest");
    syncVideoElement(
      stage,
      `${finalVideo.result_url}?v=${version}`,
      `final:${finalVideo.result_url}:${version}`,
    );
  } else {
    stage.innerHTML = `<div class="film-placeholder"><span>${merging ? "…" : "▶"}</span><p>${merging ? "Đang xử lý video tổng" : "Video tổng sẽ xuất hiện tại đây"}</p></div>`;
  }
  $("#downloadFinalVideoBtn").disabled = !completed;
  $("#startMergeBtn").disabled = !ready || merging;
  $("#startMergeBtn").textContent = completed ? "Ghép lại video" : "Ghép toàn bộ video";
  $("#mergeAllBtn").disabled = (!ready && !completed) || merging;
  $("#mergeAllBtn").textContent = completed ? "Xem video tổng" : merging ? "Đang ghép..." : "Ghép video";
}

async function startFinalVideoMerge(force = false) {
  if (!state.project) return;
  const completed = state.project.final_video?.status === "Completed";
  if (completed && !force) {
    if (!$("#finalVideoModal").open) $("#finalVideoModal").showModal();
    return;
  }
  if (!finalVideoReady()) return toast("Hãy tạo thành công toàn bộ video scene trước", true);
  if (!$("#finalVideoModal").open) $("#finalVideoModal").showModal();
  try {
    const project = await api(`/api/projects/${state.project.id}/final-video`, { method: "POST" });
    renderProject(project);
    toast("Đã bắt đầu ghép toàn bộ video");
  } catch (error) { toast(error.message, true); }
}

async function saveScene(event) {
  event.preventDefault();
  const scene = activeScene();
  if (!scene) return;
  let startState, endState, dialogues;
  try {
    startState = JSON.parse($("#startState").value);
    endState = JSON.parse($("#endState").value);
    dialogues = JSON.parse($("#editDialogues").value || "[]");
  } catch (_) {
    toast("Dialogue và Start/End state phải là JSON hợp lệ", true); return;
  }
  const patch = {
    source_text: $("#editSource").value,
    location_id: $("#editLocation").value,
    characters: [...$("#editCharacters").selectedOptions].map((option) => option.value),
    action: $("#editAction").value,
    camera: $("#editCamera").value,
    lighting: $("#editLighting").value,
    atmosphere: $("#editAtmosphere").value,
    duration: Number($("#editDuration").value),
    voiceover: $("#editVoiceover").value,
    dialogues,
    reference_image: $("#editReference").value,
    visual_prompt: $("#editVisualPrompt").value,
    start_state: startState,
    end_state: endState,
  };
  try {
    renderProject(await api(`/api/projects/${state.project.id}/scenes/${scene.id}`, { method: "PATCH", body: JSON.stringify(patch) }));
    toast("Đã lưu scene và kiểm tra ảnh hưởng downstream");
  } catch (error) { toast(error.message, true); }
}

function projectPayload() {
  const analysisProvider = $("#analysisProviderInput").value;
  return {
    name: $("#projectNameInput").value,
    original_text: $("#storyInput").value,
    settings: {
      aspect_ratio: $("#aspectInput").value,
      resolution: "1080p",
      style: $("#styleInput").value,
      custom_style: "",
      scene_duration: Number($("#durationInput").value),
      character_lock: $("#characterLock").checked,
      location_lock: $("#locationLock").checked,
      auto_continuity: $("#autoContinuity").checked,
      quality_threshold: 85,
      provider: $("#providerInput").value,
      video_model: $("#videoModelInput").value,
      analysis_provider: analysisProvider,
      analysis_model: analysisProvider === "xkiro" ? $("#analysisModelInput").value : "",
    },
  };
}

function renderXKiroConnection(connection) {
  state.xkiroConnected = Boolean(connection.configured);
  state.xkiroModels = connection.models || state.xkiroModels || [];
  const line = $("#xkiroConnection");
  line.classList.toggle("connected", state.xkiroConnected);
  line.classList.remove("error");
  line.querySelector("span").textContent = state.xkiroConnected
    ? connection.source === "stored"
      ? `Đã ghi nhớ ${connection.key_hint || "API key xKiro"} · tự dùng cho các phiên sau`
      : `Đang dùng ${connection.key_hint || "API key xKiro"} từ biến môi trường`
    : "Chưa lưu API key xKiro";
  $("#editXkiroBtn").textContent = state.xkiroConnected ? "Thêm mới / Thay đổi" : "Thêm API key";
  $("#disconnectXkiroBtn").disabled = !state.xkiroConnected;
  $("#modelCount").textContent = connection.model_count ?? state.xkiroModels.length;
  $("#freeModelCount").textContent = connection.free_model_count ?? state.xkiroModels.filter((model) => model.access_tier === "free").length;
  const select = $("#analysisModelInput");
  const previous = select.value || state.project?.settings?.analysis_model || "";
  if (state.xkiroModels.length) {
    const groups = state.xkiroModels.reduce((result, model) => {
      (result[model.owned_by] ||= []).push(model); return result;
    }, {});
    select.innerHTML = Object.entries(groups).map(([vendor, models]) => `<optgroup label="${escapeHtml(vendor)}">${models.map((model) => `<option value="${escapeHtml(model.id)}">${escapeHtml(model.display_name)} · ${escapeHtml((model.access_tier || "unknown").toUpperCase())} · ${model.context_length ? `${Math.round(model.context_length / 1000)}K` : "? context"}</option>`).join("")}</optgroup>`).join("");
    select.disabled = false;
    if (state.xkiroModels.some((model) => model.id === previous)) select.value = previous;
  } else {
    select.innerHTML = `<option value="">Kết nối API key để tải model...</option>`;
    select.disabled = true;
  }
}

function setXKiroCredentialEditor(open) {
  $("#xkiroCredentialEditor").classList.toggle("hidden", !open);
  if (!open) $("#xkiroKeyInput").value = "";
  else setTimeout(() => $("#xkiroKeyInput").focus(), 0);
}

function renderAnalysisJob(job) {
  const panel = $("#analysisLogPanel");
  const status = $("#analysisJobStatus");
  panel.classList.remove("hidden");
  status.textContent = job.status || "unknown";
  status.className = `analysis-job-status ${job.status || ""}`;
  state.analysisLogs = job.logs || [];
  const visibleLogs = state.analysisLogs.slice(-400);
  const hiddenCount = state.analysisLogs.length - visibleLogs.length;
  $("#analysisLogEntries").innerHTML = visibleLogs.length
    ? `${hiddenCount > 0 ? `<div class="empty-copy">Đã ẩn ${hiddenCount} dòng cũ để giao diện luôn mượt; nút Sao chép log vẫn lấy đầy đủ.</div>` : ""}${visibleLogs.map((entry) => {
      const time = entry.at ? new Date(entry.at).toLocaleTimeString("vi-VN", { hour12: false }) : "--:--:--";
      const level = entry.level || "info";
      return `<div class="analysis-log-line ${escapeHtml(level)}"><time>${escapeHtml(time)}</time><b>${escapeHtml(level)}</b><span>${escapeHtml(entry.message || "")}</span></div>`;
    }).join("")}`
    : `<div class="empty-copy">Chưa có sự kiện.</div>`;
  const entries = $("#analysisLogEntries");
  entries.scrollTop = entries.scrollHeight;
  $("#cancelAnalysisBtn").classList.toggle("hidden", !["queued", "running"].includes(job.status));
}

async function waitForAnalysisJob(jobId) {
  while (state.analysisJobId === jobId) {
    await new Promise((resolve) => setTimeout(resolve, 1500));
    const job = await api(`/api/analysis/jobs/${jobId}`);
    renderAnalysisJob(job);
    if (["completed", "failed", "cancelled"].includes(job.status)) return job;
  }
  return null;
}

async function cancelAnalysis() {
  if (!state.analysisJobId) return;
  try {
    const job = await api(`/api/analysis/jobs/${state.analysisJobId}`, { method: "DELETE" });
    renderAnalysisJob(job);
  } catch (error) { toast(error.message, true); }
}

function toggleXKiroConfig() {
  $("#xkiroConfig").classList.toggle("hidden", $("#analysisProviderInput").value !== "xkiro");
}

function renderFlowConnection(connection) {
  state.flowConfigured = Boolean(connection.configured);
  state.flowAuthenticated = Boolean(connection.authenticated);
  state.flowModels = connection.models || state.flowModels || [];
  const line = $("#flowConnection");
  line.classList.toggle("connected", state.flowAuthenticated);
  line.classList.toggle("error", !connection.flow_cli_available || (connection.configured && !connection.authenticated && connection.message && !connection.message.includes("Đã lưu")));
  const details = [];
  if (connection.cookie_count) details.push(`${connection.cookie_count} cookie`);
  if (connection.credits_remaining !== null && connection.credits_remaining !== undefined) details.push(`${connection.credits_remaining} credit`);
  if (connection.tier) details.push(connection.tier);
  line.querySelector("span").textContent = state.flowAuthenticated
    ? `Google Flow đã xác thực${details.length ? ` · ${details.join(" · ")}` : ""}`
    : state.flowConfigured
      ? `Đã ghi nhớ cookie Google Flow${details.length ? ` · ${details.join(" · ")}` : ""}`
      : (connection.message || "Chưa lưu cookie Google Flow");
  $("#editFlowBtn").textContent = state.flowConfigured ? "Thêm mới / Thay đổi" : "Thêm cookie";
  $("#disconnectFlowBtn").disabled = !state.flowConfigured;
  const select = $("#videoModelInput");
  const previous = select.value || state.project?.settings?.video_model || "veo-3.1-lite-lower-priority";
  if (state.flowModels.length) {
    select.innerHTML = state.flowModels.map((model) => `<option value="${escapeHtml(model.id)}">${escapeHtml(model.display_name)}${model.note ? ` · ${escapeHtml(model.note)}` : ""}</option>`).join("");
    if (state.flowModels.some((model) => model.id === previous)) select.value = previous;
  }
  const top = $("#providerStatus");
  top.childNodes[top.childNodes.length - 1].textContent = state.flowAuthenticated
    ? " Google Flow sẵn sàng"
    : state.flowConfigured ? " Flow cần kiểm tra" : " Chưa đăng nhập Flow";
}

function setFlowCredentialEditor(open) {
  $("#flowCredentialEditor").classList.toggle("hidden", !open);
  if (!open) {
    $("#flowCookieInput").value = "";
    $("#flowCookieFile").value = "";
  } else setTimeout(() => $("#flowCookieInput").focus(), 0);
}

function toggleFlowConfig() {
  $("#flowConfig").classList.toggle("hidden", $("#providerInput").value !== "google-flow");
}

async function loadFlowStatus(verify = false) {
  try {
    renderFlowConnection(await api(`/api/video/flow/status?verify=${verify}`));
  } catch (error) {
    renderFlowConnection({ configured: false, authenticated: false, flow_cli_available: false, message: error.message, models: [] });
  }
}

async function connectFlow() {
  const cookie = $("#flowCookieInput").value.trim();
  if (!cookie) return toast("Hãy dán cookie hoặc chọn tệp cookies.json", true);
  const button = $("#connectFlowBtn");
  const original = button.textContent;
  button.disabled = true; button.textContent = "Đang xác thực...";
  try {
    const connection = await api("/api/video/flow/connect", {
      method: "POST", body: JSON.stringify({ cookie }),
    });
    renderFlowConnection(connection);
    $("#flowCookieInput").value = "";
    $("#flowCookieFile").value = "";
    setFlowCredentialEditor(false);
    toast("Đã đăng nhập Google Flow bằng cookie");
  } catch (error) {
    toast(error.message, true);
    const line = $("#flowConnection");
    line.classList.add("error"); line.querySelector("span").textContent = error.message;
  } finally {
    $("#flowCookieInput").value = "";
    button.disabled = false; button.textContent = original;
  }
}

async function saveVideoSetup() {
  if (!state.project) return;
  const provider = $("#providerInput").value;
  const videoModel = $("#videoModelInput").value || "veo-3.1-lite-lower-priority";
  const project = await api(`/api/projects/${state.project.id}/video-settings`, {
    method: "PATCH",
    body: JSON.stringify({ provider, video_model: videoModel }),
  });
  renderProject(project);
}

async function uploadReference() {
  const scene = activeScene();
  const file = $("#referenceFile").files[0];
  if (!scene || !file) return toast("Hãy chọn ảnh JPEG, PNG hoặc WebP", true);
  try {
    const response = await fetch(`/api/projects/${state.project.id}/scenes/${scene.id}/reference`, {
      method: "POST", headers: { "Content-Type": file.type, ...(state.sessionToken ? { "X-Flow-Studio-Session": state.sessionToken } : {}) }, body: file,
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload.detail || `Lỗi ${response.status}`);
    }
    renderProject(await response.json());
    $("#referenceFile").value = "";
    toast("Đã gắn ảnh tham chiếu cho scene");
  } catch (error) { toast(error.message, true); }
}

async function loadXKiroStatus() {
  try {
    const status = await api("/api/ai/xkiro/status");
    renderXKiroConnection(status);
    if (status.configured) {
      try {
        const models = await api("/api/ai/xkiro/models");
        renderXKiroConnection({
          ...status,
          models,
          model_count: models.length,
          free_model_count: models.filter((model) => model.access_tier === "free").length,
        });
      } catch (error) {
        const line = $("#xkiroConnection");
        line.classList.add("error");
        line.querySelector("span").textContent = `API key đã ghi nhớ · chưa tải được model: ${error.message}`;
      }
    }
  } catch (error) {
    renderXKiroConnection({ configured: false, models: [] });
    const line = $("#xkiroConnection");
    line.classList.add("error");
    line.querySelector("span").textContent = error.message;
  }
}

async function connectXKiro() {
  const key = $("#xkiroKeyInput").value.trim();
  if (!key) return toast("Hãy nhập API key xKiro", true);
  const button = $("#connectXkiroBtn");
  const original = button.textContent;
  button.disabled = true; button.textContent = "Đang xác thực...";
  try {
    const connection = await api("/api/ai/xkiro/connect", {
      method: "POST", body: JSON.stringify({ api_key: key }),
    });
    $("#xkiroKeyInput").value = "";
    renderXKiroConnection(connection);
    setXKiroCredentialEditor(false);
    toast(`Đã tải ${connection.model_count} model xKiro (${connection.free_model_count} free)`);
  } catch (error) {
    const line = $("#xkiroConnection");
    line.classList.remove("connected"); line.classList.add("error");
    line.querySelector("span").textContent = error.message;
    toast(error.message, true);
  } finally {
    $("#xkiroKeyInput").value = "";
    button.disabled = false; button.textContent = original;
  }
}

async function createProject(autoPipeline = false) {
  const form = $("#newProjectForm");
  if (!form.reportValidity()) return;
  if ($("#analysisProviderInput").value === "xkiro") {
    if (!state.xkiroConnected) return toast("Hãy kết nối API key xKiro trước", true);
    if (!$("#analysisModelInput").value) return toast("Hãy chọn model xKiro", true);
  }
  if (autoPipeline && $("#providerInput").value === "google-flow" && !state.flowConfigured) {
    return toast("Hãy thêm và xác thực cookie Google Flow trước khi chạy Auto pipeline", true);
  }
  const button = autoPipeline ? $("#autoPipelineSubmit") : $("#analyzeSubmit");
  const original = button.textContent;
  $("#analyzeSubmit").disabled = true; $("#autoPipelineSubmit").disabled = true;
  button.textContent = "Đang phân tích...";
  try {
    const job = await api(`/api/analysis/jobs?auto_pipeline=${autoPipeline}`, { method: "POST", body: JSON.stringify(projectPayload()) });
    state.analysisJobId = job.id;
    renderAnalysisJob(job);
    const finished = await waitForAnalysisJob(job.id);
    if (!finished) return;
    if (finished.status !== "completed" || !finished.project) {
      throw new Error(finished.error || "Phân tích không hoàn tất");
    }
    const project = await api(`/api/projects/${finished.project.id}`);
    state.queueActive = autoPipeline;
    $("#newProjectModal").close();
    renderProject(project, false);
    setWorkspaceView("storyboard");
    toast(autoPipeline ? "Auto pipeline đã bắt đầu" : `Phân tích xong · đã tạo ${project.scenes.length} scene`);
  } catch (error) { toast(error.message, true); }
  finally {
    state.analysisJobId = null;
    $("#cancelAnalysisBtn").classList.add("hidden");
    $("#analyzeSubmit").disabled = false; $("#autoPipelineSubmit").disabled = false;
    button.textContent = original;
  }
}

async function generate(sceneIds, forceRerender = false) {
  if (!state.project) return;
  if (state.project.settings.provider === "google-flow" && !state.flowConfigured) {
    $("#videoSetupModal").showModal();
    return toast("Hãy thiết lập Google Flow trước khi tạo video", true);
  }
  try {
    const project = await api(`/api/projects/${state.project.id}/generate`, { method: "POST", body: JSON.stringify({ scene_ids: sceneIds, force_rerender: forceRerender }) });
    state.queueActive = true;
    renderProject(project);
    toast(`Đã đưa ${sceneIds.length || project.scenes.length} scene vào hàng đợi`);
  } catch (error) { toast(error.message, true); }
}

function updatePolling() {
  clearInterval(state.poller);
  if (!state.project) return;
  const processing = state.project.scenes.some((scene) => ["Preparing", "Generating", "QC", "Paused"].includes(scene.status));
  const queuedWaiting = state.queueActive && state.project.scenes.some((scene) => scene.status === "Waiting");
  const merging = state.project.final_video?.status === "Merging";
  const active = processing || queuedWaiting || merging;
  if (!active) state.queueActive = false;
  if (active) {
    state.poller = setInterval(async () => {
      try {
        const project = await api(`/api/projects/${state.project.id}`);
        renderProject(project);
      } catch (_) {}
    }, 1200);
  }
}

async function listProjects() {
  try {
    const projects = await api("/api/projects");
    $("#projectList").innerHTML = projects.length ? projects.map((item) => `<article class="project-row"><div><strong>${escapeHtml(item.name)}</strong><small>${item.scene_count} scene · continuity ${item.continuity_score}% · ${new Date(item.updated_at).toLocaleString("vi-VN")}</small></div><button class="secondary-btn" data-open-project="${item.id}">Mở</button></article>`).join("") : `<div class="empty-copy">Chưa có dự án đã lưu.</div>`;
    $$('[data-open-project]').forEach((button) => button.onclick = async () => {
      renderProject(await api(`/api/projects/${button.dataset.openProject}`), false);
      setWorkspaceView("storyboard");
      $("#projectsModal").close();
    });
    return projects;
  } catch (error) { toast(error.message, true); return []; }
}

function download(url) {
  const link = document.createElement("a"); link.href = url; link.click();
}

function bindEvents() {
  $$("#workspaceSwitcher button").forEach((button) => {
    button.onclick = () => setWorkspaceView(button.dataset.workspaceView);
  });
  $("#newProjectBtn").onclick = () => {
    $("#newProjectForm").reset();
    state.analysisLogs = [];
    $("#analysisLogPanel").classList.add("hidden");
    toggleXKiroConfig();
    openAnalysisStep();
  };
  $("#openProjectsBtn").onclick = async () => { await listProjects(); $("#projectsModal").showModal(); };
  $("#closeProjectsBtn").onclick = () => $("#projectsModal").close();
  $("#newProjectForm").addEventListener("submit", (event) => {
    event.preventDefault();
    if (event.submitter?.value === "cancel") {
      if (!state.project) return toast("Hãy phân tích nội dung để bắt đầu phiên làm việc", true);
      $("#newProjectModal").close(); return;
    }
    createProject(false);
  });
  $("#newProjectModal").addEventListener("cancel", (event) => {
    if (!state.project) event.preventDefault();
  });
  $("#autoPipelineSubmit").onclick = () => createProject(true);
  $("#analysisProviderInput").onchange = toggleXKiroConfig;
  $("#editXkiroBtn").onclick = () => setXKiroCredentialEditor(true);
  $("#cancelXkiroEditBtn").onclick = () => setXKiroCredentialEditor(false);
  $("#connectXkiroBtn").onclick = connectXKiro;
  $("#disconnectXkiroBtn").onclick = async () => {
    try {
      renderXKiroConnection(await api("/api/ai/xkiro", { method: "DELETE" }));
      setXKiroCredentialEditor(false);
      toast("Đã xóa API key xKiro khỏi máy");
    } catch (error) { toast(error.message, true); }
  };
  $("#cancelAnalysisBtn").onclick = cancelAnalysis;
  $("#clearAnalysisLogBtn").onclick = () => {
    if (state.analysisJobId) return toast("Không thể xóa log khi đang phân tích", true);
    state.analysisLogs = [];
    $("#analysisLogEntries").innerHTML = `<div class="empty-copy">Chưa có sự kiện.</div>`;
    $("#analysisLogPanel").classList.add("hidden");
  };
  $("#copyAnalysisLogBtn").onclick = async () => {
    const text = state.analysisLogs.map((entry) => `[${entry.at || ""}] ${String(entry.level || "info").toUpperCase()} ${entry.message || ""}`).join("\n");
    if (!text) return toast("Chưa có log để sao chép", true);
    await navigator.clipboard.writeText(text); toast("Đã sao chép log phân tích");
  };
  $("#providerInput").onchange = toggleFlowConfig;
  const openVideoSetup = () => {
    if (!state.project) return toast("Hãy phân tích nội dung trước", true);
    $("#videoSetupModal").showModal();
  };
  $("#providerStatus").onclick = openVideoSetup;
  $("#videoSetupBtn").onclick = openVideoSetup;
  $("#videoSetupForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (event.submitter?.value === "cancel") { $("#videoSetupModal").close(); return; }
    try {
      await saveVideoSetup();
      $("#videoSetupModal").close();
      toast("Đã lưu thiết lập tạo video");
    } catch (error) { toast(error.message, true); }
  });
  $("#connectFlowBtn").onclick = connectFlow;
  $("#editFlowBtn").onclick = () => setFlowCredentialEditor(true);
  $("#cancelFlowEditBtn").onclick = () => setFlowCredentialEditor(false);
  $("#verifyFlowBtn").onclick = () => loadFlowStatus(true);
  $("#disconnectFlowBtn").onclick = async () => {
    try { renderFlowConnection(await api("/api/video/flow", { method: "DELETE" })); setFlowCredentialEditor(false); toast("Đã xóa cookie Google Flow khỏi máy"); }
    catch (error) { toast(error.message, true); }
  };
  $("#flowCookieFile").onchange = async (event) => {
    const file = event.target.files[0];
    if (file) $("#flowCookieInput").value = await file.text();
  };
  $("#uploadReferenceBtn").onclick = uploadReference;
  $("#sceneForm").addEventListener("submit", saveScene);
  $("#sceneLockBtn").onclick = toggleSceneLock;
  $("#savePromptBtn").onclick = async () => {
    const scene = activeScene(); if (!scene) return;
    try { renderProject(await api(`/api/projects/${state.project.id}/scenes/${scene.id}`, { method: "PATCH", body: JSON.stringify({ flow_prompt: $("#editFlowPrompt").value }) })); toast("Đã lưu Flow prompt"); }
    catch (error) { toast(error.message, true); }
  };
  $("#copyPromptBtn").onclick = async () => { await navigator.clipboard.writeText($("#editFlowPrompt").value); toast("Đã sao chép prompt"); };
  $("#generateSceneBtn").onclick = () => activeScene() && generate([activeScene().id], true);
  $("#generateAllBtn").onclick = () => generate([]);
  $("#mergeAllBtn").onclick = () => startFinalVideoMerge(false);
  $("#startMergeBtn").onclick = () => startFinalVideoMerge(true);
  $("#closeFinalVideoBtn").onclick = () => $("#finalVideoModal").close();
  $("#downloadFinalVideoBtn").onclick = () => {
    const url = state.project?.final_video?.result_url;
    if (url) download(url);
  };
  $("#generateSelectedBtn").onclick = () => {
    const ids = state.project.scenes.filter((scene) => scene.selected).map((scene) => scene.id);
    if (!ids.length) return toast("Hãy chọn ít nhất một scene", true);
    generate(ids, true);
  };
  const checkContinuity = async () => {
    try { renderProject(await api(`/api/projects/${state.project.id}/continuity?auto_fix=true`, { method: "POST" })); toast("Đã đồng bộ start/end frame và kiểm tra continuity"); }
    catch (error) { toast(error.message, true); }
  };
  $("#continuityBtn").onclick = checkContinuity; $("#checkSceneBtn").onclick = checkContinuity;
  $("#analyzeAgainBtn").onclick = () => {
    $("#projectNameInput").value = state.project.name;
    $("#storyInput").value = state.project.original_text;
    $("#aspectInput").value = state.project.settings.aspect_ratio;
    $("#durationInput").value = state.project.settings.scene_duration;
    $("#styleInput").value = state.project.settings.style;
    $("#providerInput").value = state.project.settings.provider;
    $("#videoModelInput").value = state.project.settings.video_model || "veo-3.1-lite-lower-priority";
    toggleFlowConfig();
    $("#analysisProviderInput").value = state.project.settings.analysis_provider || "offline";
    toggleXKiroConfig();
    if (state.project.settings.analysis_model && state.xkiroModels.length) {
      $("#analysisModelInput").value = state.project.settings.analysis_model;
    }
    openAnalysisStep();
  };
  $("#exportBtn").onclick = () => download(`/api/projects/${state.project.id}/export.json`);
  $("#sceneSearch").oninput = (event) => { state.search = event.target.value; renderScenes(); };
  $$(".filter-btn").forEach((button) => button.onclick = () => { $$(".filter-btn").forEach((item) => item.classList.remove("active")); button.classList.add("active"); state.sceneFilter = button.dataset.filter; renderScenes(); });
  $$("#projectTabs button").forEach((button) => button.onclick = () => switchTab("#projectTabs", ".tab-content", button, "tab", "panel"));
  $$("#editorTabs button").forEach((button) => button.onclick = () => switchTab("#editorTabs", ".editor-page", button, "editorTab", "editorPanel"));
  $("#queueBtn").onclick = openQueue; $("#closeQueueBtn").onclick = closeQueue; $("#scrim").onclick = closeQueue;
  $("#pauseQueueBtn").onclick = async () => {
    const project = await api(`/api/projects/${state.project.id}/queue/pause`, { method: "POST" });
    const activeRender = project.scenes.some((scene) => ["Preparing", "Generating"].includes(scene.status));
    renderProject(project);
    toast(activeRender ? "Đã tạm dừng hàng đợi; scene đang render sẽ hoàn tất trước khi dừng" : "Đã tạm dừng hàng đợi");
  };
  $("#resumeQueueBtn").onclick = async () => { renderProject(await api(`/api/projects/${state.project.id}/queue/resume`, { method: "POST" })); toast("Đã tiếp tục hàng đợi"); };
}

function switchTab(nav, panels, button, buttonKey, panelKey) {
  $$(`${nav} button`).forEach((item) => item.classList.remove("active"));
  $$(panels).forEach((item) => item.classList.remove("active"));
  button.classList.add("active");
  const name = button.dataset[buttonKey];
  document.querySelector(`[data-${panelKey.replace(/[A-Z]/g, (m) => `-${m.toLowerCase()}`)}="${name}"]`).classList.add("active");
}

function openQueue() { $("#queueDrawer").classList.add("open"); $("#scrim").classList.add("open"); renderQueue(); }
function closeQueue() { $("#queueDrawer").classList.remove("open"); $("#scrim").classList.remove("open"); }

async function boot() {
  bindEvents();
  toggleXKiroConfig();
  toggleFlowConfig();
  await loadSession();
  openAnalysisStep();
  await Promise.all([loadXKiroStatus(), loadFlowStatus(false)]);
}

boot();
