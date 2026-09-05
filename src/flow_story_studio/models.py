"""Canonical project data models."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .migrations import CURRENT_PROJECT_SCHEMA_VERSION


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class VideoSettings(StrictModel):
    aspect_ratio: Literal["16:9", "9:16", "1:1"] = "16:9"
    resolution: Literal["720p", "1080p", "highest"] = "1080p"
    style: str = "Cinematic"
    custom_style: str = ""
    scene_duration: int = Field(default=8, ge=4, le=30)
    character_lock: bool = True
    location_lock: bool = True
    auto_continuity: bool = True
    quality_threshold: int = Field(default=85, ge=0, le=100)
    provider: Literal["mock", "google-flow"] = "mock"
    video_model: str = "veo-3.1-lite-lower-priority"
    analysis_provider: Literal["offline", "xkiro"] = "offline"
    analysis_model: str = ""


class Character(StrictModel):
    id: str
    name: str
    gender: str = "Không xác định"
    estimated_age: str = "Không xác định"
    nationality_appearance: str = "Theo nội dung gốc"
    relative_height: str = "Trung bình"
    build: str = "Cân đối"
    face: str = "Giữ nhận dạng khuôn mặt nhất quán"
    hairstyle: str = "Theo mô tả gốc"
    hair_color: str = "Tự nhiên"
    eye_color: str = "Tự nhiên"
    skin_tone: str = "Theo nhân vật"
    clothing: str = "Trang phục phù hợp bối cảnh, giữ nguyên cho đến khi có thay đổi"
    accessories: str = "Không có nếu không được nêu"
    shoes: str = "Phù hợp trang phục"
    identifying_features: str = "Nhận dạng ổn định giữa mọi cảnh"
    personality: str = "Suy ra từ hành động và lời thoại"
    gestures: str = "Tự nhiên"
    movement: str = "Tự nhiên, có trọng lượng"
    reference_images: list[str] = Field(default_factory=list)


class Location(StrictModel):
    id: str
    name: str
    place_type: str = "Không gian trong câu chuyện"
    architecture: str = "Nhất quán giữa các cảnh"
    space: str = "Bố cục cố định"
    interior: str = "Theo nội dung gốc"
    objects: list[str] = Field(default_factory=list)
    colors: str = "Bảng màu điện ảnh nhất quán"
    lighting: str = "Ánh sáng có động cơ, tự nhiên"
    time_of_day: str = "Theo timeline"
    weather: str = "Theo nội dung gốc"
    spatial_anchors: str = "Giữ nguyên vị trí tương đối của vật thể quan trọng"
    reference_images: list[str] = Field(default_factory=list)


class Prop(StrictModel):
    id: str
    name: str
    description: str
    owner: str = "Chưa xác định"
    initial_location: str = "Theo cảnh đầu xuất hiện"
    state: str = "Nguyên vẹn"


class StoryBible(StrictModel):
    main_theme: str
    genre: str
    purpose: str
    audience: str
    mood: str
    synopsis: str


class Dialogue(StrictModel):
    character_id: str
    text: str
    emotion: str = "Tự nhiên, đúng ngữ cảnh"
    delivery: Literal["onscreen", "offscreen", "phone", "recorded"] = "onscreen"


class ContinuityState(StrictModel):
    character_positions: dict[str, str] = Field(default_factory=dict)
    character_wardrobe: dict[str, str] = Field(default_factory=dict)
    prop_positions: dict[str, str] = Field(default_factory=dict)
    time: str = "Liên tục từ cảnh trước"
    weather: str = "Không đổi nếu chưa được nêu"
    camera: str = "Trục camera nhất quán"
    notes: str = ""


class QualityReport(StrictModel):
    character: int = 100
    clothing: int = 100
    location: int = 100
    props: int = 100
    story: int = 100
    temporal: int = 100
    visual: int = 100
    score: int = 100
    recommendation: str = "Đạt"


class VisualIssue(StrictModel):
    code: str
    severity: Literal["warning", "error"] = "error"
    message: str = ""


class VisualQCReport(StrictModel):
    status: Literal["Pending", "Passed", "Failed", "Unavailable"] = "Pending"
    score: int = Field(default=0, ge=0, le=100)
    character_identity: int = Field(default=0, ge=0, le=100)
    location_identity: int = Field(default=0, ge=0, le=100)
    prop_consistency: int = Field(default=0, ge=0, le=100)
    wardrobe_consistency: int = Field(default=0, ge=0, le=100)
    lighting_consistency: int = Field(default=0, ge=0, le=100)
    action_consistency: int = Field(default=0, ge=0, le=100)
    composition_consistency: int = Field(default=0, ge=0, le=100)
    first_frame: str = ""
    middle_frame: str = ""
    last_frame: str = ""
    model_id: str = ""
    issues: list[VisualIssue] = Field(default_factory=list)


class ContinuityQCReport(StrictModel):
    status: Literal["NotApplicable", "Pending", "Passed", "Failed", "Unavailable"] = "NotApplicable"
    score: int = Field(default=100, ge=0, le=100)
    character_match: int = Field(default=100, ge=0, le=100)
    location_match: int = Field(default=100, ge=0, le=100)
    wardrobe_match: int = Field(default=100, ge=0, le=100)
    prop_state_match: int = Field(default=100, ge=0, le=100)
    lighting_match: int = Field(default=100, ge=0, le=100)
    screen_direction_match: int = Field(default=100, ge=0, le=100)
    model_id: str = ""
    issues: list[VisualIssue] = Field(default_factory=list)


class ProductionAcceptance(StrictModel):
    status: Literal["Pending", "Accepted", "Rejected", "Blocked"] = "Pending"
    score: int = Field(default=0, ge=0, le=100)
    reasons: list[str] = Field(default_factory=list)


class VisualReference(StrictModel):
    id: str
    entity_type: Literal["character", "location", "prop"]
    entity_id: str
    name: str
    lock_text: str
    reference_images: list[str] = Field(default_factory=list)
    status: Literal["missing", "candidate", "approved", "rejected"] = "missing"
    approved_reference: str = ""
    source_scene_id: str = ""


class VisualBible(StrictModel):
    version: int = 1
    references: list[VisualReference] = Field(default_factory=list)


class SceneVisualPlan(StrictModel):
    dependency_mode: Literal["opening", "direct", "canonical"] = "canonical"
    anchor_scene_id: str = ""
    character_reference_ids: list[str] = Field(default_factory=list)
    location_reference_id: str = ""
    prop_reference_ids: list[str] = Field(default_factory=list)
    lock_prompt: str = ""


class Scene(StrictModel):
    id: str
    order: int
    title: str
    source_text: str
    summary: str
    characters: list[str] = Field(default_factory=list)
    location_id: str
    action: str
    camera: str
    lighting: str
    atmosphere: str
    duration: int = Field(ge=4, le=30)
    visual_prompt: str
    flow_prompt: str
    voiceover: str = ""
    dialogues: list[Dialogue] = Field(default_factory=list)
    start_state: ContinuityState
    end_state: ContinuityState
    reference_image: str = ""
    visual_plan: SceneVisualPlan = Field(default_factory=SceneVisualPlan)
    status: Literal[
        "Waiting",
        "Preparing",
        "Generating",
        "QC",
        "Accepted",
        "FailedQC",
        "Blocked",
        "Failed",
        "Paused",
        "Completed",
    ] = "Waiting"
    progress: int = Field(default=0, ge=0, le=100)
    selected: bool = False
    warnings: list[str] = Field(default_factory=list)
    result_url: str = ""
    result_file: str = ""
    last_frame_file: str = ""
    provider_job_id: str = ""
    upstream_project_id: str = ""
    upstream_workflow_id: str = ""
    upstream_media_id: str = ""
    upstream_resource_name: str = ""
    quality: QualityReport | None = None
    visual_qc: VisualQCReport = Field(default_factory=VisualQCReport)
    continuity_qc: ContinuityQCReport = Field(default_factory=ContinuityQCReport)
    acceptance: ProductionAcceptance = Field(default_factory=ProductionAcceptance)
    contract_version: int = 1
    contract_hash: str = ""
    ai_locked: bool = False
    ai_lock_reason: str = "Scene cũ chưa được AI Continuity Lock duyệt"


class FinalVideo(StrictModel):
    status: Literal["NotReady", "Ready", "Merging", "Completed", "Failed"] = "NotReady"
    progress: int = Field(default=0, ge=0, le=100)
    result_url: str = ""
    result_file: str = ""
    error: str = ""
    scene_count: int = Field(default=0, ge=0)
    generated_at: str = ""


class Project(StrictModel):
    schema_version: int = CURRENT_PROJECT_SCHEMA_VERSION
    id: str
    name: str
    original_text: str
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)
    settings: VideoSettings
    story_bible: StoryBible
    characters: list[Character] = Field(default_factory=list)
    locations: list[Location] = Field(default_factory=list)
    props: list[Prop] = Field(default_factory=list)
    timeline: list[str] = Field(default_factory=list)
    visual_style: str
    master_prompt: str
    visual_bible: VisualBible = Field(default_factory=VisualBible)
    scenes: list[Scene] = Field(default_factory=list)
    continuity_score: int = 100
    continuity_warnings: list[str] = Field(default_factory=list)
    flow_project_id: str = ""
    final_video: FinalVideo = Field(default_factory=FinalVideo)


class AnalyzeRequest(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    # Local desktop input can represent multi-hour screenplays. The pipeline streams
    # model work in bounded chunks, so the request itself is the only practical cap.
    original_text: str = Field(min_length=20, max_length=5_000_000)
    settings: VideoSettings = Field(default_factory=VideoSettings)


class SceneUpdate(StrictModel):
    source_text: str | None = None
    summary: str | None = None
    characters: list[str] | None = None
    location_id: str | None = None
    action: str | None = None
    camera: str | None = None
    lighting: str | None = None
    atmosphere: str | None = None
    duration: int | None = Field(default=None, ge=4, le=30)
    visual_prompt: str | None = None
    flow_prompt: str | None = None
    voiceover: str | None = None
    dialogues: list[Dialogue] | None = None
    start_state: ContinuityState | None = None
    end_state: ContinuityState | None = None
    reference_image: str | None = None
    selected: bool | None = None


class SceneLockUpdate(StrictModel):
    locked: bool


class GenerateRequest(StrictModel):
    scene_ids: list[str] = Field(default_factory=list)
    force_rerender: bool = False


class VideoProviderUpdate(StrictModel):
    provider: Literal["mock", "google-flow"]
    video_model: str = Field(min_length=1, max_length=200)


class ReorderRequest(StrictModel):
    scene_ids: list[str] = Field(min_length=1)


class XKiroConnectRequest(StrictModel):
    api_key: str = Field(min_length=8, max_length=500)


class XKiroModel(StrictModel):
    id: str
    display_name: str
    owned_by: str
    access_tier: str = "unknown"
    context_length: int | None = None
    max_output_tokens: int | None = None
    pricing: dict[str, object] = Field(default_factory=dict)
    capabilities: dict[str, bool] = Field(default_factory=dict)


class XKiroConnection(StrictModel):
    configured: bool
    key_hint: str = ""
    source: Literal["none", "environment", "session", "stored"] = "none"
    free_model_count: int = 0
    model_count: int = 0
    models: list[XKiroModel] = Field(default_factory=list)


class FlowCookieConnectRequest(StrictModel):
    cookie: str = Field(min_length=8, max_length=200_000)


class FlowVideoModel(StrictModel):
    id: str
    display_name: str
    note: str = ""


class FlowConnection(StrictModel):
    configured: bool
    authenticated: bool = False
    transport: Literal["none", "flow-cli"] = "none"
    cookie_count: int = 0
    message: str = ""
    flow_cli_available: bool = False
    browser_ready: bool = False
    credits_remaining: int | None = None
    tier: str = ""
    models: list[FlowVideoModel] = Field(default_factory=list)
