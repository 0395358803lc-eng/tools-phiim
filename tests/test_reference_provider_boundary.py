from types import SimpleNamespace

import pytest

from flow_story_studio.models import VisualReference
from flow_story_studio.reference_manager import ReferenceManager


@pytest.mark.asyncio
async def test_reference_manager_blocks_without_provider(tmp_path) -> None:
    manager = ReferenceManager(None, SimpleNamespace(), tmp_path)  # type: ignore[arg-type]
    reference = VisualReference(
        id="char-1",
        entity_type="character",
        entity_id="char-1",
        name="A",
        lock_text="stable identity",
    )
    project = SimpleNamespace()
    assert await manager.ensure_reference(project, reference) is False
    assert reference.status == "missing"


class _NoGenerateProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def generate_reference_image(self, project, reference_id, prompt):
        self.calls += 1
        raise AssertionError("existing candidate must be re-QC'd without generation")


class _VisionUnavailable:
    async def inspect_reference(self, reference, relative_path, *, model_id=""):
        from flow_story_studio.models import VisualIssue

        return 0, [
            VisualIssue(
                code="VISION_UNAVAILABLE",
                severity="error",
                message="billing/model unavailable",
            )
        ]


class _VisionPass:
    async def inspect_reference(self, reference, relative_path, *, model_id=""):
        return 92, []


@pytest.mark.asyncio
async def test_reference_manager_keeps_candidate_when_vision_is_unavailable(tmp_path) -> None:
    relative = "references/project/char-1.jpg"
    target = tmp_path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"existing-image")

    provider = _NoGenerateProvider()
    manager = ReferenceManager(provider, _VisionUnavailable(), tmp_path)  # type: ignore[arg-type]
    reference = VisualReference(
        id="char-1",
        entity_type="character",
        entity_id="char-1",
        name="A",
        lock_text="stable identity",
        status="candidate",
        reference_images=[relative],
        vision_model="premium-model",
    )
    project = SimpleNamespace(
        visual_style="cinematic",
        settings=SimpleNamespace(vision_model="premium-model", quality_threshold=85),
    )

    assert await manager.ensure_reference(project, reference) is False
    assert provider.calls == 0
    assert reference.status == "candidate"
    assert reference.reference_images == [relative]
    assert reference.approved_reference == ""
    assert reference.vision_issues[0].code == "VISION_UNAVAILABLE"


@pytest.mark.asyncio
async def test_reference_manager_reuses_rejected_image_after_vision_model_change(tmp_path) -> None:
    relative = "references/project/char-1.jpg"
    target = tmp_path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"existing-image")

    provider = _NoGenerateProvider()
    manager = ReferenceManager(provider, _VisionPass(), tmp_path)  # type: ignore[arg-type]
    reference = VisualReference(
        id="char-1",
        entity_type="character",
        entity_id="char-1",
        name="A",
        lock_text="stable identity",
        status="rejected",
        reference_images=[relative],
        vision_model="old-paid-model",
    )
    project = SimpleNamespace(
        visual_style="cinematic",
        settings=SimpleNamespace(vision_model="new-free-model", quality_threshold=85),
    )

    assert await manager.ensure_reference(project, reference) is True
    assert provider.calls == 0
    assert reference.status == "approved"
    assert reference.vision_score == 92
    assert reference.vision_model == "new-free-model"
    assert reference.approved_reference == relative


class _FailingProvider:
    async def generate_reference_image(self, project, reference_id, prompt):
        raise RuntimeError("provider boom")


@pytest.mark.asyncio
async def test_reference_manager_propagates_provider_generation_error(tmp_path) -> None:
    manager = ReferenceManager(_FailingProvider(), _VisionPass(), tmp_path)  # type: ignore[arg-type]
    reference = VisualReference(
        id="char-1",
        entity_type="character",
        entity_id="char-1",
        name="A",
        lock_text="stable identity",
    )
    project = SimpleNamespace(
        visual_style="cinematic",
        settings=SimpleNamespace(vision_model="vision", quality_threshold=85),
    )

    with pytest.raises(RuntimeError, match="provider boom"):
        await manager.ensure_reference(project, reference)
    assert reference.status == "missing"
    assert reference.approved_reference == ""


class _CaptureProvider:
    def __init__(self, root) -> None:
        self.root = root
        self.prompts = []

    async def generate_reference_image(self, project, reference_id, prompt):
        self.prompts.append(prompt)
        relative = f"references/{project.id}/{reference_id}.jpg"
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"corrected-image")
        return relative


@pytest.mark.asyncio
async def test_rejected_master_regeneration_uses_vision_feedback_in_prompt(tmp_path) -> None:
    from flow_story_studio.models import VisualIssue

    provider = _CaptureProvider(tmp_path)
    manager = ReferenceManager(provider, _VisionPass(), tmp_path)  # type: ignore[arg-type]
    reference = VisualReference(
        id="VIS-CHAR_003",
        entity_type="character",
        entity_id="CHAR_003",
        name="ONG HAI",
        lock_text=(
            "Male, 60 years old, light blue shirt and dark station uniform jacket."
        ),
        status="rejected",
        vision_model="vision-free",
        vision_score=82,
        vision_issues=[
            VisualIssue(
                code="wardrobe_state",
                severity="warning",
                message="Visible dirt/stain marks on the uniform.",
            ),
            VisualIssue(
                code="background_contamination",
                severity="warning",
                message="A wooden bench intrudes into the frame.",
            ),
        ],
    )
    project = SimpleNamespace(
        id="project-1",
        visual_style="cinematic realistic",
        settings=SimpleNamespace(
            vision_model="vision-free",
            quality_threshold=85,
        ),
    )

    assert await manager.ensure_reference(project, reference) is True
    assert len(provider.prompts) == 1
    prompt = provider.prompts[0]
    assert "canonical film CHARACTER MASTER" in prompt
    assert "seamless plain neutral-gray studio backdrop" in prompt
    assert "NO alley, station, apartment, rain, weather" in prompt
    assert "CORRECTIVE REGENERATION REQUIREMENTS" in prompt
    assert "wardrobe_state" in prompt
    assert "Visible dirt/stain marks on the uniform." not in prompt
    assert "background_contamination" in prompt
    assert "A wooden bench intrudes into the frame." not in prompt
    assert "untrusted evaluator output" not in prompt
    assert "Return wardrobe to the clean locked baseline condition" in prompt
    assert "Remove unrelated background objects and scenery" in prompt
    assert reference.status == "approved"
    assert reference.vision_score == 92


class _IngredientCaptureProvider:
    def __init__(self, root) -> None:
        self.root = root
        self.ingredient_files = None
        self.prompt = ""
        self.output_token = ""

    async def generate_reference_image(
        self,
        project,
        reference_id,
        prompt,
        *,
        ingredient_files=None,
    ):
        self.ingredient_files = list(ingredient_files or [])
        self.prompt = prompt
        self.output_token = reference_id
        relative = f"references/{project.id}/{reference_id}-corrected.jpg"
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"corrected-with-anchor")
        return relative


@pytest.mark.asyncio
async def test_rejected_master_retry_uses_existing_image_as_identity_ingredient(tmp_path) -> None:
    from flow_story_studio.models import VisualIssue

    relative = "references/project-1/VIS-CHAR_003.jpg"
    anchor = tmp_path / relative
    anchor.parent.mkdir(parents=True, exist_ok=True)
    anchor.write_bytes(b"previous-candidate")

    provider = _IngredientCaptureProvider(tmp_path)
    manager = ReferenceManager(provider, _VisionPass(), tmp_path)  # type: ignore[arg-type]
    reference = VisualReference(
        id="VIS-CHAR_003",
        entity_type="character",
        entity_id="CHAR_003",
        name="ONG HAI",
        lock_text="Male, 60 years old, light blue shirt, dark station uniform jacket.",
        status="rejected",
        reference_images=[relative],
        vision_model="vision-free",
        vision_score=82,
        vision_issues=[
            VisualIssue(
                code="background_contamination",
                severity="warning",
                message="Wooden bench intrudes into the frame.",
            )
        ],
    )
    project = SimpleNamespace(
        id="project-1",
        visual_style="cinematic realistic",
        settings=SimpleNamespace(
            vision_model="vision-free",
            quality_threshold=85,
        ),
    )

    assert await manager.ensure_reference(project, reference) is True
    assert provider.ingredient_files == [anchor.resolve()]
    assert "IDENTITY ANCHOR REQUIREMENT" in provider.prompt
    assert "background_contamination" in provider.prompt
    assert reference.status == "approved"



@pytest.mark.asyncio
async def test_rejected_location_retry_does_not_anchor_failed_candidate(tmp_path) -> None:
    from flow_story_studio.models import VisualIssue

    relative = "references/project-1/VIS-LOC_010.jpg"
    failed = tmp_path / relative
    failed.parent.mkdir(parents=True, exist_ok=True)
    failed.write_bytes(b"failed-location")

    provider = _IngredientCaptureProvider(tmp_path)
    manager = ReferenceManager(provider, _VisionPass(), tmp_path)  # type: ignore[arg-type]
    reference = VisualReference(
        id="VIS-LOC_010",
        entity_type="location",
        entity_id="LOC_010",
        name="Apartment",
        lock_text="stable apartment architecture and layout",
        status="rejected",
        reference_images=[relative],
        vision_model="vision-free",
        vision_score=82,
        vision_issues=[
            VisualIssue(
                code="contamination",
                severity="warning",
                message="Loose tableware is present.",
            ),
            VisualIssue(
                code="fixed_object_state",
                severity="warning",
                message="Door has unsupported heavy weathering.",
            ),
        ],
    )
    project = SimpleNamespace(
        id="project-1",
        visual_style="cinematic realistic",
        scenes=[],
        settings=SimpleNamespace(
            vision_model="vision-free",
            quality_threshold=85,
        ),
    )

    assert await manager.ensure_reference(project, reference) is True
    assert provider.ingredient_files == []
    assert provider.output_token == "VIS-LOC_010-retry-02"
    assert "IDENTITY ANCHOR REQUIREMENT" not in provider.prompt
    assert "clear tableware, bowls, cups, papers" in provider.prompt
    assert "Normalize unsupported wear" in provider.prompt
    assert reference.approved_reference.endswith(
        "VIS-LOC_010-retry-02-corrected.jpg"
    )
    assert reference.reference_images == [
        relative,
        reference.approved_reference,
    ]



@pytest.mark.asyncio
async def test_rejected_character_with_identity_or_wardrobe_issue_does_not_anchor_candidate(
    tmp_path,
) -> None:
    from flow_story_studio.models import VisualIssue

    relative = "references/project-1/VIS-CHAR_001.jpg"
    failed = tmp_path / relative
    failed.parent.mkdir(parents=True, exist_ok=True)
    failed.write_bytes(b"wrong-character-candidate")

    provider = _IngredientCaptureProvider(tmp_path)
    manager = ReferenceManager(provider, _VisionPass(), tmp_path)  # type: ignore[arg-type]
    reference = VisualReference(
        id="VIS-CHAR_001",
        entity_type="character",
        entity_id="CHAR_001",
        name="KHAI",
        lock_text="Dark grey shirt, black jacket, stable face and hair.",
        status="rejected",
        reference_images=[relative],
        vision_model="vision-free",
        vision_score=70,
        vision_issues=[
            VisualIssue(
                code="wardrobe_mismatch",
                severity="error",
                message="Wrong garment type.",
            )
        ],
    )
    project = SimpleNamespace(
        id="project-1",
        visual_style="cinematic realistic",
        settings=SimpleNamespace(
            vision_model="vision-free",
            quality_threshold=85,
        ),
    )

    assert await manager.ensure_reference(project, reference) is True
    assert provider.ingredient_files == []
    assert provider.output_token == "VIS-CHAR_001-retry-02"
    assert "IDENTITY ANCHOR REQUIREMENT" not in provider.prompt



@pytest.mark.asyncio
async def test_legacy_approved_character_below_new_gate_is_regenerated(tmp_path) -> None:
    relative = "references/project-1/VIS-CHAR_001.jpg"
    old = tmp_path / relative
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_bytes(b"legacy-approved")

    provider = _IngredientCaptureProvider(tmp_path)
    manager = ReferenceManager(provider, _VisionPass(), tmp_path)  # type: ignore[arg-type]
    reference = VisualReference(
        id="VIS-CHAR_001",
        entity_type="character",
        entity_id="CHAR_001",
        name="KHAI",
        lock_text="Dark grey shirt, black jacket, stable face and hair.",
        status="approved",
        approved_reference=relative,
        reference_images=[relative],
        vision_model="vision-free",
        vision_score=88,
        vision_issues=[],
    )
    project = SimpleNamespace(
        id="project-1",
        visual_style="cinematic realistic",
        settings=SimpleNamespace(
            vision_model="vision-free",
            quality_threshold=85,
        ),
    )

    assert await manager.ensure_reference(project, reference) is True
    assert provider.output_token == "VIS-CHAR_001-retry-02"
    assert provider.ingredient_files == []
    assert reference.approved_reference != relative
    assert reference.vision_score == 92



@pytest.mark.asyncio
async def test_detailed_location_master_does_not_reuse_scene_weather_context(tmp_path) -> None:
    from flow_story_studio.models import Location

    provider = _CaptureProvider(tmp_path)
    manager = ReferenceManager(provider, _VisionPass(), tmp_path)  # type: ignore[arg-type]
    reference = VisualReference(
        id="VIS-LOC_001",
        entity_type="location",
        entity_id="LOC_001",
        name="Apartment",
        lock_text=(
            "Legacy lock: Night 23:17, rainy-night mood, low-output LED and hallway spill."
        ),
    )
    location = Location(
        id="LOC_001",
        name="Apartment",
        place_type="Compact modest apartment",
        architecture="Intact modest urban apartment",
        space="Dining table near window; short clear route to entrance door",
        interior="Dining table and rain window are fixed anchors",
        objects=["rain-facing window", "low-output ceiling LED", "entrance door"],
        spatial_anchors="table ↔ rain window ↔ entrance door",
        lighting="rainy-night cold blue ambient",
        time_of_day="23:17",
        weather="rain",
    )
    project = SimpleNamespace(
        id="project-1",
        visual_style="cinematic realistic",
        locations=[location],
        characters=[],
        props=[],
        scenes=[
            SimpleNamespace(
                location_id="LOC_001",
                source_text="RAIN hits the window at NIGHT while the actor answers a phone.",
                lighting="cold blue rainy-night light",
                atmosphere="stormy and tense",
            )
        ],
        settings=SimpleNamespace(
            vision_model="vision-free",
            quality_threshold=85,
        ),
    )

    assert await manager.ensure_reference(project, reference) is True
    prompt = provider.prompts[0]
    assert "Legacy lock" not in prompt
    assert "rainy-night mood" not in prompt
    assert "23:17" not in prompt
    assert "low-output" not in prompt
    assert "RAIN hits the window" not in prompt
    assert "Dining table near window" in prompt
    assert "entrance door" in prompt
