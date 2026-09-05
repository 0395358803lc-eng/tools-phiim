"""Automatic reference lifecycle and scene reference resolution."""

from __future__ import annotations

from pathlib import Path

from .flow_integration import FlowCLIIntegration, FlowIntegrationError
from .models import Project, Scene, VisualReference
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
    """Return the best currently approved physical reference for one Flow image input."""
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
    # Prefer a reference created from this continuity anchor, then a character, location, prop.
    anchored = [
        item for item in approved if item.source_scene_id == scene.visual_plan.anchor_scene_id
    ]
    ranked = anchored or sorted(
        approved,
        key=lambda item: {"character": 0, "location": 1, "prop": 2}.get(item.entity_type, 9),
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


def promote_accepted_scene_references(project: Project, scene: Scene) -> bool:
    """Promote an accepted scene frame as canonical refs for previously unseen entities."""
    if scene.acceptance.status != "Accepted" or not scene.visual_qc.middle_frame:
        return False
    changed = False
    for reference in relevant_references(project, scene):
        if reference.status == "approved" and reference.approved_reference:
            continue
        reference.approved_reference = scene.visual_qc.middle_frame
        if scene.visual_qc.middle_frame not in reference.reference_images:
            reference.reference_images.append(scene.visual_qc.middle_frame)
        reference.status = "approved"
        reference.source_scene_id = scene.id
        changed = True
    return changed


def _generation_prompt(reference: VisualReference, visual_style: str) -> str:
    if reference.entity_type == "character":
        purpose = (
            "Create one canonical film character reference: clear facial identity, three-quarter "
            "view and full-body appearance, one locked costume, neutral pose, no other people."
        )
    elif reference.entity_type == "location":
        purpose = (
            "Create one canonical cinematic environment reference with stable architecture, "
            "spatial layout, fixed landmarks and palette. No people and no redesign variants."
        )
    else:
        purpose = (
            "Create one canonical isolated prop reference showing exact form, material, color, "
            "scale cues and physical condition. Neutral background, no substitute objects."
        )
    return (
        f"{purpose}\nLOCKED SPECIFICATION:\n{reference.lock_text}\n"
        f"FILM STYLE: {visual_style}\n"
        "This becomes a persistent production identity reference. Do not improvise "
        "identity-changing details."
    )


class ReferenceManager:
    """Generate, vision-QC, approve and resolve canonical visual references."""

    def __init__(self, flow: FlowCLIIntegration, vision: VisualQCAnalyzer, data_root: Path) -> None:
        self.flow = flow
        self.vision = vision
        self.data_root = data_root.resolve()

    async def ensure_reference(self, project: Project, reference: VisualReference) -> bool:
        if reference.status == "approved" and reference.approved_reference:
            target = (self.data_root / reference.approved_reference).resolve()
            try:
                target.relative_to(self.data_root)
            except ValueError:
                target = Path()
            if target.is_file():
                return True
            reference.status = "missing"
            reference.approved_reference = ""

        if not self.flow.configured:
            return False
        try:
            generated = await self.flow.generate_reference_image(
                project.id, reference.id, _generation_prompt(reference, project.visual_style)
            )
        except FlowIntegrationError:
            reference.status = "missing"
            return False

        reference.status = "candidate"
        if generated not in reference.reference_images:
            reference.reference_images.append(generated)
        score, issues = await self.vision.inspect_reference(reference, generated)
        passed = score >= project.settings.quality_threshold and not any(
            issue.severity == "error" for issue in issues
        )
        if passed:
            reference.status = "approved"
            reference.approved_reference = generated
            return True
        reference.status = "rejected"
        reference.approved_reference = ""
        return False

    async def ensure_scene_references(self, project: Project, scene: Scene) -> bool:
        references = relevant_references(project, scene)
        if not references:
            return True
        results = [await self.ensure_reference(project, reference) for reference in references]
        return all(results)

    def resolve_scene_reference(self, project: Project, scene: Scene) -> str:
        return resolve_scene_reference(project, scene, self.data_root)
