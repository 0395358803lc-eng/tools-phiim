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
    resolution: Literal["360p", "720p", "1080p", "highest"] = "1080p"
    style: str = "Cinematic"
    custom_style: str = ""
    scene_duration: int = Field(default=8, ge=4, le=30)
    character_lock: bool = True
    location_lock: bool = True
    auto_continuity: bool = True
    quality_threshold: int = Field(default=85, ge=0, le=100)
    provider: str = "unconfigured"
    video_model: str = ""
    image_model: str = ""
    analysis_provider: Literal["offline", "xkiro"] = "offline"
    analysis_model: str = ""
    vision_model: str = ""


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


class PropPhysicalState(StrictModel):
    entity_id: str
    instance_id: str = ""
    present: bool = True
    part: Literal["whole", "right_corner_fragment", "fragment", "unknown"] = "whole"
    owner_id: str = ""
    location_id: str = ""
    container: str = ""
    condition: str = "intact"
    piece_count: int = Field(default=1, ge=0)
    visibility: Literal["visible", "offscreen", "unknown"] = "unknown"
    scope: Literal[
        "physical_world",
        "cctv",
        "screen",
        "photo",
        "mirror",
        "phone_video",
        "recording",
        "memory",
        "imagined",
    ] = "physical_world"


class PropEvent(StrictModel):
    entity_id: str
    action: Literal[
        "appear",
        "pick_up",
        "place",
        "take_out",
        "put_away",
        "transfer",
        "tear",
        "drop",
        "destroy",
        "move",
        "inspect",
        "activate",
    ]
    actor_id: str = ""
    source_owner_id: str = ""
    target_owner_id: str = ""
    source_location: str = ""
    target_location: str = ""
    source_condition: str = ""
    target_condition: str = ""
    evidence: str = ""


class AISemanticFact(StrictModel):
    entity_id: str = ""
    fact_type: Literal[
        "presence",
        "absence",
        "ownership",
        "location",
        "condition",
        "part",
        "event",
        "scope",
        "time",
    ]
    value: str = ""
    evidence: str = ""
    confidence: int = Field(default=100, ge=0, le=100)


class AISemanticProposal(StrictModel):
    facts: list[AISemanticFact] = Field(default_factory=list)
    negative_facts: list[AISemanticFact] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    rejected_facts: list[AISemanticFact] = Field(default_factory=list)
    normalization_issues: list[str] = Field(default_factory=list)


class SceneTemporalState(StrictModel):
    timeline_branch: str = "main"
    daypart: str = "source-defined time"
    scene_clock: str = ""
    diegetic_clock_observations: dict[str, str] = Field(default_factory=dict)


class SceneSemanticTruth(StrictModel):
    entry_props: dict[str, PropPhysicalState] = Field(default_factory=dict)
    exit_props: dict[str, PropPhysicalState] = Field(default_factory=dict)
    entry_part_instances: dict[str, PropPhysicalState] = Field(default_factory=dict)
    exit_part_instances: dict[str, PropPhysicalState] = Field(default_factory=dict)
    prop_events: list[PropEvent] = Field(default_factory=list)
    temporal: SceneTemporalState = Field(default_factory=SceneTemporalState)
    perceptual_entities: dict[str, str] = Field(default_factory=dict)
    narrative_transition: Literal[
        "opening",
        "continuous",
        "cut",
        "location_transition",
        "time_jump",
        "flashback",
        "return_from_flashback",
        "parallel",
        "montage",
    ] = "cut"
    frame_anchor: Literal[
        "canonical_master",
        "previous_final_frame",
    ] = "canonical_master"
    source_trace: dict[str, str] = Field(default_factory=dict)


class SemanticReadinessReport(StrictModel):
    status: Literal["Ready", "Blocked"] = "Blocked"
    score: int = Field(default=0, ge=0, le=100)
    blockers: list[str] = Field(default_factory=list)
    dimensions: dict[str, int] = Field(default_factory=dict)


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
    quarter_frame: str = ""
    middle_frame: str = ""
    three_quarter_frame: str = ""
    last_frame: str = ""
    model_id: str = ""
    issues: list[VisualIssue] = Field(default_factory=list)


class AudioQCReport(StrictModel):
    status: Literal["Pending", "Passed", "Failed", "Unavailable"] = "Pending"
    score: int = Field(default=0, ge=0, le=100)
    audio_present: bool = False
    sample_rate_hz: int = Field(default=0, ge=0)
    channels: int = Field(default=0, ge=0)
    integrated_lufs: float | None = None
    true_peak_db: float | None = None
    clipping_detected: bool = False
    model_id: str = "ffmpeg"
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
    vision_score: int = Field(default=0, ge=0, le=100)
    vision_issues: list[VisualIssue] = Field(default_factory=list)
    vision_model: str = ""


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


class SceneImagePlan(StrictModel):
    status: Literal["Planned", "Ready", "Blocked", "Generated", "Approved", "Rejected"] = "Planned"
    renderer_status: Literal["unconfigured", "configured"] = "unconfigured"
    dependency_mode: Literal["opening", "direct", "canonical"] = "canonical"
    anchor_scene_id: str = ""
    start_frame_strategy: Literal[
        "canonical_reanchor",
        "previous_accepted_end_frame",
    ] = "canonical_reanchor"
    start_frame_source: str = ""
    start_frame_requirement: str = ""
    target_frame_strategy: str = "generate_scene_exit_keyframe"
    character_reference_ids: list[str] = Field(default_factory=list)
    location_reference_id: str = ""
    prop_reference_ids: list[str] = Field(default_factory=list)
    reference_status: dict[str, str] = Field(default_factory=dict)
    approved_reference_images: list[str] = Field(default_factory=list)
    identity_lock: str = ""
    composition_lock: str = ""
    start_frame_prompt: str = ""
    target_frame_prompt: str = ""
    negative_prompt: str = ""
    plan_hash: str = ""
    generated_start_frame: str = ""
    generated_target_frame: str = ""


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
    render_prompt: str
    voiceover: str = ""
    dialogues: list[Dialogue] = Field(default_factory=list)
    start_state: ContinuityState
    end_state: ContinuityState
    reference_image: str = ""
    visual_plan: SceneVisualPlan = Field(default_factory=SceneVisualPlan)
    image_plan: SceneImagePlan = Field(default_factory=SceneImagePlan)
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
    render_provider: str = ""
    render_model: str = ""
    provider_job_id: str = ""
    upstream_project_id: str = ""
    upstream_workflow_id: str = ""
    upstream_media_id: str = ""
    upstream_resource_name: str = ""
    quality: QualityReport | None = None
    visual_qc: VisualQCReport = Field(default_factory=VisualQCReport)
    audio_qc: AudioQCReport = Field(default_factory=AudioQCReport)
    continuity_qc: ContinuityQCReport = Field(default_factory=ContinuityQCReport)
    acceptance: ProductionAcceptance = Field(default_factory=ProductionAcceptance)
    contract_version: int = 1
    contract_hash: str = ""
    orchestration: dict[str, object] = Field(default_factory=dict)
    ai_semantic_proposal: AISemanticProposal = Field(default_factory=AISemanticProposal)
    semantic_truth: SceneSemanticTruth = Field(default_factory=SceneSemanticTruth)
    render_contract: dict[str, object] = Field(default_factory=dict)
    render_contract_hash: str = ""
    accepted_end_state: ContinuityState | None = None
    accepted_state_hash: str = ""
    render_attempt: int = Field(default=0, ge=0)
    runtime_repair_instruction: str = ""
    repair_history: list[str] = Field(default_factory=list)
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
    provider_project_id: str = ""
    film_model: dict[str, object] = Field(default_factory=dict)
    film_model_hash: str = ""
    semantic_readiness: SemanticReadinessReport = Field(default_factory=SemanticReadinessReport)
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
    render_prompt: str | None = None
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
    provider: str = "unconfigured"
    video_model: str = Field(default="", max_length=200)
    resolution: Literal["360p", "720p", "1080p", "highest"] | None = None


class VisionSettingsUpdate(StrictModel):
    vision_model: str = Field(min_length=1, max_length=200)


class ImageSettingsUpdate(StrictModel):
    image_model: str = Field(min_length=1, max_length=200)


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
