"""Automatic provider-neutral reference lifecycle and scene reference resolution."""

from __future__ import annotations

from pathlib import Path

from .models import Project, Scene, VisualReference
from .production_gate import master_reference_qc_blockers
from .providers.reference import ReferenceProvider
from .visual_bible import canonical_reference_lock
from .visual_qc import VisualQCAnalyzer


def _reference_map(project: Project) -> dict[str, VisualReference]:
    return {item.id: item for item in project.visual_bible.references}


def relevant_references(project: Project, scene: Scene) -> list[VisualReference]:
    by_id = _reference_map(project)
    ids = list(scene.visual_plan.character_reference_ids)
    if scene.visual_plan.location_reference_id:
        ids.append(scene.visual_plan.location_reference_id)
    ids.extend(scene.visual_plan.prop_reference_ids)
    return [by_id[item_id] for item_id in ids if item_id in by_id]


def resolve_scene_reference(project: Project, scene: Scene, data_root: Path) -> str:
    """Return the best currently approved physical reference."""
    if scene.reference_image:
        candidate = (data_root / scene.reference_image).resolve()
        try:
            candidate.relative_to(data_root.resolve())
        except ValueError:
            candidate = Path()
        if candidate.is_file():
            return scene.reference_image

    refs = relevant_references(project, scene)
    approved = [
        item for item in refs
        if item.status == "approved" and item.approved_reference
    ]
    anchored = [
        item for item in approved
        if item.source_scene_id == scene.visual_plan.anchor_scene_id
    ]
    ranked = anchored or sorted(
        approved,
        key=lambda item: {
            "character": 0,
            "location": 1,
            "prop": 2,
        }.get(item.entity_type, 9),
    )
    for item in ranked:
        candidate = (data_root / item.approved_reference).resolve()
        try:
            candidate.relative_to(data_root.resolve())
        except ValueError:
            continue
        if candidate.is_file():
            return item.approved_reference
    return ""


def scene_outputs_cannot_promote_master_references(
    project: Project,
    scene: Scene,
) -> bool:
    """Master References are project assets and are never promoted from scene output frames."""
    del project, scene
    return False


_GENERIC_LOCATION_MARKERS = (
    "kiến trúc đặc trưng",
    "bố cục",
    "được thiết lập ở cảnh đầu",
    "nội thất và vật liệu nhất quán",
    "theo nội dung gốc",
    "nhất quán giữa các cảnh",
    "không gian trong câu chuyện",
)


def _location_needs_source_context(project: Project, reference: VisualReference) -> bool:
    location = next(
        (
            item
            for item in getattr(project, "locations", [])
            if item.id == reference.entity_id
        ),
        None,
    )
    if location is None:
        return True
    fields = (
        str(location.architecture or "").casefold(),
        str(location.space or "").casefold(),
        str(location.interior or "").casefold(),
    )
    generic_hits = sum(
        any(marker in field for marker in _GENERIC_LOCATION_MARKERS)
        for field in fields
    )
    has_specific_objects = bool(location.objects)
    anchors = str(location.spatial_anchors or "").casefold()
    generic_anchor = "giữ nguyên vị trí tương đối" in anchors or not anchors.strip()
    return generic_hits >= 2 and not has_specific_objects and generic_anchor


def _location_scene_context(project: Project, reference: VisualReference) -> str:
    if reference.entity_type != "location":
        return ""
    if not _location_needs_source_context(project, reference):
        return ""
    scenes = [
        scene
        for scene in project.scenes
        if scene.location_id == reference.entity_id
    ]
    if not scenes:
        return ""
    parts = [
        f"{scene.id} STRUCTURAL SOURCE EVIDENCE ONLY:\n{scene.source_text[:900]}"
        for scene in scenes
    ]
    return (
        "SOURCE SCENES ARE EVIDENCE ONLY BECAUSE THIS LOCATION BIBLE IS TOO GENERIC:\n"
        + "\n".join(parts)
        + "\nHARD MASTER EXTRACTION RULE:\n"
        "Extract ONLY fixed architecture, layout, built-ins, fixed furniture/equipment, "
        "windows/doors, material junctions and repeatable spatial anchors. NEVER canonicalize "
        "weather, precipitation, time-of-day, darkness/brightness, color cast, lamp/LED on-off "
        "state, light intensity/spill, people, actions, handheld props, temporary clutter or "
        "readable screen/sign content, EVEN IF every supplied scene happens to share that same "
        "temporary state. Scene recurrence does not make a transient state canonical. "
        "When source names a defining fixed feature such as a CCTV monitor wall, ticket counter, "
        "elevator bank or door/window relationship, preserve the physical feature but not its "
        "display content or temporary light state."
    )


def _safe_corrective_instruction(reference: VisualReference, issue_code: str) -> str:
    """Translate Vision QC codes into deterministic generation instructions.

    Vision-model prose is intentionally not copied into the provider prompt. It is
    untrusted evaluator output and can be contradictory, overly specific, or contain
    instructions that should not control generation.
    """
    common = {
        "identity_not_established": (
            "Strengthen repeatable identity using stable, source-authorized physical features. "
            "Do not invent labels, brands, readable text or temporary story props."
        ),
        "identity_mismatch": (
            "Regenerate from the locked source identity instead of preserving the rejected "
            "candidate's face/body/design."
        ),
        "appearance_mismatch": (
            "Match only observable appearance facts present in the locked specification. "
            "Do not infer ethnicity, nationality or unrelated casting details."
        ),
        "age_mismatch": (
            "Correct the apparent age to the locked specification while keeping anatomy natural."
        ),
        "hair_mismatch": (
            "Correct hairstyle, length and color to the locked specification."
        ),
        "wardrobe_mismatch": (
            "Use the exact locked garment types, layers and colors. Do not substitute a T-shirt "
            "for a shirt or redesign a simple jacket into a different outerwear style."
        ),
        "wardrobe_state": (
            "Return wardrobe to the clean locked baseline condition unless the locked "
            "specification explicitly requires dirt, damage, wetness or distress."
        ),
        "scene_specific_background": (
            "Remove all story-location scenery. Use a plain neutral studio background for a "
            "character Master or an isolated neutral background for a prop Master."
        ),
        "background_contamination": (
            "Remove unrelated background objects and scenery that are not part of the canonical "
            "identity. Keep the reference visually clean and inspection-friendly."
        ),
        "scene_specific_lighting": (
            "Replace dramatic or story-specific lighting with neutral inspection lighting that "
            "reveals canonical identity without imposing a scene mood."
        ),
        "lighting_mood": (
            "Remove strong cinematic color grading and mood lighting from the Master baseline."
        ),
        "action_pose": (
            "Use a calm neutral reference pose/expression with no narrative action."
        ),
        "missing_specific_signature": (
            "Add distinctive non-textual, non-branded identity cues using only stable physical "
            "design features such as geometry, materials, built-ins, fixed lighting or silhouette."
        ),
        "ambiguous_spatial_anchors": (
            "Show multiple stable spatial anchors in one readable composition and make their "
            "relative positions unambiguous for later continuity matching."
        ),
        "missing_spatial_anchor": (
            "Restore the source-named fixed spatial anchor and show its relationship to the other "
            "canonical anchors clearly."
        ),
        "layout_mismatch": (
            "Rebuild the location from source-grounded architecture and relative anchor positions; "
            "do not preserve the rejected layout."
        ),
        "scene_specific_weather": (
            "Use a neutral dry baseline and remove every visible scene-weather or precipitation "
            "state. Preserve only the fixed architecture behind it."
        ),
        "transient_story_prop": (
            "Remove tickets, phones, recorders, cups and other scene-action props from the "
            "location Master unless explicitly part of fixed canonical identity."
        ),
        "people_present": (
            "Remove all people from the location Master."
        ),
        "readable_text": (
            "Remove readable signage, labels and display content unless the locked specification "
            "explicitly requires exact text."
        ),
        "logo_or_brand": (
            "Remove unrequested logos, brand marks and product labels."
        ),
        "contamination": (
            "Remove transient loose clutter, consumables and action props that are not part of "
            "the locked identity. For a location, clear tableware, bowls, cups, papers and other "
            "temporary items while keeping architecture and stable layout-defining furniture."
        ),
        "fixed_object_state": (
            "Normalize unsupported wear, patina, damage or temporary state on fixed objects. "
            "Preserve such state only when the locked specification explicitly requires it."
        ),
        "shape_mismatch": (
            "Correct the prop silhouette and geometry to the locked specification."
        ),
        "material_mismatch": (
            "Correct the prop material and surface response to the locked specification."
        ),
        "color_mismatch": (
            "Correct the prop's canonical color without changing form or material."
        ),
        "state_mismatch": (
            "Return the prop to its locked baseline physical state; scene-specific transformations "
            "must be applied later by scene logic."
        ),
        "reference_missing": (
            "Produce a clear canonical reference that visibly establishes the locked identity."
        ),
    }
    issue_key = str(issue_code or "").casefold()
    instruction = common.get(
        issue_key,
        (
            f"Correct Vision QC issue {issue_code!r} while preserving the locked source identity. "
            "Do not add unsupported story facts, readable text, brands or transient scene details."
        ),
    )
    if reference.entity_type == "character":
        return (
            instruction
            + " Keep face, apparent age, body proportions, hair and wardrobe identity stable."
        )
    if reference.entity_type == "location":
        return (
            instruction
            + " Prefer architecture, fixed furniture, windows, doors, built-ins "
            "and material junctions."
        )
    if reference.entity_type == "prop":
        return (
            instruction
            + " Keep form, material, scale, condition and distinctive geometry stable."
        )
    return instruction


def _use_rejected_candidate_anchor(reference: VisualReference) -> bool:
    """Reuse a rejected candidate only when its identity itself is not under dispute.

    Bootstrap candidates are not canonical. If Vision flags wardrobe/hair/shape/state/spec
    mismatch, anchoring the rejected image would lock the defect into every retry. Character/prop
    anchors are therefore allowed only for hygiene-only failures such as background or lighting.
    Locations are always regenerated from source truth until one is approved.
    """
    if reference.entity_type not in {"character", "prop"}:
        return False
    hygiene_only = {
        "background_contamination",
        "scene_specific_background",
        "lighting_mood",
        "scene_specific_lighting",
        "contamination",
        "watermark",
    }
    issue_codes = {issue.code.casefold() for issue in reference.vision_issues}
    return bool(issue_codes) and issue_codes.issubset(hygiene_only)


def _generation_output_token(reference: VisualReference) -> str:
    if reference.status != "rejected":
        return reference.id
    return f"{reference.id}-retry-{len(reference.reference_images) + 1:02d}"


def _identity_anchor_requirement(reference: VisualReference) -> str:
    if reference.entity_type == "character":
        return (
            "Use the supplied candidate only as a character identity anchor. Preserve face, "
            "apparent age, body proportions, hair and locked wardrobe identity while correcting "
            "the structured Vision QC defects."
        )
    if reference.entity_type == "prop":
        return (
            "Use the supplied candidate only as a prop identity anchor. Preserve form, scale, "
            "material and distinctive geometry while correcting the structured Vision QC defects."
        )
    return ""


def _generation_prompt(project: Project, reference: VisualReference) -> str:
    visual_style = project.visual_style
    purpose = {
        "character": (
            "Create one canonical film CHARACTER MASTER, not a cinematic scene. "
            "The image exists only to lock identity for reuse across many different locations. "
            "Use a seamless plain neutral-gray studio backdrop, soft diffuse near-neutral white "
            "inspection lighting, eye-level camera, natural standing posture and calm neutral "
            "expression. Show face, hair, body proportions and the complete locked wardrobe "
            "clearly. NO alley, station, apartment, rain, weather, bokeh scenery, dramatic rim "
            "light, colored cinematic grading, story action, handheld story props or "
            "scene-specific pose. "
            "Only wearable items explicitly required by the LOCKED SPECIFICATION may appear. "
            "Keep wardrobe clean, intact and exactly matched to the locked garment type; a shirt "
            "must remain a shirt, a jacket must not be redesigned into another outerwear style. "
            "Do not infer ethnicity, nationality or identity details beyond source truth."
        ),
        "location": (
            "Create one canonical cinematic environment reference with stable "
            "architecture and layout. This is a MASTER REFERENCE for visual inspection: "
            "use balanced, readable exposure and enough fill light to clearly reveal "
            "walls, flooring, furniture, materials, color palette and spatial layout. "
            "Use a repeatable neutral dry baseline with no visible weather effects, crowds, "
            "event dressing or dramatic time-of-day treatment unless the LOCKED SPECIFICATION "
            "explicitly defines such a condition as permanent architecture. "
            "Treat scene-specific weather, darkness and atmosphere as transient context rather "
            "than canonical location identity. Preserve structural lighting fixtures and the "
            "intended architectural mood without crushing shadows or hiding identity-defining "
            "environment details."
        ),
        "prop": (
            "Create one canonical isolated PROP MASTER on a plain neutral background. "
            "Show the complete object clearly with exact locked form, material, color, scale cues "
            "and baseline physical condition. No hands, people, story location, dramatic lighting "
            "or action composition. Keep the object clean and free of invented scratches, dirt, "
            "damage, stickers, labels or wear unless the LOCKED SPECIFICATION explicitly requires "
            "them. Treat switches, displays and LED on/off state as scene-level state unless the "
            "locked specification explicitly fixes that state; preserve the physical control/LED "
            "hardware itself."
        ),
    }.get(reference.entity_type, "Create one canonical production reference.")
    neutralization = {
        "character": (
            "Use neutral, non-branded wardrobe/accessories unless the locked specification "
            "explicitly requires otherwise. No logos, readable text, labels or copyrighted artwork."
        ),
        "location": (
            "Use neutral production-safe decor. No recognizable movie posters, "
            "copyrighted artwork, logos, brand marks, product labels or readable text "
            "unless the locked specification "
            "explicitly requires them."
        ),
        "prop": (
            "No logos, brand marks, product labels or readable text unless the locked "
            "specification explicitly requires them."
        ),
    }.get(reference.entity_type, "No unrequested logos, brands or readable text.")
    context = _location_scene_context(project, reference)
    sections = [
        purpose,
        f"LOCKED SPECIFICATION:\n{reference.lock_text}",
        f"FILM STYLE: {visual_style}",
        f"PRODUCTION-SAFE CONSTRAINTS: {neutralization}",
    ]
    if context:
        sections.append(context)
    if reference.status == "rejected" and reference.vision_issues:
        corrections = "\n".join(
            f"- {issue.code}: {_safe_corrective_instruction(reference, issue.code)}"
            for issue in reference.vision_issues
        )
        sections.append(
            "CORRECTIVE REGENERATION REQUIREMENTS:\n"
            "The previous candidate failed Vision QC. Regenerate the same canonical identity while "
            "fixing only the structured issues below. These instructions are derived from issue "
            "codes, not from free-form evaluator prose. Do not introduce unsupported "
            "scene context, temporary props, readable text, brands, wardrobe changes "
            "or identity drift.\n"
            f"{corrections}"
        )
    sections.append(
        "Preserve production identity. Do not improvise identity-changing details."
    )
    return "\n".join(sections)


class ReferenceManager:
    """Generate, vision-QC, approve and resolve canonical visual references."""
    def __init__(
        self,
        provider: ReferenceProvider | None,
        vision: VisualQCAnalyzer,
        data_root: Path,
    ) -> None:
        self.provider = provider
        self.vision = vision
        self.data_root = data_root.resolve()

    async def ensure_reference(
        self,
        project: Project,
        reference: VisualReference,
    ) -> bool:
        if reference.entity_type == "location":
            canonical_lock = canonical_reference_lock(project, reference)
            lock_changed = canonical_lock != reference.lock_text
            if lock_changed:
                if reference.status == "approved" and reference.approved_reference:
                    if reference.approved_reference not in reference.reference_images:
                        reference.reference_images.append(reference.approved_reference)
                    reference.status = "candidate"
                    reference.approved_reference = ""
                reference.lock_text = canonical_lock

        if reference.status == "approved" and reference.approved_reference:
            target = (self.data_root / reference.approved_reference).resolve()
            try:
                target.relative_to(self.data_root)
            except ValueError:
                target = Path()
            if target.is_file() and not master_reference_qc_blockers(project, reference):
                return True
            if target.is_file():
                reference.status = "rejected"
            else:
                reference.status = "missing"
            reference.approved_reference = ""

        generated = ""
        was_rejected = reference.status == "rejected"
        correction_anchor: Path | None = None
        correction_anchor_relative = ""
        can_reuse_existing = (
            reference.status == "candidate"
            or (
                reference.status == "rejected"
                and bool(reference.vision_model)
                and reference.vision_model != project.settings.vision_model
            )
        )
        if reference.reference_images:
            for relative in reversed(reference.reference_images):
                candidate = (self.data_root / relative).resolve()
                try:
                    candidate.relative_to(self.data_root)
                except ValueError:
                    continue
                if not candidate.is_file():
                    continue
                if can_reuse_existing:
                    generated = relative
                elif (
                    reference.status == "rejected"
                    and reference.vision_model == project.settings.vision_model
                    and _use_rejected_candidate_anchor(reference)
                ):
                    correction_anchor = candidate
                    correction_anchor_relative = relative
                break

        if not generated:
            if self.provider is None:
                return False
            prompt = _generation_prompt(project, reference)
            output_token = _generation_output_token(reference)
            if correction_anchor is not None:
                anchor_requirement = _identity_anchor_requirement(reference)
                if anchor_requirement:
                    prompt += "\nIDENTITY ANCHOR REQUIREMENT:\n" + anchor_requirement
            try:
                if correction_anchor is not None:
                    generated = await self.provider.generate_reference_image(
                        project,
                        output_token,
                        prompt,
                        ingredient_files=[correction_anchor],
                    )
                else:
                    generated = await self.provider.generate_reference_image(
                        project,
                        output_token,
                        prompt,
                    )
            except Exception:
                reference.status = "rejected" if was_rejected else "missing"
                reference.approved_reference = ""
                raise

            reference.status = "candidate"
            if generated not in reference.reference_images:
                reference.reference_images.append(generated)

        project_aware = getattr(self.vision, "inspect_reference_for_project", None)
        if correction_anchor_relative and correction_anchor_relative != generated:
            project_aware_anchor = getattr(
                self.vision,
                "inspect_reference_against_anchor_for_project",
                None,
            )
            legacy_anchor = getattr(
                self.vision,
                "inspect_reference_against_anchor",
                None,
            )
            if callable(project_aware_anchor):
                score, issues = await project_aware_anchor(
                    project,
                    reference,
                    generated,
                    correction_anchor_relative,
                    model_id=project.settings.vision_model,
                )
            elif callable(legacy_anchor):
                score, issues = await legacy_anchor(
                    reference,
                    generated,
                    correction_anchor_relative,
                    model_id=project.settings.vision_model,
                )
            elif callable(project_aware):
                score, issues = await project_aware(
                    project,
                    reference,
                    generated,
                    model_id=project.settings.vision_model,
                )
            else:
                score, issues = await self.vision.inspect_reference(
                    reference,
                    generated,
                    model_id=project.settings.vision_model,
                )
        elif callable(project_aware):
            score, issues = await project_aware(
                project,
                reference,
                generated,
                model_id=project.settings.vision_model,
            )
        else:
            score, issues = await self.vision.inspect_reference(
                reference,
                generated,
                model_id=project.settings.vision_model,
            )
        reference.vision_score = score
        reference.vision_issues = issues
        reference.vision_model = project.settings.vision_model
        passed = not master_reference_qc_blockers(project, reference)
        if passed:
            reference.status = "approved"
            reference.approved_reference = generated
            return True

        vision_unavailable = any(
            issue.code == "VISION_UNAVAILABLE" and issue.severity == "error"
            for issue in issues
        )
        reference.status = "candidate" if vision_unavailable else "rejected"
        reference.approved_reference = ""
        return False
    async def ensure_scene_references(
        self,
        project: Project,
        scene: Scene,
    ) -> bool:
        references = relevant_references(project, scene)
        if not references:
            return True
        results = [
            await self.ensure_reference(project, reference)
            for reference in references
        ]
        return all(results)

    def resolve_scene_reference(self, project: Project, scene: Scene) -> str:
        return resolve_scene_reference(project, scene, self.data_root)
