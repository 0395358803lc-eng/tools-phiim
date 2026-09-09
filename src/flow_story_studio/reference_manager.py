"""Automatic provider-neutral reference lifecycle and scene reference resolution."""

from __future__ import annotations

from pathlib import Path

from .models import Project, Scene, VisualReference
from .production_gate import master_reference_qc_blockers
from .providers.reference import ReferenceProvider
from .visual_bible import canonical_reference_lock, source_grounded_location_clues
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
    approved = [item for item in refs if item.status == "approved" and item.approved_reference]
    anchored = [
        item for item in approved if item.source_scene_id == scene.visual_plan.anchor_scene_id
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
        (item for item in getattr(project, "locations", []) if item.id == reference.entity_id),
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
        any(marker in field for marker in _GENERIC_LOCATION_MARKERS) for field in fields
    )
    has_specific_objects = bool(location.objects)
    anchors = str(location.spatial_anchors or "").casefold()
    generic_anchor = "giữ nguyên vị trí tương đối" in anchors or not anchors.strip()
    return generic_hits >= 2 and not has_specific_objects and generic_anchor


def _source_grounded_location_clues(
    project: Project,
    reference: VisualReference,
) -> list[str]:
    """Compatibility wrapper around the shared canonical structural-clue extractor."""
    return source_grounded_location_clues(project, reference)


def _location_scene_context(project: Project, reference: VisualReference) -> str:
    if reference.entity_type != "location":
        return ""
    if not _location_needs_source_context(project, reference):
        return ""
    clues = _source_grounded_location_clues(project, reference)
    if not clues:
        return (
            "STRUCTURAL SOURCE FALLBACK:\n"
            "The screenplay provides no additional safe fixed clue beyond the location name. "
            "Use only the locked location type and neutral canonical architecture. Do NOT infer "
            "story props, people, readable signs, weather, time-of-day or lighting state."
        )
    bullets = "\n".join(f"- {item}" for item in clues)
    return (
        "SOURCE-GROUNDED STRUCTURAL CLUES ONLY:\n"
        f"{bullets}\n"
        "These clues were extracted deterministically from source evidence. They are the ONLY "
        "extra scene-derived facts allowed in this Location Master. Do NOT copy screenplay "
        "characters, actions, handheld/loose props, readable text, weather, time-of-day, "
        "temporary clutter or fixture on/off/color/intensity state into the image."
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
        "hair_mismatch": ("Correct hairstyle, length and color to the locked specification."),
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
            "Replace dramatic or story-specific lighting with flat neutral inspection exposure. "
            "Do not encode night/day, colored cast, motivated key/rim light, fixture glow, "
            "window weather light or cinematic grading into the Master."
        ),
        "transient_state_control": (
            "Reset the Location Master to a neutral canonical baseline: no weather, "
            "no time-of-day look, no readable displays, no loose story props, and no "
            "temporary fixture/light state."
        ),
        "lighting_mood": (
            "Remove strong cinematic color grading and mood lighting from the Master baseline."
        ),
        "action_pose": ("Use a calm neutral reference pose/expression with no narrative action."),
        "incomplete_full_body": (
            "Regenerate a true head-to-toe full-body character Master. Keep the entire head, "
            "body, both legs and both feet fully visible with clean margin around the silhouette; "
            "do not crop at the waist, thighs, knees, ankles or shoes."
        ),
        "framing_mismatch": (
            "Use the standard character-Master framing: eye-level, front-facing, full-body "
            "head-to-toe, both feet visible, centered subject, and consistent camera distance "
            "with the other canonical character Masters."
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
            "Restore every source-named fixed spatial anchor with unmistakable geometry and show "
            "its relative position to the other canonical anchors. For an entry threshold, make "
            "the door leaf, frame and floor threshold clearly readable at the Location-Master "
            "scale. Micro-details needed only for a close-up interaction, such as a small "
            "under-door gap used by a sliding ticket, belong to Scene Images and must not be "
            "required as a wide Location-Master visibility condition. Do not add unsourced "
            "furniture to make an anchor look plausible."
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
            "Remove all movable scene-level content from the Location Master: tickets, phones, "
            "recorders, cups, papers, bags, trains, vehicles, carts, trolleys and other rolling "
            "stock. Preserve only fixed architecture, built-ins, stable furniture/equipment and "
            "source-named spatial anchors."
        ),
        "people_present": ("Remove all people from the location Master."),
        "readable_text": (
            "Remove readable signage, labels and display content unless the locked specification "
            "explicitly requires exact text."
        ),
        "logo_or_brand": ("Remove unrequested logos, brand marks and product labels."),
        "contamination": (
            "Remove transient loose clutter, consumables and action props that are not part of "
            "the locked identity. For a location, clear tableware, bowls, cups, papers and other "
            "temporary items while keeping architecture and stable layout-defining furniture."
        ),
        "fixed_object_state": (
            "Normalize unsupported wear, patina, damage or temporary state on fixed objects. "
            "Preserve such state only when the locked specification explicitly requires it."
        ),
        "shape_mismatch": ("Correct the prop silhouette and geometry to the locked specification."),
        "material_mismatch": (
            "Correct the prop material and surface response to the locked specification."
        ),
        "color_mismatch": ("Correct the prop's canonical color without changing form or material."),
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
            instruction + " Prefer architecture, fixed furniture, windows, doors, built-ins "
            "and material junctions."
        )
    if reference.entity_type == "prop":
        return (
            instruction + " Keep form, material, scale, condition and distinctive geometry stable."
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
            "expression. STRICT FULL-BODY FRAMING: show the subject head-to-toe with the entire "
            "head, both legs and both feet/shoes fully visible, centered, with clean margin above "
            "the head and below the feet. Never crop at the waist, thighs, knees, ankles or shoes. "
            "Use the same repeatable camera distance/framing template for every character Master. "
            "Show face, hair, body proportions and the complete locked wardrobe clearly. "
            "NO alley, station, apartment, rain, weather, bokeh scenery, dramatic rim "
            "light, colored cinematic grading, story action, handheld story props or "
            "scene-specific pose. "
            "Only wearable items explicitly required by the LOCKED SPECIFICATION may appear. "
            "Keep wardrobe clean, intact and exactly matched to the locked garment type; a shirt "
            "must remain a shirt, a jacket must not be redesigned into another outerwear style. "
            "Do not infer ethnicity, nationality or identity details beyond source truth."
        ),
        "location": (
            "Create one canonical LOCATION MASTER inspection plate, NOT a cinematic scene. "
            "Lock stable architecture, layout, built-ins, stable furniture/equipment, material "
            "junctions and every source-named spatial anchor. Use a wide readable 16:9 composition "
            "that shows the source-named anchors and their relative geometry in one view whenever "
            "physically possible; do not invent extra anchors merely to decorate the space. "
            "LIGHTING MUST BE NEUTRAL INSPECTION EXPOSURE: broad even near-white fill "
            "with readable walls, floor, ceiling, furniture and materials. No night/day look, "
            "no dramatic shadows, no colored cast, no motivated key/rim light, no fixture glow "
            "emphasis and no cinematic color grade. Structural light fixtures may remain "
            "physically visible but MUST be OFF/unlit in the Master; illuminate the environment "
            "only with "
            "neutral non-diegetic inspection fill so fixture on/off/intensity/color is never baked "
            "into canonical identity. Windows and "
            "exterior openings may remain as fixed anchors, but any exterior beyond them must be "
            "soft neutral/featureless and must not encode cityscape brightness, sky color, weather "
            "or time-of-day. Use a neutral dry baseline with no weather, fog, crowds or event "
            "dressing. ABSOLUTELY NO people, trains, vehicles, carts, trolleys or other rolling "
            "stock, and no loose story objects such as tickets, phones, recorders, cups, papers, "
            "bags, clothing or temporary clutter. Include only stable furniture/equipment named by "
            "the LOCKED SPECIFICATION or source-grounded structural clues; omit unsourced benches, "
            "stools, chairs and decorative furniture that could become false continuity anchors. "
            "No readable text, "
            "numbers, labels, brands or screen content anywhere; signs/displays must be blank or "
            "unreadable unless exact text is explicitly locked. This image is an identity map for "
            "future scenes, not a storytelling frame."
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
    style_section = (
        "STYLE REFERENCE FOR MATERIAL REALISM ONLY: "
        f"{visual_style} "
        "For a Location Master, do NOT apply scene mood, cinematic lighting, time-of-day "
        "or color grading from this style text."
        if reference.entity_type == "location"
        else f"FILM STYLE: {visual_style}"
    )
    sections = [
        purpose,
        f"LOCKED SPECIFICATION:\n{reference.lock_text}",
        style_section,
        f"PRODUCTION-SAFE CONSTRAINTS: {neutralization}",
    ]
    if context:
        sections.append(context)
    if reference.status == "rejected" and reference.vision_issues:
        actionable_codes: list[str] = []
        seen_codes: set[str] = set()
        for issue in reference.vision_issues:
            code = str(issue.code or "").strip()
            normalized = code.casefold()
            if not code or normalized.startswith("master_") or normalized in seen_codes:
                continue
            seen_codes.add(normalized)
            actionable_codes.append(code)
        corrections = "\n".join(
            f"- {code}: {_safe_corrective_instruction(reference, code)}"
            for code in actionable_codes
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
    sections.append("Preserve production identity. Do not improvise identity-changing details.")
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
        can_reuse_existing = reference.status == "candidate" or (
            reference.status == "rejected"
            and bool(reference.vision_model)
            and reference.vision_model != project.settings.vision_model
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
            issue.code == "VISION_UNAVAILABLE" and issue.severity == "error" for issue in issues
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
        results = [await self.ensure_reference(project, reference) for reference in references]
        return all(results)

    def resolve_scene_reference(self, project: Project, scene: Scene) -> str:
        return resolve_scene_reference(project, scene, self.data_root)
