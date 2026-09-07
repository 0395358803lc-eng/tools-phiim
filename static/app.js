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
  masterBatchActive: false,
  activity: {
    active: false,
    status: "idle",
    title: "",
    stage: "",
    detail: "",
    progress: 0,
    token: 0,
  },
  xkiroConnected: false,
  xkiroModels: [],
  render: {
    provider: "unconfigured",
    configured: false,
    message: "Chưa cấu hình render engine.",
    availableProviders: [],
    configuredProviders: [],
    providerDetails: {},
  },
  googleFlow: { configured: false, chrome_available: false, active: null, pending: null },
  analysisJobId: null,
  analysisLogs: [],
  workspaceView: "storyboard",
  sessionId: "",
  workspacePath: "",
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

async function api(path, options = {}) {
  const {
    activitySilent = false,
    activityLabel = "",
    ...fetchOptions
  } = options;
  const method = String(fetchOptions.method || "GET").toUpperCase();
  const mutation = ["POST", "PUT", "PATCH", "DELETE"].includes(method);
  const autoActivity = mutation && !activitySilent && !state.activity.active;
  if (autoActivity) {
    beginActivity(
      activityLabel || requestActivityLabel(path, method),
      "Đang gửi yêu cầu tới TH Media...",
      path,
      12,
    );
  }

  try {
    const response = await fetch(path, {
      headers: {
        "Content-Type": "application/json",
        ...(state.sessionToken ? { "X-Flow-Studio-Session": state.sessionToken } : {}),
        ...(fetchOptions.headers || {}),
      },
      ...fetchOptions,
    });
    if (!response.ok) {
      let message = `Lỗi ${response.status}`;
      try {
        const data = await response.json();
        message = typeof data.detail === "string" ? data.detail : message;
      } catch (_) {}
      throw new Error(message);
    }
    const payload = response.status === 204 ? null : await response.json();
    if (autoActivity) {
      completeActivity("Yêu cầu đã hoàn tất.", path);
    }
    return payload;
  } catch (error) {
    if (autoActivity) {
      failActivity(error.message || "Yêu cầu thất bại", path);
    }
    throw error;
  }
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
  syncRenderSetupForm();
  $$("#exportBtn, #analyzeAgainBtn, #continuityBtn").forEach((button) => button.disabled = false);
  updateRenderControls();
  renderBible();
  renderSettings();
  renderScenes();
  renderEditor();
  renderQueue();
  renderFinalVideo();
  syncActivityFromProject();
  updatePolling();
}

function masterReferenceGenerationReady() {
  return Boolean(
    state.project
    && state.xkiroConnected
    && state.project.settings?.vision_model
    && projectImageModelReady()
    && state.render.configuredProviders.includes("google-flow-browser")
    && !state.masterBatchActive
  );
}

function masterReferenceGateReady(reference) {
  if (!reference || !state.project) return false;
  const threshold = reference.entity_type === "character"
    ? Math.max(90, Number(state.project.settings?.quality_threshold || 0))
    : Math.max(85, Number(state.project.settings?.quality_threshold || 0));
  const blockingCodes = {
    character: new Set([
      "identity_mismatch", "identity_not_established", "appearance_mismatch",
      "age_mismatch", "hair_mismatch", "wardrobe_mismatch", "wardrobe_state",
      "scene_specific_background", "background_contamination",
      "scene_specific_lighting", "lighting_mood", "action_pose",
      "transient_story_state", "readable_text", "logo_or_brand",
    ]),
    location: new Set([
      "layout_mismatch", "missing_spatial_anchor", "ambiguous_spatial_anchors",
      "scene_specific_weather", "scene_specific_lighting", "transient_story_prop",
      "people_present", "readable_text", "logo_or_brand",
    ]),
    prop: new Set([
      "identity_mismatch", "shape_mismatch", "material_mismatch", "color_mismatch",
      "state_mismatch", "background_contamination", "readable_text", "logo_or_brand",
    ]),
  };
  const typeCodes = blockingCodes[reference.entity_type] || new Set();
  const hasBlockingIssue = (reference.vision_issues || []).some((issue) => (
    issue.severity === "error"
    || typeCodes.has(String(issue.code || "").toLowerCase())
  ));
  const selectedVision = state.project.settings?.vision_model || "";
  const visionEvidenceCurrent = (
    !selectedVision || reference.vision_model === selectedVision
  );
  return (
    reference.status === "approved"
    && Boolean(reference.approved_reference)
    && Number(reference.vision_score || 0) >= threshold
    && visionEvidenceCurrent
    && !hasBlockingIssue
  );
}

function projectMasterGateReady() {
  const references = state.project?.visual_bible?.references || [];
  return references.every((reference) => masterReferenceGateReady(reference));
}

function masterReferenceIssueSummary(reference) {
  const issues = reference?.vision_issues || [];
  if (!issues.length) return "";
  return issues
    .slice(0, 2)
    .map((issue) => `${issue.code}: ${issue.message || issue.severity}`)
    .join(" · ");
}

function masterReferenceVisionUnavailable(reference) {
  return (reference?.vision_issues || []).some(
    (issue) => issue.code === "VISION_UNAVAILABLE",
  );
}

function renderBible() {
  const project = state.project;
  const bible = project.story_bible;
  const byEntity = new Map(
    (project.visual_bible?.references || []).map((reference) => [reference.entity_id, reference]),
  );
  const masterPanel = (entityId) => {
    const reference = byEntity.get(entityId);
    if (!reference) {
      return `<div class="master-reference missing"><strong>MASTER MISSING</strong><span>Chưa có Visual Reference cho entity này.</span></div>`;
    }
    const status = reference.status || "missing";
    const gateReady = masterReferenceGateReady(reference);
    const usage = project.scenes.filter((scene) => {
      const ids = [
        ...(scene.visual_plan?.character_reference_ids || []),
        ...(scene.visual_plan?.prop_reference_ids || []),
        ...(scene.visual_plan?.location_reference_id ? [scene.visual_plan.location_reference_id] : []),
      ];
      return ids.includes(reference.id);
    }).length;
    const statusText = gateReady
      ? "✓ MASTER GATE PASS"
      : status === "approved"
        ? "✕ MASTER GATE FAIL"
        : status === "candidate"
          ? "◌ MASTER CANDIDATE"
          : status === "rejected"
            ? "✕ MASTER REJECTED"
            : "⚠ MASTER MISSING";
    const detail = gateReady
      ? `REUSE an toàn trong ${usage} scene · Project Master đã đạt chuẩn`
      : status === "approved"
        ? "Status cũ là Approved nhưng không đạt Master Gate mới · cần QC/tạo lại"
        : status === "candidate"
          ? "Đã lưu ảnh candidate · Chưa đạt Master Gate nên scene vẫn bị khóa"
          : status === "rejected"
            ? "Ảnh gần nhất không đạt Master QC · Cần tạo lại Master"
            : `Cần một Master cấp Project trước khi ${usage} scene có thể tạo ảnh`;
    const current = reference.approved_reference
      ? `Master hiện tại: ${escapeHtml(reference.approved_reference)}`
      : "Chưa có Master đạt Gate";
    const vision = reference.vision_model
      ? `Master QC: ${reference.vision_score || 0}/100 · ${escapeHtml(reference.vision_model)}`
      : "Master QC: chưa chạy";
    const issues = masterReferenceIssueSummary(reference);
    const canGenerate = masterReferenceGenerationReady() && !gateReady;
    const generationLabel = gateReady
      ? "Master Gate PASS"
      : status === "approved" || status === "rejected"
        ? "Tạo lại Master"
        : "Tạo Master";
    const displayStatus = gateReady ? "approved" : status === "approved" ? "rejected" : status;
    return `<div class="master-reference ${escapeHtml(displayStatus)}">
      <div class="master-reference-head">
        <div><code>${escapeHtml(reference.id)}</code><strong>${statusText}</strong></div>
        <span>${usage} scene</span>
      </div>
      <p>${detail}</p>
      <small>${current}</small>
      <small>${vision}</small>
      ${issues ? `<small class="master-vision-issues">${escapeHtml(issues)}</small>` : ""}
      <div class="master-reference-actions">
        <input class="master-ref-file" data-master-ref="${escapeHtml(reference.id)}" type="file" accept="image/jpeg,image/png,image/webp">
        <button class="secondary-btn master-ref-upload" data-master-ref="${escapeHtml(reference.id)}" type="button">${status === "approved" ? "Thay Master" : "Tải Master"}</button>
        <button class="ghost-btn master-ref-generate" data-master-ref="${escapeHtml(reference.id)}" type="button" ${canGenerate ? "" : "disabled"}>${generationLabel}</button>
      </div>
    </div>`;
  };

  $("#storyBible").innerHTML = `<div class="story-card"><strong>${escapeHtml(bible.main_theme)}</strong><p>${escapeHtml(bible.synopsis)}</p><small>${escapeHtml(bible.genre)} · ${escapeHtml(bible.mood)}</small></div>`;
  $("#characterCount").textContent = project.characters.length;
  $("#characterBible").innerHTML = project.characters.map((item) => `<article class="bible-card master-bible-card"><header><strong>${escapeHtml(item.name)}</strong><code>${escapeHtml(item.id)}</code></header><p>${escapeHtml(item.gender)}, ${escapeHtml(item.estimated_age)} · ${escapeHtml(item.clothing)} · ${escapeHtml(item.identifying_features)}</p>${masterPanel(item.id)}</article>`).join("") || `<div class="empty-copy">Không có nhân vật.</div>`;
  $("#locationCount").textContent = project.locations.length;
  $("#locationBible").innerHTML = project.locations.map((item) => `<article class="bible-card master-bible-card"><header><strong>${escapeHtml(item.name)}</strong><code>${escapeHtml(item.id)}</code></header><p>${escapeHtml(item.architecture)} · ${escapeHtml(item.space)} · ${escapeHtml(item.lighting)}</p>${masterPanel(item.id)}</article>`).join("") || `<div class="empty-copy">Không có bối cảnh.</div>`;
  $("#propCount").textContent = project.props.length;
  $("#propBible").innerHTML = project.props.map((item) => `<article class="bible-card master-bible-card"><header><strong>${escapeHtml(item.name)}</strong><code>${escapeHtml(item.id)}</code></header><p>${escapeHtml(item.description)} · ${escapeHtml(item.initial_location)}</p>${masterPanel(item.id)}</article>`).join("") || `<div class="empty-copy">Chưa phát hiện đạo cụ quan trọng.</div>`;

  $$(".master-ref-upload").forEach((button) => {
    button.onclick = () => uploadMasterReference(button.dataset.masterRef || "");
  });
  $$(".master-ref-generate").forEach((button) => {
    button.onclick = () => generateMasterReferenceInteractive(button.dataset.masterRef || "");
  });
  const missing = (project.visual_bible?.references || []).filter(
    (reference) => !masterReferenceGateReady(reference),
  );
  const masterVisionSelect = $("#masterVisionModelInput");
  const masterVisionSave = $("#saveMasterVisionModelBtn");
  if (masterVisionSelect && masterVisionSave) {
    const visionModels = (state.xkiroModels || [])
      .filter((model) => Boolean(model.capabilities?.vision))
      .sort((left, right) => {
        const tier = Number(left.access_tier !== "free") - Number(right.access_tier !== "free");
        return tier || left.display_name.localeCompare(right.display_name);
      });
    masterVisionSelect.innerHTML = visionModels.length
      ? '<option value="">Chọn Vision QC model...</option>'
        + visionModels.map((model) => {
          const tier = (model.access_tier || "unknown").toUpperCase();
          const badge = tier === "FREE" ? "[FREE]" : tier === "PAID" ? "[PAID]" : `[${tier}]`;
          return `<option value="${escapeHtml(model.id)}">${escapeHtml(badge)} ${escapeHtml(model.display_name)} · ${escapeHtml(model.id)}</option>`;
        }).join("")
      : '<option value="">Kết nối xKiro để tải Vision models</option>';
    const selectedVision = project.settings?.vision_model || "";
    if (visionModels.some((model) => model.id === selectedVision)) {
      masterVisionSelect.value = selectedVision;
    }
    masterVisionSelect.disabled = !state.xkiroConnected || !visionModels.length || state.masterBatchActive;
    masterVisionSave.disabled = (
      !state.xkiroConnected
      || !visionModels.length
      || !masterVisionSelect.value
      || masterVisionSelect.value === selectedVision
      || state.masterBatchActive
    );
    masterVisionSelect.onchange = () => {
      masterVisionSave.disabled = (
        !masterVisionSelect.value
        || masterVisionSelect.value === (state.project?.settings?.vision_model || "")
        || state.masterBatchActive
      );
    };
    masterVisionSave.onclick = saveMasterVisionModel;
  }

  const batchButton = $("#generateMissingMastersBtn");
  if (batchButton) {
    batchButton.disabled = !masterReferenceGenerationReady() || !missing.length;
    batchButton.textContent = missing.length
      ? `Tạo/khắc phục Master chưa đạt Gate · ${missing.length}`
      : "Project Master Gate PASS";
    batchButton.onclick = generateMissingMasterReferences;
  }
  const batchStatus = $("#masterBatchStatus");
  if (batchStatus && !state.masterBatchActive) {
    if (!state.xkiroConnected) {
      batchStatus.textContent = "Cần cấu hình xKiro trước khi tạo Master để Master QC hoạt động.";
    } else if (!project.settings?.vision_model) {
      batchStatus.textContent = "Cần chọn Vision QC model trong cài đặt AI trước khi tạo Master.";
    } else if (!project.settings?.image_model) {
      batchStatus.textContent = "Cần chọn và Lưu Google Flow Image Model trước khi tạo Master.";
    } else if (!state.render.configuredProviders.includes("google-flow-browser")) {
      batchStatus.textContent = "Cần Google Flow Session active trước khi tạo Master.";
    } else if (!missing.length) {
      batchStatus.textContent = "Project Master Gate PASS · production scene đã được mở.";
    } else {
      batchStatus.textContent = `${missing.length} Master chưa đạt Gate · QC downstream tuần tự, fail-closed.`;
    }
  }
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
    ["Vision QC model", settings.vision_model || "Tự chọn model Vision khả dụng"],
    ["Provider", settings.provider],
    ["Video model", settings.video_model || "Chưa cấu hình"],
  ];
  $("#settingsView").innerHTML = entries.map(([label, value]) => `<div class="setting"><span>${label}</span><strong>${escapeHtml(value)}</strong></div>`).join("") + `<button class="secondary-btn full" id="exportPromptsBtn" style="grid-column:1/-1">Export all render prompts (.zip)</button>`;
  $("#exportPromptsBtn").onclick = () => download(`/api/projects/${state.project.id}/render-prompts.zip`);
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
  $("#editRenderPrompt").value = scene.render_prompt;
  $("#startState").value = JSON.stringify(scene.start_state, null, 2);
  $("#endState").value = JSON.stringify(scene.end_state, null, 2);
  const protectedControls = [
    "editLocation", "editDuration", "editCharacters", "editSource", "editAction",
    "editCamera", "editLighting", "editAtmosphere", "editVoiceover", "editDialogues",
    "editVisualPrompt", "editRenderPrompt", "startState", "endState", "saveSceneBtn",
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
  renderImagePlan(scene);
  renderResult(scene);
}

function renderImagePlan(scene) {
  const plan = scene.image_plan || {};
  const labels = {
    canonical_reanchor: "Tạo composition từ Master References",
    previous_accepted_end_frame: "Kế thừa Last Frame + Master Guards",
    generate_scene_exit_keyframe: "Tạo target/end keyframe từ cùng Master",
  };
  const status = plan.status || "Planned";
  const statusNode = $("#imagePlanStatus");
  statusNode.textContent = status.toUpperCase();
  statusNode.dataset.status = status;

  $("#imageStartStrategy").textContent =
    labels[plan.start_frame_strategy] || plan.start_frame_strategy || "Chưa lập kế hoạch";
  $("#imageStartRequirement").textContent =
    plan.start_frame_requirement || "Chưa có yêu cầu start-frame.";
  $("#imageStartSource").textContent = plan.start_frame_source
    ? `Nguồn hiện tại: ${plan.start_frame_source}`
    : plan.start_frame_strategy === "previous_accepted_end_frame"
      ? "Đang chờ frame cuối Accepted của cảnh trước"
      : "Dùng lại Master References cấp Project để tạo composition của scene";

  $("#imageTargetStrategy").textContent =
    labels[plan.target_frame_strategy] || plan.target_frame_strategy || "Tạo target/end keyframe";
  $("#imageAnchorScene").textContent =
    `Anchor: ${plan.anchor_scene_id || scene.id} · ${plan.dependency_mode || "canonical"}`;

  const refIds = [
    ...(plan.character_reference_ids || []),
    ...(plan.location_reference_id ? [plan.location_reference_id] : []),
    ...(plan.prop_reference_ids || []),
  ];
  const refMap = new Map(
    (state.project.visual_bible?.references || []).map((item) => [item.id, item]),
  );
  $("#imageReferenceCount").textContent = refIds.length;
  $("#imageReferenceList").innerHTML = refIds.length
    ? refIds.map((id) => {
      const reference = refMap.get(id);
      const refStatus = plan.reference_status?.[id] || reference?.status || "missing";
      const approved = reference?.approved_reference || "";
      const badge = refStatus === "approved" ? "✓ REUSE MASTER" : "⚠ MASTER MISSING";
      const detail = refStatus === "approved"
        ? `Master cấp Project: ${approved} · Scene này chỉ reuse, không tạo lại entity`
        : "Scene bị BLOCKED cho đến khi Master cấp Project được Vision duyệt";
      return `<article class="image-reference-item ${escapeHtml(refStatus)}">
        <div><code>${escapeHtml(id)}</code><strong>${escapeHtml(reference?.name || id)}</strong></div>
        <span>${badge}</span>
        <small>${escapeHtml(detail)}</small>
      </article>`;
    }).join("")
    : `<div class="empty-copy">Scene này không yêu cầu reference entity riêng.</div>`;

  $("#imageIdentityLock").textContent = plan.identity_lock || "—";
  $("#imageCompositionLock").textContent = plan.composition_lock || "—";
  $("#startImagePrompt").value = plan.start_frame_prompt || "";
  $("#targetImagePrompt").value = plan.target_frame_prompt || "";
  $("#imageNegativePrompt").textContent = plan.negative_prompt || "—";

  const startAvailable = Boolean(
    plan.generated_start_frame || plan.start_frame_source || scene.reference_image,
  );
  const targetAvailable = Boolean(plan.generated_target_frame);
  const version = encodeURIComponent(
    plan.generated_target_frame || plan.generated_start_frame || plan.plan_hash || "current",
  );
  $("#sceneStartImagePreview").classList.toggle("empty-copy", !startAvailable);
  $("#sceneStartImagePreview").innerHTML = startAvailable
    ? `<img src="/api/projects/${state.project.id}/scenes/${scene.id}/image/start?v=${version}" alt="Start frame ${escapeHtml(scene.id)}">`
    : "Chưa có ảnh Start";
  $("#sceneTargetImagePreview").classList.toggle("empty-copy", !targetAvailable);
  $("#sceneTargetImagePreview").innerHTML = targetAvailable
    ? `<img src="/api/projects/${state.project.id}/scenes/${scene.id}/image/target?v=${version}" alt="Target frame ${escapeHtml(scene.id)}">`
    : "Chưa có ảnh Target";

  syncSceneImageModelControl();
  const generateImage = $("#generateImageBtn");
  if (generateImage) {
    const provider = state.project?.settings?.provider || "unconfigured";
    const detail = state.render.providerDetails[provider] || {};
    const missingMasters = missingSceneMasterReferences(scene);
    const imageReady = (
      projectImageModelReady()
      && Array.isArray(detail.image_models)
      && detail.image_models.length > 0
    );
    generateImage.disabled = !imageReady;
    generateImage.title = imageReady
      ? missingMasters.length
        ? `Sẽ tự tạo và Vision duyệt ${missingMasters.length} Master còn thiếu trước khi tạo Start/Target.`
        : "Tạo Start/Target keyframe theo Scene Image Continuity Plan."
      : !projectImageModelReady()
        ? "Hãy chọn và Lưu Google Flow Image Model trước."
        : "Provider hiện tại chưa có image renderer sẵn sàng.";
    generateImage.textContent = imageReady
      ? missingMasters.length
        ? `Tạo ảnh cảnh · Tự tạo ${missingMasters.length} Master`
        : "Tạo ảnh cảnh"
      : !projectImageModelReady()
        ? "Tạo ảnh cảnh · Chưa lưu model ảnh"
        : "Tạo ảnh cảnh · Chưa có renderer ảnh";
  }
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
    stage.innerHTML = `<div class="film-placeholder"><span>✓</span><p>Đã hoàn tất (${escapeHtml(scene.provider_job_id)})</p><small>${scene.result_url.startsWith("mock:") ? "Kết quả mô phỏng — chỉ dùng cho kiểm thử nội bộ." : escapeHtml(scene.result_url)}</small></div>`;
  } else {
    stage.innerHTML = `<div class="film-placeholder"><span>${scene.status === "Generating" ? "…" : "▶"}</span><p>${escapeHtml(scene.status)} · ${scene.progress}%</p></div>`;
  }
  const quality = scene.quality;
  const metrics = quality ? [
    ["Readiness", quality.score],
    ["Nhân vật", quality.character],
    ["Bối cảnh", quality.location],
    ["Timeline", quality.temporal],
  ] : [];
  if (scene.visual_qc && scene.visual_qc.status !== "Pending") {
    metrics.push([
      "Vision QC",
      `${scene.visual_qc.score} · ${scene.visual_qc.model_id || "chưa có model"}`,
    ]);
  }
  $("#qualityCard").innerHTML = metrics.map(([label, value]) =>
    `<div class="quality-metric"><strong>${escapeHtml(String(value))}</strong><span>${label}</span></div>`
  ).join("");
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
  $$(".retry-btn").forEach((button) => button.onclick = () => generate([button.dataset.retry], button.dataset.force === "true"));
}

function finalVideoReady() {
  if (!state.project?.scenes?.length) return false;
  return ["Ready", "Completed"].includes(state.project.final_video?.status || "NotReady");
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
  beginActivity(
    "Ghép video tổng",
    "Đang khởi động FFmpeg merge...",
    `${state.project.scenes.length} scene`,
    5,
  );
  state.activity.kind = "merge";
  try {
    const project = await api(`/api/projects/${state.project.id}/final-video`, { method: "POST" });
    renderProject(project);
    updateActivity(
      "Merge job đã bắt đầu · đang chờ FFmpeg...",
      Math.max(8, Number(project.final_video?.progress || 8)),
      `${project.final_video?.scene_count || project.scenes.length} scene`,
    );
    toast("Đã bắt đầu ghép toàn bộ video");
  } catch (error) {
    failActivity(error.message || "Ghép video thất bại.", "Final video");
    toast(error.message, true);
  }
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
      vision_model: analysisProvider === "xkiro" ? $("#visionModelInput").value : "",
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
  const renderModelOptions = (
    select,
    models,
    previous,
    emptyMessage,
    placeholder = "",
  ) => {
    if (!models.length) {
      select.innerHTML = `<option value="">${escapeHtml(emptyMessage)}</option>`;
      select.disabled = true;
      return;
    }
    const groups = models.reduce((result, model) => {
      (result[model.owned_by] ||= []).push(model);
      return result;
    }, {});
    const groupedOptions = Object.entries(groups).map(([vendor, vendorModels]) =>
      `<optgroup label="${escapeHtml(vendor)}">${vendorModels.map((model) =>
        `<option value="${escapeHtml(model.id)}">[${escapeHtml((model.access_tier || "unknown").toUpperCase())}] ${escapeHtml(model.display_name)} · ${escapeHtml(model.id)} · ${model.context_length ? `${Math.round(model.context_length / 1000)}K` : "? context"}</option>`
      ).join("")}</optgroup>`
    ).join("");
    select.innerHTML = (placeholder && !previous
      ? `<option value="">${escapeHtml(placeholder)}</option>`
      : "") + groupedOptions;
    select.disabled = false;
    if (models.some((model) => model.id === previous)) select.value = previous;
    else if (placeholder) select.value = "";
  };

  const analysisSelect = $("#analysisModelInput");
  const previousAnalysis = analysisSelect.value || state.project?.settings?.analysis_model || "";
  renderModelOptions(
    analysisSelect,
    state.xkiroModels,
    previousAnalysis,
    "Kết nối API key để tải model...",
  );

  const visionModels = state.xkiroModels
    .filter((model) => Boolean(model.capabilities?.vision))
    .sort((left, right) => {
      const tier = Number(left.access_tier !== "free") - Number(right.access_tier !== "free");
      return tier || left.display_name.localeCompare(right.display_name);
    });
  $("#visionModelCount").textContent = visionModels.length;
  const visionSelect = $("#visionModelInput");
  const previousVision = visionSelect.value || state.project?.settings?.vision_model || "";
  renderModelOptions(
    visionSelect,
    visionModels,
    previousVision,
    state.xkiroConnected
      ? "Không có model Vision khả dụng trong catalog xKiro"
      : "Kết nối API key để tải model Vision...",
    "Chọn model Vision...",
  );
  if (state.project) renderBible();
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
    const logs = job.logs || [];
    const last = logs[logs.length - 1];
    const progress = job.status === "completed"
      ? 100
      : Math.min(92, 14 + logs.length * 3);
    updateActivity(
      last?.message || `Phân tích đang chạy · ${job.status}`,
      progress,
      `${logs.length} sự kiện · job ${jobId}`,
    );
    if (["completed", "failed", "cancelled"].includes(job.status)) return job;
  }
  return null;
}

async function cancelAnalysis() {
  if (!state.analysisJobId) return;
  try {
    updateActivity("Đang hủy analysis job...", Math.max(5, state.activity.progress || 5), state.analysisJobId);
    const job = await api(`/api/analysis/jobs/${state.analysisJobId}`, { method: "DELETE" });
    renderAnalysisJob(job);
    completeActivity("Đã hủy phân tích.", state.analysisJobId);
  } catch (error) {
    failActivity(error.message || "Không hủy được phân tích.", state.analysisJobId);
    toast(error.message, true);
  }
}

function toggleXKiroConfig() {
  $("#xkiroConfig").classList.toggle("hidden", $("#analysisProviderInput").value !== "xkiro");
}

function renderRenderStatus(status) {
  state.render = {
    provider: status.provider || "unconfigured",
    configured: Boolean(status.configured),
    message: status.message || "Chưa cấu hình render engine.",
    availableProviders: status.available_providers || [],
    configuredProviders: status.configured_providers || [],
    providerDetails: status.provider_details || {},
  };
  syncRenderSetupForm();
  syncSceneImageModelControl();
  updateRenderControls();
}

function renderProviderLabel(name) {
  if (name === "google-flow-browser") return "Google Flow Browser";
  if (name === "unconfigured") return "Chưa cấu hình";
  return name;
}

function projectRenderReady() {
  const provider = state.project?.settings?.provider || "unconfigured";
  if (!state.render.configuredProviders.includes(provider)) return false;
  const detail = state.render.providerDetails[provider] || {};
  const capabilities = detail.capabilities || {};
  const resolution = state.project?.settings?.resolution;
  const aspect = state.project?.settings?.aspect_ratio;
  if (
    capabilities.video_resolutions?.length
    && !capabilities.video_resolutions.includes(resolution)
  ) return false;
  if (
    capabilities.video_aspect_ratios?.length
    && !capabilities.video_aspect_ratios.includes(aspect)
  ) return false;
  return true;
}

function syncRenderSetupForm(preserveSelection = false) {
  const providerSelect = $("#providerInput");
  const modelSelect = $("#videoModelInput");
  const resolutionSelect = $("#videoResolutionInput");
  if (!providerSelect || !modelSelect || !resolutionSelect) return;

  const previousProvider = providerSelect.value;
  const projectProvider = state.project?.settings?.provider || "";
  const providerNames = (state.render.availableProviders || [])
    .filter((name) => !["mock", "unconfigured"].includes(name));
  providerSelect.innerHTML = '<option value="unconfigured">Chưa cấu hình</option>'
    + providerNames.map((name) => {
      const ready = state.render.configuredProviders.includes(name);
      return `<option value="${escapeHtml(name)}" ${ready ? "" : "disabled"}>${escapeHtml(renderProviderLabel(name))}${ready ? "" : " · cần phiên đăng nhập"}</option>`;
    }).join("");
  const desiredProvider = preserveSelection
    ? previousProvider
    : (projectProvider || previousProvider || state.render.provider);
  providerSelect.value = [...providerSelect.options].some((item) => item.value === desiredProvider)
    ? desiredProvider
    : "unconfigured";

  const detail = state.render.providerDetails[providerSelect.value] || {};
  const models = detail.models || [];
  const preferredModel = (
    state.project?.settings?.provider === providerSelect.value
      ? state.project?.settings?.video_model
      : ""
  ) || models[0] || "";
  modelSelect.innerHTML = models.length
    ? models.map((model) => `<option value="${escapeHtml(model)}">${escapeHtml(model)}</option>`).join("")
    : '<option value="">Không có model khả dụng</option>';
  if (models.includes(preferredModel)) modelSelect.value = preferredModel;

  const resolutions = detail.capabilities?.video_resolutions || ["720p", "360p"];
  const projectResolution = state.project?.settings?.resolution || "720p";
  resolutionSelect.innerHTML = resolutions
    .map((value) => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`)
    .join("");
  resolutionSelect.value = resolutions.includes(projectResolution)
    ? projectResolution
    : (resolutions[0] || "720p");

  const configured = state.render.configuredProviders.includes(providerSelect.value);
  const line = $("#renderConnection");
  if (line) {
    line.classList.toggle("connected", configured);
    line.classList.toggle("error", providerSelect.value !== "unconfigured" && !configured);
    line.querySelector("span").textContent = providerSelect.value === "unconfigured"
      ? "Chưa chọn render provider cho project."
      : configured
        ? `${renderProviderLabel(providerSelect.value)} sẵn sàng`
        : (detail.message || "Provider chưa sẵn sàng.");
  }
  const caps = detail.capabilities || {};
  const meta = $("#renderCapabilityMeta");
  if (meta) {
    const rows = [];
    if (models.length) rows.push(`<div><span>MODELS</span><strong>${models.length}</strong><small>${escapeHtml(models.join(" · "))}</small></div>`);
    if (caps.video_durations?.length) rows.push(`<div><span>DURATION</span><strong>${escapeHtml(caps.video_durations.join("/"))}s</strong><small>Scene dài hơn 10s dùng subclip planner.</small></div>`);
    if (caps.video_input_modes?.length) rows.push(`<div><span>CONTINUITY INPUT</span><strong>${escapeHtml(caps.video_input_modes.join(" / "))}</strong><small>Start/End Frames hoặc Master Ingredients.</small></div>`);
    meta.innerHTML = rows.join("");
  }
  modelSelect.disabled = providerSelect.value === "unconfigured" || !models.length;
  resolutionSelect.disabled = providerSelect.value === "unconfigured" || !resolutions.length;
  $("#saveVideoSetupBtn").disabled = (
    providerSelect.value !== "unconfigured"
    && (!configured || !modelSelect.value || !resolutionSelect.value)
  );
}

async function saveVideoSetup(event) {
  event.preventDefault();
  if (!state.project) return;
  const provider = $("#providerInput").value;
  const videoModel = provider === "unconfigured" ? "" : $("#videoModelInput").value;
  const resolution = provider === "unconfigured"
    ? state.project.settings.resolution
    : ($("#videoResolutionInput").value || state.project.settings.resolution);
  if (provider !== "unconfigured" && !state.render.configuredProviders.includes(provider)) {
    return toast("Provider chưa sẵn sàng. Hãy kiểm tra Google Flow Session.", true);
  }
  try {
    const project = await api(`/api/projects/${state.project.id}/video-settings`, {
      method: "PATCH",
      body: JSON.stringify({
        provider,
        video_model: videoModel,
        resolution,
      }),
    });
    renderProject(project);
    await loadRenderStatus();
    $("#videoSetupModal").close();
    toast(provider === "unconfigured" ? "Đã tắt render provider" : "Đã lưu cấu hình render");
  } catch (error) {
    toast(error.message, true);
  }
}

function renderGoogleFlowSessionStatus(status) {
  state.googleFlow = {
    configured: Boolean(status.configured),
    chrome_available: Boolean(status.chrome_available),
    active: status.active || null,
    pending: status.pending || null,
  };

  const top = $("#googleFlowStatus");
  top.classList.toggle("connected", state.googleFlow.configured);
  top.classList.toggle("pending", Boolean(state.googleFlow.pending));
  top.childNodes[top.childNodes.length - 1].textContent = state.googleFlow.pending
    ? " Đang chờ xác nhận phiên Google Flow mới"
    : state.googleFlow.configured
      ? " Google Flow · phiên đã lưu"
      : " Chưa có phiên Google Flow";

  const line = $("#googleFlowSessionConnection");
  line.classList.toggle("connected", state.googleFlow.configured);
  line.classList.toggle("error", !state.googleFlow.chrome_available);
  line.querySelector("span").textContent = !state.googleFlow.chrome_available
    ? "Không tìm thấy Google Chrome trên máy"
    : state.googleFlow.pending
      ? "Có phiên đăng nhập mới đang chờ bạn xác nhận"
      : state.googleFlow.configured
        ? "Đã có phiên Google Flow active được lưu trong Chrome profile riêng"
        : "Chưa có phiên Google Flow active";

  const active = state.googleFlow.active;
  const pending = state.googleFlow.pending;
  const meta = [];
  if (active) {
    meta.push(`<div><span>ACTIVE SESSION</span><strong>${escapeHtml(active.id)}</strong><small>${active.activated_at ? new Date(active.activated_at).toLocaleString("vi-VN") : "Đã lưu"}</small></div>`);
  }
  if (pending) {
    meta.push(`<div><span>PENDING SESSION</span><strong>${escapeHtml(pending.id)}</strong><small>Đang chờ xác nhận sau khi đăng nhập</small></div>`);
  }
  $("#googleFlowSessionMeta").innerHTML = meta.length
    ? meta.join("")
    : '<div class="empty-copy">Chưa có profile đăng nhập Google Flow.</div>';

  $("#openGoogleFlowSessionBtn").disabled =
    !state.googleFlow.configured || !state.googleFlow.chrome_available;
  $("#newGoogleFlowSessionBtn").disabled =
    !state.googleFlow.chrome_available || Boolean(state.googleFlow.pending);
  $("#googleFlowPendingPanel").classList.toggle("hidden", !pending);
}

async function loadGoogleFlowSessionStatus() {
  try {
    renderGoogleFlowSessionStatus(await api("/api/google-flow/session"));
  } catch (error) {
    renderGoogleFlowSessionStatus({
      configured: false,
      chrome_available: false,
      active: null,
      pending: null,
    });
    toast(`Không đọc được trạng thái Google Flow: ${error.message}`, true);
  }
}

async function createGoogleFlowSession() {
  beginActivity(
    "Google Flow Session",
    "Đang tạo Chrome profile đăng nhập mới...",
    "Không đọc/import cookie · dùng profile Chrome riêng",
    8,
  );
  state.activity.kind = "google-flow-session";
  try {
    const status = await api("/api/google-flow/session/new", { method: "POST" });
    renderGoogleFlowSessionStatus(status);
    completeActivity(
      "Chrome đã mở · đang chờ bạn đăng nhập và xác nhận phiên.",
      status.pending?.id || "Pending Google Flow session",
    );
    toast("Chrome đã mở. Hãy tự đăng nhập Google Flow rồi quay lại xác nhận phiên mới.");
  } catch (error) {
    failActivity(error.message || "Không tạo được Google Flow Session.", "Chrome profile");
    toast(error.message, true);
  }
}

async function openGoogleFlowSession(pending = false) {
  beginActivity(
    "Mở Google Flow",
    pending ? "Đang mở pending Chrome profile..." : "Đang mở active Chrome profile...",
    pending ? "Pending session" : "Active session",
    20,
  );
  state.activity.kind = "google-flow-session";
  try {
    const path = pending
      ? "/api/google-flow/session/pending/open"
      : "/api/google-flow/session/open";
    renderGoogleFlowSessionStatus(await api(path, { method: "POST" }));
    completeActivity("Google Flow đã mở bằng đúng Chrome profile.", pending ? "Pending session" : "Active session");
    toast("Đã mở Google Flow bằng Chrome profile của phiên này");
  } catch (error) {
    failActivity(error.message || "Không mở được Google Flow.", "Chrome profile");
    toast(error.message, true);
  }
}

async function activateGoogleFlowSession() {
  beginActivity("Kích hoạt Google Flow Session", "Đang xác nhận phiên đăng nhập mới...", "", 25);
  state.activity.kind = "google-flow-session";
  try {
    const status = await api("/api/google-flow/session/pending/activate", { method: "POST" });
    renderGoogleFlowSessionStatus(status);
    completeActivity("Đã chuyển sang Google Flow Session mới.", status.active?.id || "Active session");
    toast("Đã chuyển sang phiên Google Flow mới");
  } catch (error) {
    failActivity(error.message || "Không kích hoạt được Google Flow Session.", "Pending session");
    toast(error.message, true);
  }
}

async function cancelGoogleFlowSession() {
  beginActivity("Hủy Google Flow Session mới", "Đang hủy pending profile...", "", 30);
  state.activity.kind = "google-flow-session";
  try {
    renderGoogleFlowSessionStatus(
      await api("/api/google-flow/session/pending", { method: "DELETE" }),
    );
    completeActivity("Đã hủy pending session.", "Phiên active cũ không thay đổi");
    toast("Đã hủy phiên đăng nhập mới; phiên active cũ không thay đổi");
  } catch (error) {
    failActivity(error.message || "Không hủy được pending session.", "Google Flow Session");
    toast(error.message, true);
  }
}

function updateRenderControls() {
  const configured = projectRenderReady();
  const masterGateReady = projectMasterGateReady();
  const scenes = state.project?.scenes || [];
  const current = activeScene();
  const selected = scenes.filter((scene) => scene.selected);
  const sceneReady = configured
    && masterGateReady
    && current
    && current.image_plan?.status !== "Blocked";
  const allReady = configured && masterGateReady && scenes.length > 0
    && scenes.every((scene) => scene.image_plan?.status !== "Blocked");
  const selectedReady = configured && masterGateReady && selected.length > 0
    && selected.every((scene) => scene.image_plan?.status !== "Blocked");

  const sceneButton = $("#generateSceneBtn");
  if (sceneButton) {
    sceneButton.disabled = !sceneReady;
    sceneButton.title = sceneReady
      ? ""
      : !masterGateReady
        ? "Project Master Gate chưa PASS."
        : current?.image_plan?.status === "Blocked"
          ? "Scene đang Blocked bởi dependency/start-frame requirement."
          : "Provider của project chưa sẵn sàng hoặc cấu hình không tương thích.";
  }
  const allButton = $("#generateAllBtn");
  if (allButton) {
    allButton.disabled = !allReady;
    allButton.title = allReady
      ? ""
      : !masterGateReady
        ? "Project Master Gate chưa PASS."
        : configured
          ? "Có scene đang Blocked bởi dependency/start-frame requirement."
          : "Provider của project chưa sẵn sàng hoặc cấu hình không tương thích.";
  }
  const selectedButton = $("#generateSelectedBtn");
  if (selectedButton) {
    selectedButton.disabled = !selectedReady;
    selectedButton.title = selectedReady
      ? ""
      : !masterGateReady
        ? "Project Master Gate chưa PASS."
        : selected.some((scene) => scene.image_plan?.status === "Blocked")
          ? "Scene đã chọn có dependency/start-frame chưa sẵn sàng."
          : "Hãy chọn scene Ready và bảo đảm provider đang sẵn sàng.";
  }
  const generateImage = $("#generateImageBtn");
  if (generateImage) {
    const provider = state.project?.settings?.provider || "unconfigured";
    const detail = state.render.providerDetails[provider] || {};
    const scene = activeScene();
    const imageReady = (
      Boolean(scene)
      && masterGateReady
      && scene?.image_plan?.status !== "Blocked"
      && projectImageModelReady()
      && Array.isArray(detail.image_models)
      && detail.image_models.length > 0
    );
    generateImage.disabled = !imageReady;
    generateImage.title = imageReady
      ? "Tạo Start/Target keyframe sau khi Project Master Gate đã PASS."
      : !masterGateReady
        ? "Hãy hoàn tất và nghiệm thu toàn bộ Project Masters trước."
        : scene?.image_plan?.status === "Blocked"
          ? "Scene đang chờ dependency/start frame hợp lệ."
          : !projectImageModelReady()
            ? "Hãy chọn và Lưu Google Flow Image Model trước."
            : "Provider hiện tại chưa hỗ trợ tạo ảnh hoặc chưa sẵn sàng.";
    generateImage.textContent = imageReady
      ? "Tạo ảnh cảnh"
      : !masterGateReady
        ? "Tạo ảnh cảnh · Master Gate chưa PASS"
        : scene?.image_plan?.status === "Blocked"
          ? "Tạo ảnh cảnh · Scene đang Blocked"
          : !projectImageModelReady()
            ? "Tạo ảnh cảnh · Chưa lưu model ảnh"
            : "Tạo ảnh cảnh · Chưa có renderer ảnh";
  }
}

async function loadRenderStatus() {
  try {
    renderRenderStatus(await api("/api/render/status"));
  } catch (error) {
    renderRenderStatus({ configured: false, provider: "unconfigured", message: error.message });
  }
}

function requiredSceneMasterIds(scene) {
  const plan = scene?.image_plan || {};
  return [...new Set([
    ...(plan.character_reference_ids || []),
    ...(plan.location_reference_id ? [plan.location_reference_id] : []),
    ...(plan.prop_reference_ids || []),
  ])];
}

function missingSceneMasterReferences(scene) {
  const references = new Map(
    (state.project?.visual_bible?.references || []).map((reference) => [reference.id, reference]),
  );
  return requiredSceneMasterIds(scene)
    .map((id) => references.get(id) || {
      id,
      name: id,
      status: "missing",
      approved_reference: "",
    })
    .filter((reference) => (
      reference.status !== "approved"
      || !reference.approved_reference
    ));
}

async function ensureSceneMastersForImage(scene, button) {
  const missing = missingSceneMasterReferences(scene);
  if (!missing.length) return true;

  if (!state.xkiroConnected) {
    failActivity(
      "Thiếu xKiro Vision QC.",
      `Scene cần ${missing.length} Master Reference trước khi tạo ảnh.`,
    );
    const bibleTab = document.querySelector('#projectTabs [data-tab="bible"]');
    if (bibleTab) bibleTab.click();
    toast(
      `Scene thiếu ${missing.length} Master. Hãy cấu hình xKiro để Vision QC trước khi tạo ảnh.`,
      true,
    );
    return false;
  }
  if (!state.project.settings?.vision_model) {
    failActivity(
      "Chưa chọn Vision QC model.",
      `Scene cần ${missing.length} Master Reference trước khi tạo ảnh.`,
    );
    const bibleTab = document.querySelector('#projectTabs [data-tab="bible"]');
    if (bibleTab) bibleTab.click();
    toast("Hãy chọn và Lưu Vision QC model trong Bible trước khi tạo ảnh.", true);
    return false;
  }

  for (let index = 0; index < missing.length; index += 1) {
    const reference = missing[index];
    const name = reference?.name || reference?.id || `Master ${index + 1}`;
    const progress = 15 + ((index + 1) / Math.max(1, missing.length)) * 48;
    button.textContent = `Đang tạo Master ${index + 1}/${missing.length} · ${name}`;
    updateActivity(
      `Master ${index + 1}/${missing.length} · ${name} · Google Flow + Vision QC`,
      progress,
      reference?.id || "",
    );
    const updated = await generateMasterReferenceWithRepair(reference.id, {
      maxCorrectiveRetries: 2,
      onRetry: ({ retryNumber, maxCorrectiveRetries, issues }) => {
        button.textContent = `Corrective retry ${retryNumber}/${maxCorrectiveRetries} · ${name}`;
        updateActivity(
          `Master ${index + 1}/${missing.length} · Vision reject → corrective retry ${retryNumber}/${maxCorrectiveRetries}`,
          Math.min(68, progress + retryNumber * 2),
          issues || reference.id,
        );
      },
    });
    if (!updated || updated.status !== "approved" || !updated.approved_reference) {
      const issues = updated ? masterReferenceIssueSummary(updated) : "";
      throw new Error(
        `Master ${name} chưa được Vision Approved${issues ? ` · ${issues}` : ""}`,
      );
    }
  }
  return true;
}

async function generateSceneImages() {
  const scene = activeScene();
  if (!scene || !state.project) return;
  if (!projectMasterGateReady()) {
    return toast(
      "Project Master Gate chưa PASS. Hãy tạo và nghiệm thu toàn bộ Master trước.",
      true,
    );
  }
  if (scene.image_plan?.status === "Blocked") {
    return toast("Scene đang Blocked bởi dependency/start-frame requirement.", true);
  }
  beginActivity(
    `Tạo ảnh · ${scene.id}`,
    "Master Gate PASS · kiểm tra renderer...",
    scene.summary || scene.title || "",
    4,
  );
  state.activity.kind = "image";
  const provider = state.project?.settings?.provider || "unconfigured";
  if (!state.render.configuredProviders.includes(provider)) {
    failActivity("Google Flow renderer chưa sẵn sàng.", provider);
    $("#videoSetupModal").showModal();
    return toast("Hãy cấu hình renderer Google Flow trước khi tạo ảnh cảnh.", true);
  }
  if (!projectImageModelReady()) {
    failActivity("Chưa lưu Google Flow Image Model.", "Chọn model ảnh rồi bấm Lưu model ảnh.");
    syncSceneImageModelControl();
    return toast("Hãy chọn và Lưu model tạo ảnh Google Flow trước khi Generate.", true);
  }
  const button = $("#generateImageBtn");
  const previousText = button.textContent;
  button.disabled = true;
  button.textContent = "Đang xác nhận Master Gate...";
  try {
    updateActivity("Project Master Gate PASS.", 12, scene.id);
    const refreshedScene = activeScene();
    if (!refreshedScene) throw new Error("Không tìm thấy scene hiện tại.");
    if (refreshedScene.image_plan?.status === "Blocked") {
      throw new Error(
        "Scene đang Blocked bởi dependency/start-frame requirement.",
      );
    }

    button.textContent = "Đang tạo Start/Target trên Google Flow...";
    updateActivity(
      "Master Gate PASS · đang tạo Start/Target trên Google Flow...",
      72,
      refreshedScene.id,
    );
    const project = await api(
      `/api/projects/${state.project.id}/scenes/${refreshedScene.id}/images/generate`,
      { method: "POST" },
    );
    renderProject(project);
    const updated = project.scenes.find((item) => item.id === refreshedScene.id);
    const start = updated?.image_plan?.generated_start_frame;
    const target = updated?.image_plan?.generated_target_frame;
    updateActivity("Đã tải ảnh về TH Media · đang cập nhật preview...", 96, refreshedScene.id);
    completeActivity(
      "Đã tạo và hiển thị Start/Target frame.",
      `${start || "inherited start"} · ${target || "target missing"}`,
    );
    toast(
      start
        ? "Đã tạo Start frame và Target/Exit frame cho scene."
        : target
          ? "Đã giữ inherited Start frame và tạo Target/Exit frame."
          : "Đã hoàn tất tạo ảnh scene.",
    );
  } catch (error) {
    failActivity(error.message || "Tạo ảnh scene thất bại.", scene.id);
    toast(error.message, true);
  } finally {
    button.textContent = previousText;
    updateRenderControls();
    renderImagePlan(activeScene());
  }
}

async function saveMasterVisionModel() {
  if (!state.project) return;
  const select = $("#masterVisionModelInput");
  const model = select?.value || "";
  if (!model) return toast("Hãy chọn Vision QC model.", true);
  const button = $("#saveMasterVisionModelBtn");
  const previous = button?.textContent || "Lưu Vision model";
  if (button) {
    button.disabled = true;
    button.textContent = "Đang lưu...";
  }
  try {
    const project = await api(
      `/api/projects/${state.project.id}/vision-settings`,
      {
        method: "PATCH",
        body: JSON.stringify({ vision_model: model }),
      },
    );
    renderProject(project);
    toast("Đã lưu Vision QC model cho project.");
  } catch (error) {
    toast(error.message, true);
  } finally {
    if (button && button.isConnected) button.textContent = previous;
  }
}

async function generateMasterReference(referenceId, { silent = false } = {}) {
  if (!referenceId || !state.project) return null;
  if (!silent && !state.activity.active) {
    beginActivity(
      "Tạo Master Reference",
      "Chuẩn bị Google Flow + Vision QC...",
      referenceId,
      8,
    );
    state.activity.kind = "master";
  }
  if (!state.xkiroConnected) {
    if (!silent) {
      failActivity("Cần cấu hình xKiro trước khi tạo Master.", referenceId);
      toast("Cần cấu hình xKiro trước khi tạo Master.", true);
    }
    return null;
  }
  if (!state.project.settings?.vision_model) {
    if (!silent) {
      failActivity("Cần chọn Vision QC model trước khi tạo Master.", referenceId);
      toast("Cần chọn Vision QC model trước khi tạo Master.", true);
    }
    return null;
  }
  if (!state.project.settings?.image_model) {
    if (!silent) {
      failActivity("Cần chọn và Lưu Google Flow Image Model trước khi tạo Master.", referenceId);
      toast("Cần chọn và Lưu Google Flow Image Model trước khi tạo Master.", true);
    }
    return null;
  }
  if (!state.render.configuredProviders.includes("google-flow-browser")) {
    if (!silent) {
      failActivity("Google Flow Browser chưa sẵn sàng.", referenceId);
      toast("Google Flow Browser chưa sẵn sàng.", true);
    }
    return null;
  }

  const button = $$(".master-ref-generate").find(
    (item) => item.dataset.masterRef === referenceId,
  );
  const previousText = button?.textContent || "Tạo Master";
  if (button) {
    button.disabled = true;
    button.textContent = "Đang tạo + Vision QC...";
  }

  try {
    if (!silent) {
      updateActivity("Đang tạo/reuse ảnh Master trên Google Flow...", 38, referenceId);
    }
    const project = await api(
      `/api/projects/${state.project.id}/visual-references/${encodeURIComponent(referenceId)}/generate`,
      { method: "POST" },
    );
    renderProject(project);
    const reference = project.visual_bible?.references?.find(
      (item) => item.id === referenceId,
    );
    if (!reference) return null;
    if (!silent) {
      if (masterReferenceGateReady(reference)) {
        completeActivity(
          `Master ${reference.name} đã PASS Master Gate.`,
          `Master QC ${reference.vision_score}/100 · ${reference.vision_model || "unknown model"}`,
        );
        toast(`Master ${reference.name} PASS Gate · QC ${reference.vision_score}/100`);
      } else {
        const issues = masterReferenceIssueSummary(reference);
        if (reference.status === "candidate" && masterReferenceVisionUnavailable(reference)) {
          pauseActivity(
            `Vision QC tạm thời không khả dụng · đã giữ candidate ${reference.name}`,
            issues || reference.id,
          );
          toast("Đã giữ Master candidate; thử QC lại khi xKiro Vision sẵn sàng.");
        } else {
          failActivity(
            `Master ${reference.name} chưa đạt Master Gate · ${reference.status}`,
            issues || `Master QC ${reference.vision_score || 0}/100`,
          );
          toast(
            `Master ${reference.name} chưa đạt Gate · ${reference.status}${issues ? ` · ${issues}` : ""}`,
            true,
          );
        }
      }
    }
    return reference;
  } catch (error) {
    if (!silent) {
      failActivity(error.message || "Tạo Master thất bại.", referenceId);
      toast(error.message, true);
    }
    throw error;
  } finally {
    if (button && button.isConnected) {
      button.disabled = false;
      button.textContent = previousText;
    }
  }
}

async function generateMasterReferenceWithRepair(
  referenceId,
  { maxCorrectiveRetries = 2, onRetry = null } = {},
) {
  let reference = null;
  for (let attempt = 0; attempt <= maxCorrectiveRetries; attempt += 1) {
    reference = await generateMasterReference(referenceId, { silent: true });
    if (!reference || masterReferenceGateReady(reference) || reference.status === "candidate") {
      return reference;
    }
    if (reference.status !== "rejected" || attempt >= maxCorrectiveRetries) {
      return reference;
    }

    const retryNumber = attempt + 1;
    const issues = masterReferenceIssueSummary(reference);
    if (typeof onRetry === "function") {
      onRetry({ reference, retryNumber, maxCorrectiveRetries, issues });
    } else {
      updateActivity(
        `Vision reject · corrective retry ${retryNumber}/${maxCorrectiveRetries}`,
        null,
        issues || referenceId,
      );
    }
  }
  return reference;
}

async function generateMasterReferenceInteractive(referenceId) {
  if (!referenceId || !state.project) return;
  if (!masterReferenceGenerationReady()) {
    return generateMasterReference(referenceId);
  }

  beginActivity(
    "Tạo Master Reference",
    "Đang tạo/reuse ảnh Master trên Google Flow...",
    referenceId,
    10,
  );
  state.activity.kind = "master";

  try {
    const reference = await generateMasterReferenceWithRepair(referenceId, {
      maxCorrectiveRetries: 2,
      onRetry: ({ retryNumber, maxCorrectiveRetries, issues }) => {
        updateActivity(
          `Vision reject → corrective retry ${retryNumber}/${maxCorrectiveRetries}`,
          45 + retryNumber * 15,
          issues || referenceId,
        );
      },
    });

    if (reference && masterReferenceGateReady(reference)) {
      completeActivity(
        `Master ${reference.name} đã PASS Master Gate.`,
        `Master QC ${reference.vision_score}/100 · ${reference.vision_model || "unknown model"}`,
      );
      toast(`Master ${reference.name} PASS Gate · QC ${reference.vision_score}/100`);
      return;
    }

    const issues = reference ? masterReferenceIssueSummary(reference) : "";
    if (reference?.status === "candidate" && masterReferenceVisionUnavailable(reference)) {
      pauseActivity(
        `Vision QC tạm thời không khả dụng · đã giữ candidate ${reference.name}`,
        issues || referenceId,
      );
      toast("Đã giữ Master candidate; thử QC lại khi xKiro Vision sẵn sàng.");
      return;
    }
    failActivity(
      `Master ${reference?.name || referenceId} chưa đạt Master Gate · ${reference?.status || "failed"}`,
      issues || referenceId,
    );
    toast(
      `Master ${reference?.name || referenceId} chưa đạt Master Gate${issues ? ` · ${issues}` : ""}`,
      true,
    );
  } catch (error) {
    failActivity(error.message || "Tạo Master thất bại.", referenceId);
    toast(error.message, true);
  }
}

async function generateMissingMasterReferences() {
  if (!state.project || state.masterBatchActive) return;
  const pending = (state.project.visual_bible?.references || []).filter(
    (reference) => !masterReferenceGateReady(reference),
  );
  if (!pending.length) return toast("Project Master Gate PASS.");
  if (!masterReferenceGenerationReady()) {
    if (!state.xkiroConnected) return toast("Cần cấu hình xKiro trước.", true);
    if (!state.project.settings?.vision_model) {
      return toast("Cần chọn Vision QC model trước.", true);
    }
    if (!state.project.settings?.image_model) {
      return toast("Cần chọn và Lưu Google Flow Image Model trước.", true);
    }
    return toast("Google Flow Browser chưa sẵn sàng.", true);
  }

  state.masterBatchActive = true;
  beginActivity(
    "Tạo Master References",
    `Chuẩn bị batch ${pending.length} Master...`,
    "Google Flow → Downstream Master QC → Project Master Gate",
    3,
  );
  state.activity.kind = "master-batch";
  renderBible();
  try {
    for (let index = 0; index < pending.length; index += 1) {
      const referenceId = pending[index].id;
      const status = $("#masterBatchStatus");
      if (status) {
        status.textContent = `Đang tạo Master ${index + 1}/${pending.length}: ${pending[index].name}`;
      }
      const batchProgress = 5 + ((index + 1) / Math.max(1, pending.length)) * 88;
      updateActivity(
        `Master ${index + 1}/${pending.length} · ${pending[index].name}`,
        batchProgress,
        `${pending[index].id} · Google Flow + Vision QC`,
      );
      let reference;
      try {
        reference = await generateMasterReferenceWithRepair(referenceId, {
          maxCorrectiveRetries: 2,
          onRetry: ({ retryNumber, maxCorrectiveRetries, issues }) => {
            const retryProgress = Math.min(97, batchProgress + retryNumber * 1.5);
            updateActivity(
              `Master ${index + 1}/${pending.length} · Vision reject → corrective retry ${retryNumber}/${maxCorrectiveRetries}`,
              retryProgress,
              issues || pending[index].id,
            );
            if (status) {
              status.textContent = `Corrective retry ${retryNumber}/${maxCorrectiveRetries}: ${pending[index].name}`;
            }
          },
        });
      } catch (error) {
        failActivity(
          `Batch Master dừng tại ${pending[index].name}`,
          error.message || pending[index].id,
        );
        toast(`Batch Master dừng: ${error.message}`, true);
        return;
      }
      if (!reference || reference.status !== "approved") {
        const issues = reference ? masterReferenceIssueSummary(reference) : "";
        if (reference?.status === "candidate" && masterReferenceVisionUnavailable(reference)) {
          pauseActivity(
            `Batch tạm dừng tại ${pending[index].name} · Vision QC chưa sẵn sàng`,
            issues || pending[index].id,
          );
          if (status) {
            status.textContent = "Candidate đã được giữ · retry QC khi xKiro Vision sẵn sàng.";
          }
          toast("Đã giữ candidate; batch tạm dừng để chờ xKiro Vision.");
          return;
        }
        failActivity(
          `Batch Master dừng tại ${pending[index].name}: ${reference?.status || "failed"}`,
          issues || pending[index].id,
        );
        toast(
          `Batch Master dừng tại ${pending[index].name}: ${reference?.status || "failed"}${issues ? ` · ${issues}` : ""}`,
          true,
        );
        return;
      }
    }
    const gate = await api(`/api/projects/${state.project.id}/master-gate`);
    if (!gate.ready) {
      const detail = (gate.blockers || []).slice(0, 4).join(" · ");
      failActivity("Project Master Gate FAIL.", detail || "Master Gate chưa đạt.");
      toast(`Project Master Gate FAIL${detail ? ` · ${detail}` : ""}`, true);
      return;
    }
    completeActivity(
      "Project Master Gate PASS · có thể chuyển sang tạo ảnh scene.",
      `${gate.approved_count}/${gate.master_count} Master đạt chuẩn`,
    );
    toast("Project Master Gate PASS · Scene Image generation đã được mở.");
  } finally {
    state.masterBatchActive = false;
    renderBible();
    updateRenderControls();
  }
}

async function uploadMasterReference(referenceId) {
  if (!referenceId || !state.project) return;
  const input = $$(".master-ref-file").find(
    (item) => item.dataset.masterRef === referenceId,
  );
  const file = input?.files?.[0];
  if (!file) return toast("Hãy chọn ảnh Master JPEG, PNG hoặc WebP", true);

  beginActivity(
    "Upload Master Reference",
    "Đang tải ảnh Master vào TH Media...",
    `${referenceId} · ${file.name}`,
    12,
  );
  state.activity.kind = "master-upload";
  try {
    const response = await fetch(
      `/api/projects/${state.project.id}/visual-references/${encodeURIComponent(referenceId)}/image`,
      {
        method: "POST",
        headers: {
          "Content-Type": file.type,
          ...(state.sessionToken ? { "X-Flow-Studio-Session": state.sessionToken } : {}),
        },
        body: file,
      },
    );
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload.detail || `Lỗi ${response.status}`);
    }

    const project = await response.json();
    const reference = project.visual_bible?.references?.find(
      (item) => item.id === referenceId,
    );
    renderProject(project);
    if (reference?.status === "approved") {
      completeActivity(
        "Master upload đã được Vision Approved.",
        `${reference.name || referenceId} · Vision ${reference.vision_score || 0}/100`,
      );
      toast("Master đã được xKiro Vision duyệt và sẽ được REUSE cho mọi scene");
    } else if (reference?.status === "candidate") {
      if (masterReferenceVisionUnavailable(reference)) {
        pauseActivity(
          "Master đã lưu dạng candidate · Vision QC tạm thời chưa sẵn sàng.",
          masterReferenceIssueSummary(reference) || referenceId,
        );
        toast("Đã giữ Master candidate; thử QC lại khi xKiro Vision sẵn sàng.");
      } else {
        pauseActivity(
          "Master đã lưu dạng candidate · đang chờ Vision QC.",
          masterReferenceIssueSummary(reference) || referenceId,
        );
        toast("Đã lưu Master candidate; cần Vision QC trước khi scene được mở khóa.");
      }
    } else if (reference?.status === "rejected") {
      failActivity(
        "Master không đạt Vision QC.",
        masterReferenceIssueSummary(reference) || referenceId,
      );
      toast("Master không đạt Vision QC; hãy thay ảnh khác", true);
    } else {
      completeActivity("Đã cập nhật Master Reference.", referenceId);
      toast("Đã cập nhật Master Reference");
    }
  } catch (error) {
    failActivity(error.message || "Upload Master thất bại.", referenceId);
    toast(error.message, true);
  }
}

async function uploadReference() {
  const scene = activeScene();
  const file = $("#referenceFile").files[0];
  if (!scene || !file) return toast("Hãy chọn ảnh JPEG, PNG hoặc WebP", true);
  beginActivity(
    "Upload scene reference",
    "Đang tải start-frame override...",
    `${scene.id} · ${file.name}`,
    15,
  );
  state.activity.kind = "scene-reference-upload";
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
    completeActivity(
      "Đã gắn start-frame override cho scene.",
      `${scene.id} · Master References không thay đổi`,
    );
    toast("Đã gắn start-frame override cho riêng scene; Master References không thay đổi");
  } catch (error) {
    failActivity(error.message || "Upload scene reference thất bại.", scene.id);
    toast(error.message, true);
  }
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
    if (!$("#analysisModelInput").value) {
      return toast("Hãy chọn model xKiro để phân tích nội dung", true);
    }
    if (!$("#visionModelInput").value) {
      return toast("Hãy chọn model xKiro Vision để nghiệm thu hình ảnh/video", true);
    }
  }
  if (autoPipeline && !state.render.configured) {
    return toast("Chưa cấu hình render engine.", true);
  }
  const button = autoPipeline ? $("#autoPipelineSubmit") : $("#analyzeSubmit");
  const original = button.textContent;
  $("#analyzeSubmit").disabled = true; $("#autoPipelineSubmit").disabled = true;
  button.textContent = "Đang phân tích...";
  beginActivity(
    autoPipeline ? "Phân tích + Auto Pipeline" : "Phân tích nội dung",
    "Đang tạo analysis job...",
    $("#projectNameInput").value || "Project mới",
    5,
  );
  state.activity.kind = "analysis";
  try {
    const job = await api(
      `/api/analysis/jobs?auto_pipeline=${autoPipeline}`,
      { method: "POST", body: JSON.stringify(projectPayload()) },
    );
    state.analysisJobId = job.id;
    updateActivity("Analysis job đã khởi động · đang đọc và cấu trúc nội dung...", 12, job.id);
    renderAnalysisJob(job);
    const finished = await waitForAnalysisJob(job.id);
    if (!finished) return;
    if (finished.status !== "completed" || !finished.project) {
      throw new Error(finished.error || "Phân tích không hoàn tất");
    }
    const project = await api(`/api/projects/${finished.project.id}`);
    state.queueActive = false;
    $("#newProjectModal").close();
    renderProject(project, false);
    setWorkspaceView("storyboard");
    completeActivity(
      autoPipeline
        ? "Phân tích xong · Auto Pipeline đang chờ Project Master Gate."
        : "Phân tích nội dung hoàn tất.",
      `${project.scenes.length} scene · continuity ${project.continuity_score}%`,
    );
    toast(
      autoPipeline
        ? "Phân tích xong · hãy tạo và nghiệm thu toàn bộ Master trước production"
        : `Phân tích xong · đã tạo ${project.scenes.length} scene`,
    );
  } catch (error) {
    failActivity(error.message || "Phân tích thất bại.", "Analysis pipeline");
    toast(error.message, true);
  }
  finally {
    state.analysisJobId = null;
    $("#cancelAnalysisBtn").classList.add("hidden");
    $("#analyzeSubmit").disabled = false; $("#autoPipelineSubmit").disabled = false;
    button.textContent = original;
  }
}

async function generate(sceneIds, forceRerender = false) {
  if (!state.project) return;
  if (!projectRenderReady()) {
    $("#videoSetupModal").showModal();
    return toast("Provider của project chưa sẵn sàng hoặc cấu hình chưa tương thích.", true);
  }
  const targets = sceneIds.length
    ? state.project.scenes.filter((scene) => sceneIds.includes(scene.id))
    : state.project.scenes;
  const blocked = targets.filter((scene) => scene.image_plan?.status === "Blocked");
  if (blocked.length) {
    return toast(
      `${blocked.length} scene đang Blocked vì thiếu Master References được duyệt.`,
      true,
    );
  }
  const targetIds = targets.map((scene) => scene.id);
  beginActivity(
    `Render video · ${targetIds.length} scene`,
    "Đang đưa scene vào render queue...",
    targetIds.join(", "),
    5,
  );
  state.activity.kind = "render";
  state.activity.targetSceneIds = targetIds;
  try {
    const project = await api(`/api/projects/${state.project.id}/generate`, { method: "POST", body: JSON.stringify({ scene_ids: sceneIds, force_rerender: forceRerender }) });
    state.queueActive = true;
    renderProject(project);
    updateActivity(
      "Đã vào hàng đợi · chờ scene đầu tiên bắt đầu render...",
      8,
      `${targetIds.length} scene`,
    );
    toast(`Đã đưa ${sceneIds.length || project.scenes.length} scene vào hàng đợi`);
  } catch (error) {
    failActivity(error.message || "Không enqueue được render.", targetIds.join(", "));
    toast(error.message, true);
  }
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
  const dismiss = $("#operationDismissBtn");
  if (dismiss) dismiss.onclick = dismissActivity;
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
  const openVideoSetup = async () => {
    if (!state.project) return toast("Hãy phân tích nội dung trước", true);
    await loadRenderStatus();
    syncRenderSetupForm();
    $("#videoSetupModal").showModal();
  };
  $("#googleFlowStatus").onclick = async () => {
    await loadGoogleFlowSessionStatus();
    $("#googleFlowSessionModal").showModal();
    if (
      state.googleFlow.chrome_available
      && !state.googleFlow.configured
      && !state.googleFlow.pending
    ) {
      await createGoogleFlowSession();
    }
  };
  $("#videoSetupBtn").onclick = openVideoSetup;
  $("#providerInput").onchange = () => syncRenderSetupForm(true);
  $("#closeVideoSetupBtn").onclick = () => $("#videoSetupModal").close();
  $("#cancelVideoSetupBtn").onclick = () => $("#videoSetupModal").close();
  $("#closeGoogleFlowSessionBtn").onclick = () => $("#googleFlowSessionModal").close();
  $("#doneGoogleFlowSessionBtn").onclick = () => $("#googleFlowSessionModal").close();
  $("#openGoogleFlowSessionBtn").onclick = () => openGoogleFlowSession(false);
  $("#newGoogleFlowSessionBtn").onclick = createGoogleFlowSession;
  $("#reopenGoogleFlowPendingBtn").onclick = () => openGoogleFlowSession(true);
  $("#activateGoogleFlowPendingBtn").onclick = activateGoogleFlowSession;
  $("#cancelGoogleFlowPendingBtn").onclick = cancelGoogleFlowSession;
  $("#videoSetupForm").addEventListener("submit", saveVideoSetup);
  $("#uploadReferenceBtn").onclick = uploadReference;
  $("#generateImageBtn").onclick = generateSceneImages;
  $("#sceneForm").addEventListener("submit", saveScene);
  $("#sceneLockBtn").onclick = toggleSceneLock;
  $("#savePromptBtn").onclick = async () => {
    const scene = activeScene(); if (!scene) return;
    try { renderProject(await api(`/api/projects/${state.project.id}/scenes/${scene.id}`, { method: "PATCH", body: JSON.stringify({ render_prompt: $("#editRenderPrompt").value }) })); toast("Đã lưu render prompt"); }
    catch (error) { toast(error.message, true); }
  };
  $("#copyPromptBtn").onclick = async () => { await navigator.clipboard.writeText($("#editRenderPrompt").value); toast("Đã sao chép prompt"); };
  $("#copyStartImagePromptBtn").onclick = async () => {
    const value = $("#startImagePrompt").value;
    if (!value) return toast("Scene chưa có prompt ảnh khởi đầu", true);
    await navigator.clipboard.writeText(value);
    toast("Đã sao chép prompt ảnh khởi đầu");
  };
  $("#copyTargetImagePromptBtn").onclick = async () => {
    const value = $("#targetImagePrompt").value;
    if (!value) return toast("Scene chưa có prompt ảnh đích", true);
    await navigator.clipboard.writeText(value);
    toast("Đã sao chép prompt ảnh đích");
  };
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
    beginActivity(
      "Auto Continuity",
      "Đang kiểm tra start/end state, anchor và downstream continuity...",
      `${state.project?.scenes?.length || 0} scene`,
      8,
    );
    state.activity.kind = "continuity";
    try {
      const project = await api(
        `/api/projects/${state.project.id}/continuity?auto_fix=true`,
        { method: "POST" },
      );
      renderProject(project);
      completeActivity(
        "Auto Continuity hoàn tất.",
        `Continuity ${project.continuity_score}% · ${project.continuity_warnings?.length || 0} cảnh báo`,
      );
      toast("Đã đồng bộ start/end frame và kiểm tra continuity");
    } catch (error) {
      failActivity(error.message || "Auto Continuity thất bại.", "Continuity pipeline");
      toast(error.message, true);
    }
  };
  $("#continuityBtn").onclick = checkContinuity; $("#checkSceneBtn").onclick = checkContinuity;
  $("#analyzeAgainBtn").onclick = () => {
    $("#projectNameInput").value = state.project.name;
    $("#storyInput").value = state.project.original_text;
    $("#aspectInput").value = state.project.settings.aspect_ratio;
    $("#durationInput").value = state.project.settings.scene_duration;
    $("#styleInput").value = state.project.settings.style;
    $("#providerInput").value = state.project.settings.provider;
    $("#videoModelInput").value = state.project.settings.video_model || "Chưa cấu hình";
    $("#analysisProviderInput").value = state.project.settings.analysis_provider || "offline";
    toggleXKiroConfig();
    if (state.project.settings.analysis_model && state.xkiroModels.length) {
      $("#analysisModelInput").value = state.project.settings.analysis_model;
    }
    if (state.project.settings.vision_model && state.xkiroModels.length) {
      $("#visionModelInput").value = state.project.settings.vision_model;
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
  await loadSession();

  const startup = new URLSearchParams(window.location.search);
  const startupProjectId = startup.get("project") || "";
  const requestedView = startup.get("view") || "storyboard";
  let openedStartupProject = false;

  if (startupProjectId) {
    try {
      const project = await api(`/api/projects/${encodeURIComponent(startupProjectId)}`);
      renderProject(project, false);
      setWorkspaceView(
        ["project", "storyboard", "editor"].includes(requestedView)
          ? requestedView
          : "storyboard",
      );
      if ($("#newProjectModal").open) $("#newProjectModal").close();
      openedStartupProject = true;
    } catch (error) {
      toast(`Không thể mở project nghiệm thu: ${error.message}`, true);
    }
  }

  if (!openedStartupProject) openAnalysisStep();
  await Promise.all([
    loadXKiroStatus(),
    loadRenderStatus(),
    loadGoogleFlowSessionStatus(),
  ]);
}

boot();

function projectImageModelReady() {
  const provider = state.project?.settings?.provider || "unconfigured";
  if (!state.render.configuredProviders.includes(provider)) return false;
  const detail = state.render.providerDetails[provider] || {};
  const models = Array.isArray(detail.image_models) ? detail.image_models : [];
  const selected = state.project?.settings?.image_model || "";
  const imageAspects = detail.capabilities?.image_aspect_ratios || [];
  const aspect = state.project?.settings?.aspect_ratio || "";
  if (imageAspects.length && !imageAspects.includes(aspect)) return false;
  return Boolean(selected && models.includes(selected));
}

function syncSceneImageModelControl() {
  const select = $("#sceneImageModelInput");
  const save = $("#saveSceneImageModelBtn");
  const meta = $("#imageModelMeta");
  if (!select || !save || !meta) return;

  const provider = state.project?.settings?.provider || "unconfigured";
  const detail = state.render.providerDetails[provider] || {};
  const models = Array.isArray(detail.image_models) ? detail.image_models : [];
  const saved = state.project?.settings?.image_model || "";
  const providerReady = state.render.configuredProviders.includes(provider);
  const preferred = models.includes(saved)
    ? saved
    : models.includes("Nano Banana 2")
      ? "Nano Banana 2"
      : (models[0] || "");

  select.innerHTML = models.length
    ? '<option value="">Chọn model tạo ảnh...</option>'
      + models.map((model) => `<option value="${escapeHtml(model)}">${escapeHtml(model)}</option>`).join("")
    : '<option value="">Provider chưa có image model</option>';
  select.value = preferred;
  select.disabled = !providerReady || !models.length;

  const refreshSaveState = () => {
    save.disabled = (
      !providerReady
      || !select.value
      || select.value === (state.project?.settings?.image_model || "")
    );
  };
  refreshSaveState();
  select.onchange = refreshSaveState;
  save.onclick = saveSceneImageModel;

  if (!providerReady) {
    meta.textContent = "Google Flow Browser chưa sẵn sàng.";
  } else if (!models.length) {
    meta.textContent = "Provider hiện tại không công bố model tạo ảnh.";
  } else if (!saved) {
    meta.textContent = `${models.length} model khả dụng · hãy chọn và Lưu model ảnh trước khi Generate.`;
  } else if (!models.includes(saved)) {
    meta.textContent = `Model đã lưu "${saved}" không còn khả dụng · hãy chọn lại.`;
  } else {
    meta.textContent = `Đang dùng ${saved} · áp dụng cho Master, Start Frame và Target Frame.`;
  }
}

async function saveSceneImageModel() {
  if (!state.project) return;
  const select = $("#sceneImageModelInput");
  const imageModel = select?.value || "";
  if (!imageModel) return toast("Hãy chọn model tạo ảnh Google Flow.", true);
  const button = $("#saveSceneImageModelBtn");
  const previous = button?.textContent || "Lưu model ảnh";
  if (button) {
    button.disabled = true;
    button.textContent = "Đang lưu...";
  }
  try {
    const project = await api(
      `/api/projects/${state.project.id}/image-settings`,
      {
        method: "PATCH",
        body: JSON.stringify({ image_model: imageModel }),
      },
    );
    renderProject(project);
    toast(`Đã lưu model tạo ảnh: ${imageModel}`);
  } catch (error) {
    toast(error.message, true);
  } finally {
    if (button && button.isConnected) button.textContent = previous;
    syncSceneImageModelControl();
    updateRenderControls();
  }
}


function requestActivityLabel(path, method = "POST") {
  const normalized = String(path || "");
  if (normalized.includes("/visual-references/") && normalized.endsWith("/generate")) return "Tạo Master Reference";
  if (normalized.includes("/images/generate")) return "Tạo ảnh scene";
  if (normalized.includes("/continuity")) return "Auto Continuity";
  if (normalized.includes("/generate")) return "Render scene/video";
  if (normalized.includes("/merge")) return "Ghép video";
  if (normalized.includes("/google-flow")) return "Google Flow Session";
  if (normalized.includes("/xkiro")) return "xKiro";
  if (normalized.includes("/video-settings")) return "Lưu cấu hình video";
  if (normalized.includes("/image-settings")) return "Lưu model ảnh";
  if (normalized.includes("/vision-settings")) return "Lưu Vision model";
  if (normalized.includes("/reference")) return "Cập nhật reference";
  if (normalized.includes("/lock")) return "Cập nhật khóa AI";
  return method + " · Đang xử lý";
}

function renderActivityBar() {
  const bar = $("#operationStatusBar");
  if (!bar) return;
  const activity = state.activity;
  bar.classList.remove("idle", "running", "done", "paused", "error", "hidden");
  bar.classList.add(activity.status || "idle");
  $("#operationState").textContent = activity.status === "running"
    ? "ĐANG CHẠY"
    : activity.status === "done"
      ? "HOÀN TẤT"
      : activity.status === "paused"
        ? "TẠM DỪNG"
        : activity.status === "error"
          ? "LỖI"
          : "SẴN SÀNG";
  $("#operationTitle").textContent = activity.title || "Không có tác vụ đang chạy";
  $("#operationStage").textContent = activity.stage || "Nhấn một tính năng để bắt đầu.";
  $("#operationDetail").textContent = activity.detail || "";
  const progress = Math.max(0, Math.min(100, Number(activity.progress) || 0));
  $("#operationPercent").textContent = Math.round(progress) + "%";
  $("#operationProgress").style.width = progress + "%";
}

function beginActivity(title, stage = "Đang khởi tạo...", detail = "", progress = 3) {
  state.activity = {
    active: true,
    status: "running",
    title,
    stage,
    detail,
    progress,
    token: (state.activity?.token || 0) + 1,
  };
  renderActivityBar();
  return state.activity.token;
}

function updateActivity(stage, progress = null, detail = "") {
  if (!state.activity.active) return;
  state.activity.stage = stage || state.activity.stage;
  if (progress !== null && progress !== undefined) state.activity.progress = progress;
  if (detail !== undefined) state.activity.detail = detail;
  renderActivityBar();
}

function completeActivity(stage = "Hoàn tất.", detail = "") {
  state.activity.active = false;
  state.activity.status = "done";
  state.activity.stage = stage;
  state.activity.detail = detail;
  state.activity.progress = 100;
  renderActivityBar();
}

function pauseActivity(message, detail = "") {
  state.activity.active = false;
  state.activity.status = "paused";
  state.activity.stage = message || "Tác vụ tạm dừng.";
  state.activity.detail = detail;
  state.activity.progress = Math.max(1, Math.min(99, Number(state.activity.progress) || 1));
  renderActivityBar();
}

function failActivity(message, detail = "") {
  state.activity.active = false;
  state.activity.status = "error";
  state.activity.stage = message || "Tác vụ thất bại.";
  state.activity.detail = detail;
  state.activity.progress = Math.max(1, Math.min(99, Number(state.activity.progress) || 1));
  renderActivityBar();
}

function dismissActivity() {
  state.activity.active = false;
  state.activity.status = "idle";
  state.activity.title = "";
  state.activity.stage = "";
  state.activity.detail = "";
  state.activity.progress = 0;
  renderActivityBar();
}


function syncActivityFromProject() {
  if (!state.project || !state.activity.active) return;

  if (state.activity.kind === "render") {
    const wanted = new Set(state.activity.targetSceneIds || []);
    const targets = wanted.size
      ? state.project.scenes.filter((scene) => wanted.has(scene.id))
      : state.project.scenes;
    if (!targets.length) return;

    const progress = targets.reduce((sum, scene) => sum + Number(scene.progress || 0), 0) / targets.length;
    const current = targets.find((scene) => !["Accepted", "Failed", "FailedQC"].includes(scene.status))
      || targets.find((scene) => scene.status !== "Accepted")
      || targets[targets.length - 1];
    const accepted = targets.filter((scene) => scene.status === "Accepted").length;
    const failed = targets.filter((scene) => ["Failed", "FailedQC"].includes(scene.status));

    if (accepted === targets.length) {
      completeActivity(
        "Render hoàn tất toàn bộ scene.",
        accepted + "/" + targets.length + " scene Accepted",
      );
      return;
    }

    const busy = targets.some((scene) =>
      ["Preparing", "Generating", "QC", "Waiting", "Paused"].includes(scene.status)
    );
    if (!busy && failed.length) {
      failActivity(
        "Render dừng với " + failed.length + " scene không đạt.",
        failed.map((scene) => scene.id + ":" + scene.status).join(" · "),
      );
      return;
    }

    updateActivity(
      (current?.id || "Render") + " · " + (current?.status || "Waiting"),
      Math.max(5, Math.min(98, progress)),
      accepted + "/" + targets.length + " Accepted",
    );
  }

  if (state.activity.kind === "merge") {
    const finalVideo = state.project.final_video || {};
    if (finalVideo.status === "Completed") {
      completeActivity(
        "Ghép video hoàn tất.",
        (finalVideo.scene_count || state.project.scenes.length) + " scene · " + (finalVideo.result_file || ""),
      );
      return;
    }
    if (finalVideo.status === "Failed") {
      failActivity("Ghép video thất bại.", finalVideo.error || "FFmpeg merge failed");
      return;
    }
    if (finalVideo.status === "Merging") {
      updateActivity(
        "Đang ghép video bằng FFmpeg...",
        Math.max(10, Number(finalVideo.progress || 10)),
        (finalVideo.scene_count || state.project.scenes.length) + " scene",
      );
    }
  }
}
