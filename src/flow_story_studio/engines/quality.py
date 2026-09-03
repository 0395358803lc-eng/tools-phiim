"""Structural render-readiness scoring; generated video pixels are not inspected."""

from __future__ import annotations

from ..models import QualityReport, Scene


def score_scene(scene: Scene, threshold: int) -> QualityReport:
    deductions = min(25, len(scene.warnings) * 5)
    prompt_complete = all(
        label in scene.flow_prompt
        for label in ("Character:", "Location:", "Action:", "Start frame:", "End frame:")
    )
    story = 98 if scene.action and scene.source_text else 75
    visual = 98 if prompt_complete else 78
    temporal = max(70, 100 - deductions)
    values = {
        "character": 98 if scene.characters else 94,
        "clothing": 98,
        "location": 98 if scene.location_id else 70,
        "props": 96,
        "story": story,
        "temporal": temporal,
        "visual": visual,
    }
    score = round(sum(values.values()) / len(values))
    return QualityReport(
        **values,
        score=score,
        recommendation="Render ready" if score >= threshold else "Review before render",
    )
