"""Post-render visual quality and inter-scene continuity inspection."""

from __future__ import annotations

from pathlib import Path

from .analysis_providers.xkiro import XKiroClient, XKiroError
from .engines.continuity import is_direct_continuation
from .flow_media import VisualFrames, extract_visual_frames
from .models import ContinuityQCReport, Project, Scene, VisualIssue, VisualQCReport


def _bounded(value: object, default: int = 0) -> int:
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return default
    return max(0, min(100, number))


def _issues(values: object) -> list[VisualIssue]:
    if not isinstance(values, list):
        return []
    result: list[VisualIssue] = []
    for item in values:
        if isinstance(item, str):
            result.append(VisualIssue(code="VISION_NOTE", severity="warning", message=item[:500]))
            continue
        if not isinstance(item, dict):
            continue
        result.append(
            VisualIssue(
                code=str(item.get("code") or "VISION_NOTE")[:100],
                severity="warning"
                if str(item.get("severity", "")).casefold() == "warning"
                else "error",
                message=str(item.get("message") or item.get("detail") or "")[:500],
            )
        )
    return result


def _data_path(data_root: Path, relative: str) -> Path | None:
    if not relative:
        return None
    candidate = (data_root / relative).resolve()
    try:
        candidate.relative_to(data_root.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


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
            (scene.visual_qc.first_frame, scene.visual_qc.middle_frame, scene.visual_qc.last_frame)
        ):
            frames = VisualFrames(
                first=scene.visual_qc.first_frame,
                middle=scene.visual_qc.middle_frame,
                last=scene.visual_qc.last_frame,
            )
        else:
            frames = await extract_visual_frames(self.data_root, project.id, scene.id, video)
        if not all((frames.first, frames.middle, frames.last)):
            return VisualQCReport(
                status="Unavailable",
                first_frame=frames.first,
                middle_frame=frames.middle,
                last_frame=frames.last,
                issues=[
                    VisualIssue(
                        code="FRAME_EXTRACTION_FAILED", message="Could not extract QC frames."
                    )
                ],
            )
        frame_paths = [
            _data_path(self.data_root, frames.first),
            _data_path(self.data_root, frames.middle),
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
The FIRST THREE images are first/middle/last frames from one rendered scene.
Any later images are approved canonical references. Compare against them when present.
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
composition_consistency, score, and issues. issues is an array of objects with code,
severity ('warning' or 'error'), message. score must reflect production acceptability.
"""
        try:
            data, model_id = await self.xkiro.vision_json(images, prompt)
        except XKiroError as exc:
            return VisualQCReport(
                status="Unavailable",
                first_frame=frames.first,
                middle_frame=frames.middle,
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
            "composition_consistency": _bounded(data.get("composition_consistency")),
        }
        score = _bounded(data.get("score"), round(sum(values.values()) / len(values)))
        issues = _issues(data.get("issues"))
        passed = score >= project.settings.quality_threshold and not any(
            issue.severity == "error" for issue in issues
        )
        return VisualQCReport(
            status="Passed" if passed else "Failed",
            score=score,
            first_frame=frames.first,
            middle_frame=frames.middle,
            last_frame=frames.last,
            model_id=model_id,
            issues=issues,
            **values,
        )

    async def inspect_reference(
        self,
        reference,
        relative_path: str,
    ) -> tuple[int, list[VisualIssue]]:
        target = _data_path(self.data_root, relative_path)
        if not target:
            return 0, [
                VisualIssue(code="REFERENCE_MISSING", message="Reference image file is missing.")
            ]
        prompt = f"""You are a strict canonical visual reference inspector.
Entity type: {reference.entity_type}
Entity name: {reference.name}
Locked specification: {reference.lock_text}
Judge whether the supplied image is suitable as a persistent production identity reference.
Reject identity ambiguity, extra people/objects that contaminate the reference, alternate costume
variants, wrong architecture/layout, wrong prop shape/material/color/state, or major mismatch with
the locked specification. Return exactly one JSON object with integer score 0-100 and issues array.
issues uses objects with code, severity ('warning' or 'error'), message.
"""
        try:
            data, _model_id = await self.xkiro.vision_json([target], prompt)
        except XKiroError as exc:
            return 0, [VisualIssue(code="VISION_UNAVAILABLE", message=str(exc)[:500])]
        return _bounded(data.get("score")), _issues(data.get("issues"))

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
            data, model_id = await self.xkiro.vision_json([previous_last, current_first], prompt)
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
        passed = score >= project.settings.quality_threshold and not any(
            issue.severity == "error" for issue in issues
        )
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
