"""Post-render visual quality and inter-scene continuity inspection."""

from __future__ import annotations

from pathlib import Path

from .analysis_providers.xkiro import XKiroClient, XKiroError
from .engines.continuity import is_direct_continuation
from .media_tools import VisualFrames, extract_visual_frames
from .models import (
    ContinuityQCReport,
    Project,
    Scene,
    VisualIssue,
    VisualQCReport,
    VisualReference,
)
from .production_gate import is_master_blocking_issue, master_reference_threshold
from .visual_bible import canonical_reference_lock


def _bounded(value: object, default: int = 0) -> int:
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return default
    return max(0, min(100, number))


def _looks_like_positive_non_issue(message: str, *, code: str = "") -> bool:
    text = " ".join(message.casefold().split())
    issue_code = str(code or "").casefold()
    if not text:
        return False

    hard_defect_markers = (
        "not clearly visible",
        "not visible",
        "is missing",
        "are missing",
        "must be removed",
        "must remove",
        "must be stripped",
        "must strip",
        "requires cleanup",
        "does not satisfy",
        "fails to",
        "incorrect",
        "wrong ",
        "conflict",
        "mismatch",
    )

    if issue_code == "missing_spatial_anchor":
        if (
            "no additional anchors are required" in text
            or "image satisfies this" in text
            or "locked spec only names" in text
            or "locked specification only names" in text
            or "acceptable as ordinary fixed fixture" in text
            or "acceptable as ordinary fixed fixtures" in text
            or "acceptable as a fixed fixture" in text
        ) and not any(marker in text for marker in hard_defect_markers):
            return True

    if issue_code == "layout_mismatch":
        if (
            "is acceptable" in text
            or "are acceptable" in text
            or "acceptable since" in text
            or "no layout conflict" in text
        ) and not any(marker in text for marker in hard_defect_markers):
            return True

    if issue_code == "transient_story_prop":
        if (
            "not a transient prop" in text
            or "no transient story props" in text
            or "no story props" in text
            or "no blocking defect" in text
            or "correctly retained" in text
            or "correctly preserved" in text
            or "acceptable as a fixed architectural anchor" in text
            or "acceptable as fixed architectural context" in text
        ) and not any(
            marker in text
            for marker in (
                "must be removed",
                "must remove",
                "transient prop is visible",
                "story prop is visible",
                "train is visible",
                "trains are visible",
                "vehicle is visible",
                "vehicles are visible",
                "cart is visible",
                "trolley is visible",
            )
        ):
            return True

    if issue_code == "missing_spatial_anchor":
        anchors_present = (
            "anchors are present" in text
            or "anchor is present" in text
            or "anchors are all present" in text
        )
        acceptable = (
            "acceptable as a lobby interior baseline" in text
            or "acceptable baseline" in text
            or "no blocking defect" in text
        )
        hard_missing = any(
            marker in text
            for marker in (
                "is missing",
                "are missing",
                "not visible",
                "absent",
                "cannot be located",
                "cannot identify",
            )
        )
        if anchors_present and acceptable and not hard_missing:
            return True

    if any(token in text for token in (" but ", " however ", " although ", " except ")):
        return False

    return any(
        marker in text
        for marker in (
            "scene is clean",
            "reference is clean",
            "no extra people",
            "no extra objects",
            "no contaminants",
            "no contamination",
            "no issue detected",
            "no issues detected",
            "no problem detected",
            "no problems detected",
            "satisfies the locked specification",
            "meets the locked specification",
            "correctly retained",
            "correctly preserved",
        )
    ) and any(
        marker in text
        for marker in (
            "detected",
            "present",
            "clean",
            "consistent",
            "satisfies",
            "meets",
            "correctly",
        )
    )


def _issues(values: object) -> list[VisualIssue]:
    if not isinstance(values, list):
        return []
    result: list[VisualIssue] = []
    for item in values:
        if isinstance(item, str):
            message = item[:500]
            if _looks_like_positive_non_issue(message):
                continue
            result.append(VisualIssue(code="VISION_NOTE", severity="warning", message=message))
            continue
        if not isinstance(item, dict):
            continue
        message = str(item.get("message") or item.get("detail") or "")[:500]
        code = str(item.get("code") or "VISION_NOTE")[:100]
        folded = " ".join(message.casefold().split())
        watermark_like = (
            "watermark" in folded
            or "ui overlay" in folded
            or (
                ("sparkle" in folded or "diamond" in folded)
                and ("corner" in folded or "lower right" in folded)
                and "artifact" in folded
            )
        )
        if watermark_like:
            result.append(
                VisualIssue(
                    code="watermark",
                    severity="warning",
                    message=message,
                )
            )
            continue
        if _looks_like_positive_non_issue(message, code=code):
            continue
        result.append(
            VisualIssue(
                code=code,
                severity="warning"
                if str(item.get("severity", "")).casefold() == "warning"
                else "error",
                message=message,
            )
        )
    return result


def _component_failures(
    values: dict[str, int],
    threshold: int,
    *,
    prefix: str,
) -> list[VisualIssue]:
    failures: list[VisualIssue] = []
    for name, score in values.items():
        if score >= threshold:
            continue
        failures.append(
            VisualIssue(
                code=f"{prefix}_{name.upper()}_BELOW_THRESHOLD"[:100],
                severity="error",
                message=f"{name} scored {score}, below required {threshold}.",
            )
        )
    return failures


def _data_path(data_root: Path, relative: str) -> Path | None:
    if not relative:
        return None
    candidate = (data_root / relative).resolve()
    try:
        candidate.relative_to(data_root.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def _reference_scene_context(project: Project, reference: VisualReference) -> str:
    """Summarize every downstream scene that depends on one Project Master."""
    rows: list[str] = []
    for scene in project.scenes:
        ids = set(scene.visual_plan.character_reference_ids)
        ids.update(scene.visual_plan.prop_reference_ids)
        if scene.visual_plan.location_reference_id:
            ids.add(scene.visual_plan.location_reference_id)
        if reference.id not in ids:
            continue
        rows.append(
            f"{scene.id} | location={scene.location_id} | "
            f"lighting={scene.lighting[:220]} | action={scene.action[:300]} | "
            f"source={scene.source_text[:500]}"
        )
    if not rows:
        return "No downstream scene currently references this Master."
    return "\n".join(rows)


def _master_qc_rules(reference: VisualReference) -> str:
    if reference.entity_type == "character":
        return (
            "CHARACTER MASTER RULES: This must be a neutral reusable identity asset, not a scene. "
            "Require a clean neutral/studio-like background, non-dramatic inspection lighting, "
            "neutral pose/expression, exact locked garment types, stable face/hair/body identity, "
            "and strict head-to-toe full-body framing with both feet fully visible and "
            "clean margin around the complete silhouette. The camera distance should match the "
            "repeatable "
            "character-Master template rather than a medium/waist/thigh crop. "
            "Use issue code incomplete_full_body when any body region or either foot is cropped, "
            "framing_mismatch when the shot is materially tighter/looser than a reusable full-body "
            "Master template. Also require no scene-specific weather/action/scenery. "
            "Use issue code scene_specific_background for an environmental backdrop, "
            "scene_specific_lighting for dramatic/colored lighting, "
            "action_pose for a narrative pose, wardrobe_mismatch for wrong garment type, "
            "hair_mismatch/age_mismatch/appearance_mismatch for observable lock conflicts. "
            "Those are blocking production defects."
        )
    if reference.entity_type == "location":
        return (
            "LOCATION MASTER RULES: Judge the persistent intersection across ALL downstream "
            "scenes. Require defining architecture/layout/fixed equipment and every spatial anchor "
            "that is explicitly named in the locked specification. Do NOT demand additional "
            "invented anchors when source truth defines only one anchor or one topology axis. "
            "Do not bake scene-specific weather, temporary light state, readable displays, people, "
            "handheld/action props or transient clutter into canonical identity. Trains, vehicles, "
            "carts, trolleys and other rolling stock are movable scene context, not "
            "Location-Master identity; when visible, report transient_story_prop and name the "
            "rolling stock in the "
            "issue message. Use issue codes layout_mismatch, missing_spatial_anchor, "
            "scene_specific_weather, scene_specific_lighting, transient_story_prop, people_present "
            "or readable_text for blocking defects. Ordinary stable furniture/decor is allowed."
        )
    if reference.entity_type == "prop":
        return (
            "PROP MASTER RULES: Require isolated reusable identity with exact locked form, "
            "material, color, scale and baseline state. Scene-specific handling/state changes "
            "belong to scenes, not the Master. Use issue codes shape_mismatch, "
            "material_mismatch, color_mismatch, "
            "state_mismatch or background_contamination for blocking defects."
        )
    return "Judge strict reusable canonical identity and source compliance."


class VisualQCAnalyzer:
    def __init__(self, data_root: Path, xkiro: XKiroClient) -> None:
        self.data_root = data_root.resolve()
        self.xkiro = xkiro

    def _reference_images(self, project: Project, scene: Scene) -> list[Path]:
        wanted = set(scene.visual_plan.character_reference_ids)
        wanted.update(scene.visual_plan.prop_reference_ids)
        if scene.visual_plan.location_reference_id:
            wanted.add(scene.visual_plan.location_reference_id)
        paths: list[Path] = []
        for reference in project.visual_bible.references:
            if reference.id not in wanted or reference.status != "approved":
                continue
            target = _data_path(self.data_root, reference.approved_reference)
            if target and target not in paths:
                paths.append(target)
            if len(paths) >= 4:
                break
        return paths

    async def inspect_scene(self, project: Project, scene: Scene) -> VisualQCReport:
        video = _data_path(self.data_root, scene.result_file)
        if not video:
            return VisualQCReport(
                status="Unavailable",
                issues=[
                    VisualIssue(code="VIDEO_MISSING", message="Rendered video file is missing.")
                ],
            )
        if all(
            (
                scene.visual_qc.first_frame,
                scene.visual_qc.quarter_frame,
                scene.visual_qc.middle_frame,
                scene.visual_qc.three_quarter_frame,
                scene.visual_qc.last_frame,
            )
        ):
            frames = VisualFrames(
                first=scene.visual_qc.first_frame,
                quarter=scene.visual_qc.quarter_frame,
                middle=scene.visual_qc.middle_frame,
                three_quarter=scene.visual_qc.three_quarter_frame,
                last=scene.visual_qc.last_frame,
            )
        else:
            frames = await extract_visual_frames(self.data_root, project.id, scene.id, video)
        if not all(
            (
                frames.first,
                frames.quarter,
                frames.middle,
                frames.three_quarter,
                frames.last,
            )
        ):
            return VisualQCReport(
                status="Unavailable",
                first_frame=frames.first,
                quarter_frame=frames.quarter,
                middle_frame=frames.middle,
                three_quarter_frame=frames.three_quarter,
                last_frame=frames.last,
                issues=[
                    VisualIssue(
                        code="FRAME_EXTRACTION_FAILED", message="Could not extract QC frames."
                    )
                ],
            )
        frame_paths = [
            _data_path(self.data_root, frames.first),
            _data_path(self.data_root, frames.quarter),
            _data_path(self.data_root, frames.middle),
            _data_path(self.data_root, frames.three_quarter),
            _data_path(self.data_root, frames.last),
        ]
        images = [path for path in frame_paths if path]
        images.extend(self._reference_images(project, scene))
        characters = {item.id: item.name for item in project.characters}
        location = next((item for item in project.locations if item.id == scene.location_id), None)
        props = {item.id: item.name for item in project.props}
        prop_ids = sorted(
            set(scene.start_state.prop_positions) | set(scene.end_state.prop_positions)
        )
        prompt = f"""You are a strict film visual continuity QC inspector.
The FIRST FIVE images are first/25%/50%/75%/last frames from one rendered scene.
Any later images are approved canonical references. Compare against them when present.
Evaluate identity, wardrobe, props, location, lighting and action across the full five-frame
progression, not only at the scene boundaries.
Expected visible characters: {[characters.get(cid, cid) for cid in scene.characters]}
Expected location: {location.name if location else scene.location_id}
Expected props: {[props.get(pid, pid) for pid in prop_ids]}
Expected lighting: {scene.lighting}
Expected action: {scene.action}
Visual Bible locks:
{scene.visual_plan.lock_prompt}
Judge only what is visually observable. Detect wrong identity, wrong location, wrong/missing props,
wardrobe drift, lighting/time drift, extra characters or major composition violations.
Return exactly one JSON object with integer scores 0-100: character_identity,
location_identity, prop_consistency, wardrobe_consistency, lighting_consistency,
action_consistency, composition_consistency, score, and issues.
issues is an array of objects with code,
severity ('warning' or 'error'), message. score must reflect production acceptability.
"""
        try:
            data, model_id = await self.xkiro.vision_json(
                images,
                prompt,
                model_id=project.settings.vision_model,
            )
        except XKiroError as exc:
            return VisualQCReport(
                status="Unavailable",
                first_frame=frames.first,
                quarter_frame=frames.quarter,
                middle_frame=frames.middle,
                three_quarter_frame=frames.three_quarter,
                last_frame=frames.last,
                issues=[VisualIssue(code="VISION_UNAVAILABLE", message=str(exc)[:500])],
            )
        values = {
            "character_identity": _bounded(
                data.get("character_identity"), 100 if not scene.characters else 0
            ),
            "location_identity": _bounded(data.get("location_identity")),
            "prop_consistency": _bounded(data.get("prop_consistency"), 100 if not prop_ids else 0),
            "wardrobe_consistency": _bounded(data.get("wardrobe_consistency"), 100),
            "lighting_consistency": _bounded(data.get("lighting_consistency")),
            "action_consistency": _bounded(data.get("action_consistency")),
            "composition_consistency": _bounded(data.get("composition_consistency")),
        }
        score = _bounded(data.get("score"), round(sum(values.values()) / len(values)))
        issues = _issues(data.get("issues"))
        issues.extend(
            _component_failures(
                values,
                project.settings.quality_threshold,
                prefix="VISUAL",
            )
        )
        passed = not any(issue.severity == "error" for issue in issues)
        return VisualQCReport(
            status="Passed" if passed else "Failed",
            score=score,
            first_frame=frames.first,
            quarter_frame=frames.quarter,
            middle_frame=frames.middle,
            three_quarter_frame=frames.three_quarter,
            last_frame=frames.last,
            model_id=model_id,
            issues=issues,
            **values,
        )

    async def inspect_reference(
        self,
        reference,
        relative_path: str,
        *,
        model_id: str = "",
    ) -> tuple[int, list[VisualIssue]]:
        return await self._inspect_reference(
            reference,
            relative_path,
            model_id=model_id,
        )

    async def inspect_reference_for_project(
        self,
        project: Project,
        reference: VisualReference,
        relative_path: str,
        *,
        model_id: str = "",
    ) -> tuple[int, list[VisualIssue]]:
        if not callable(getattr(self.xkiro, "vision_json", None)):
            return await self.inspect_reference(
                reference,
                relative_path,
                model_id=model_id,
            )
        return await self._inspect_reference(
            reference,
            relative_path,
            model_id=model_id,
            project=project,
        )

    async def inspect_reference_against_anchor(
        self,
        reference,
        relative_path: str,
        identity_anchor_path: str,
        *,
        model_id: str = "",
    ) -> tuple[int, list[VisualIssue]]:
        return await self._inspect_reference(
            reference,
            relative_path,
            model_id=model_id,
            identity_anchor_path=identity_anchor_path,
        )

    async def inspect_reference_against_anchor_for_project(
        self,
        project: Project,
        reference: VisualReference,
        relative_path: str,
        identity_anchor_path: str,
        *,
        model_id: str = "",
    ) -> tuple[int, list[VisualIssue]]:
        if not callable(getattr(self.xkiro, "vision_json", None)):
            return await self.inspect_reference_against_anchor(
                reference,
                relative_path,
                identity_anchor_path,
                model_id=model_id,
            )
        return await self._inspect_reference(
            reference,
            relative_path,
            model_id=model_id,
            identity_anchor_path=identity_anchor_path,
            project=project,
        )

    async def _inspect_reference(
        self,
        reference: VisualReference,
        relative_path: str,
        *,
        model_id: str = "",
        identity_anchor_path: str = "",
        project: Project | None = None,
    ) -> tuple[int, list[VisualIssue]]:
        target = _data_path(self.data_root, relative_path)
        if not target:
            return 0, [
                VisualIssue(code="REFERENCE_MISSING", message="Reference image file is missing.")
            ]

        anchor = _data_path(self.data_root, identity_anchor_path) if identity_anchor_path else None
        if anchor == target:
            anchor = None

        if anchor is None:
            mode_prompt = """
BOOTSTRAP MODE ? THIS IMAGE IS THE FIRST CANONICAL MASTER FOR THIS ENTITY.
There is intentionally no earlier canonical image to compare against. The supplied image is being
qualified to BECOME the persistent identity baseline. Judge only observable suitability against the
locked specification and production-reference hygiene. Do NOT reject, lower the score, or emit
identity_not_established / missing_prior_reference / cannot_verify_identity merely because no prior
canonical image exists. Identity persistence across later scenes is enforced by comparing future
outputs against this approved Master, not by requiring a predecessor for the first Master.
"""
            images = [target]
        else:
            mode_prompt = """
COMPARISON MODE ? TWO IMAGES ARE PROVIDED.
Image 1 is the existing identity anchor/baseline. Image 2 is the candidate replacement or corrected
Master. The candidate must preserve the same canonical identity/world design while fixing only real
defects. Reject identity drift between Image 1 and Image 2 in addition to ordinary specification
violations.
"""
            images = [anchor, target]

        downstream_context = (
            _reference_scene_context(project, reference)
            if project is not None
            else "Downstream scene context was not supplied."
        )
        threshold = master_reference_threshold(project, reference) if project is not None else 85
        locked_specification = (
            canonical_reference_lock(project, reference)
            if project is not None
            else reference.lock_text
        )
        prompt = f"""You are a strict Project Master acceptance inspector.
Entity type: {reference.entity_type}
Entity name: {reference.name}
Locked specification: {locked_specification}
{mode_prompt}

DOWNSTREAM SCENES THAT WILL REUSE THIS MASTER:
{downstream_context}

{_master_qc_rules(reference)}

Judge SOURCE COMPLIANCE and REUSABILITY, not beauty. A cinematic-looking image can still fail if it
bakes one scene's background, lighting, weather, action, text or transient state into canonical
identity. Do not infer ethnicity, nationality, brands or story facts that are absent from
source truth. For locations, stable furniture/decor is allowed; transient clutter/action props
are not.
For bootstrap Masters, do not penalize the lack of an earlier identity image.

Return exactly one JSON object with integer 0-100 fields:
spec_match, identity_clarity, downstream_reusability, transient_state_control, continuity_safety,
score, and issues.
score MUST equal the LOWEST of those five component scores, not an average.
Any component below {threshold} is production-blocking.
issues is an array of objects with code, severity ('warning' or 'error'), message.
Use the canonical blocking issue codes described above whenever they apply, and mark them error.
Only report real observable defects; omit categories that pass.
NEVER emit an issue object merely to explain that a criterion is satisfied, acceptable, absent,
correctly retained or not required. Every issue object must assert one concrete observable defect
that actually requires correction. If a feature passes, omit it from issues entirely.
"""
        try:
            data, _model_id = await self.xkiro.vision_json(
                images,
                prompt,
                model_id=model_id,
            )
        except XKiroError as exc:
            return 0, [VisualIssue(code="VISION_UNAVAILABLE", message=str(exc)[:500])]

        component_names = (
            "spec_match",
            "identity_clarity",
            "downstream_reusability",
            "transient_state_control",
            "continuity_safety",
        )
        present_components = {
            name: _bounded(data.get(name)) for name in component_names if name in data
        }
        score = (
            min(present_components.values()) if present_components else _bounded(data.get("score"))
        )
        issues = _issues(data.get("issues"))
        if project is not None:
            issues.extend(
                _component_failures(
                    present_components,
                    threshold,
                    prefix="MASTER",
                )
            )
        normalized: list[VisualIssue] = []
        for issue in issues:
            if is_master_blocking_issue(reference, issue.code) and issue.severity != "error":
                normalized.append(
                    VisualIssue(
                        code=issue.code,
                        severity="error",
                        message=issue.message,
                    )
                )
            else:
                normalized.append(issue)
        return score, normalized

    async def inspect_continuity(
        self,
        project: Project,
        previous: Scene | None,
        scene: Scene,
    ) -> ContinuityQCReport:
        if previous is None or not is_direct_continuation(previous, scene):
            return ContinuityQCReport(status="NotApplicable", score=100)
        previous_last = _data_path(
            self.data_root, previous.visual_qc.last_frame or previous.last_frame_file
        )
        current_first = _data_path(self.data_root, scene.visual_qc.first_frame)
        if not previous_last or not current_first:
            return ContinuityQCReport(
                status="Unavailable",
                score=0,
                issues=[
                    VisualIssue(
                        code="CONTINUITY_FRAMES_MISSING", message="Boundary frames are missing."
                    )
                ],
            )
        prompt = f"""You are a strict film shot continuity supervisor.
Image 1 is the LAST accepted frame of the previous scene. Image 2 is the FIRST frame of the
current direct-continuation scene. These scenes are expected to connect continuously.
Previous end state: {previous.end_state.model_dump()}
Current start state: {scene.start_state.model_dump()}
Current visual locks: {scene.visual_plan.lock_prompt}
Compare identity and physical continuity, not artistic style. Return exactly one JSON object with
integer scores 0-100: character_match, location_match, wardrobe_match, prop_state_match,
lighting_match, screen_direction_match, score, and issues. issues uses code/severity/message.
"""
        try:
            data, model_id = await self.xkiro.vision_json(
                [previous_last, current_first],
                prompt,
                model_id=project.settings.vision_model,
            )
        except XKiroError as exc:
            return ContinuityQCReport(
                status="Unavailable",
                score=0,
                issues=[VisualIssue(code="VISION_UNAVAILABLE", message=str(exc)[:500])],
            )
        values = {
            "character_match": _bounded(data.get("character_match")),
            "location_match": _bounded(data.get("location_match")),
            "wardrobe_match": _bounded(data.get("wardrobe_match")),
            "prop_state_match": _bounded(data.get("prop_state_match")),
            "lighting_match": _bounded(data.get("lighting_match")),
            "screen_direction_match": _bounded(data.get("screen_direction_match")),
        }
        score = _bounded(data.get("score"), round(sum(values.values()) / len(values)))
        issues = _issues(data.get("issues"))
        issues.extend(
            _component_failures(
                values,
                project.settings.quality_threshold,
                prefix="CONTINUITY",
            )
        )
        passed = not any(issue.severity == "error" for issue in issues)
        return ContinuityQCReport(
            status="Passed" if passed else "Failed",
            score=score,
            model_id=model_id,
            issues=issues,
            **values,
        )

    async def compare_continuity(
        self,
        project: Project,
        previous: Scene,
        scene: Scene,
    ) -> ContinuityQCReport:
        return await self.inspect_continuity(project, previous, scene)
