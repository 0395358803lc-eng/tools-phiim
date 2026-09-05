"""Classify post-render QC failures and build bounded repair instructions."""
from __future__ import annotations

from .models import Scene

MAX_AUTO_RENDER_ATTEMPTS = 3


def _issue_codes(scene: Scene) -> list[str]:
    codes: list[str] = []
    for report in (scene.visual_qc, scene.audio_qc, scene.continuity_qc):
        for issue in report.issues:
            code = str(issue.code or "").strip().upper()
            if code and code not in codes:
                codes.append(code)
    return codes


def classify_qc_failures(scene: Scene) -> list[str]:
    categories: list[str] = []

    def add(value: str) -> None:
        if value not in categories:
            categories.append(value)

    for code in _issue_codes(scene):
        if any(token in code for token in ("IDENTITY", "CHARACTER")):
            add("IDENTITY_DRIFT")
        if "WARDROBE" in code or "CLOTHING" in code:
            add("WARDROBE_DRIFT")
        if "PROP" in code or "OBJECT" in code:
            add("PROP_STATE_ERROR")
        if "LOCATION" in code or "ARCHITECTURE" in code:
            add("LOCATION_DRIFT")
        if "ACTION" in code:
            add("ACTION_ERROR")
        if "CONTINUITY" in code or "SCREEN_DIRECTION" in code:
            add("CONTINUITY_ERROR")
        if any(
            token in code
            for token in (
                "AUDIO",
                "LOUDNESS",
                "PEAK",
                "SAMPLE_RATE",
                "SPEECH",
                "DIALOGUE",
                "VOICE",
            )
        ):
            add("AUDIO_ERROR")

    if scene.visual_qc.status not in {"Passed", "Pending"} and not categories:
        add("VISUAL_QUALITY_ERROR")
    if scene.audio_qc.status not in {"Passed", "Pending"}:
        add("AUDIO_ERROR")
    if scene.continuity_qc.status not in {"Passed", "NotApplicable", "Pending"}:
        add("CONTINUITY_ERROR")
    return categories


_REPAIR_RULES = {
    "IDENTITY_DRIFT": (
        "Reproduce only the canonical character identities from the locked contract. "
        "Do not alter recurring appearance, body proportions, hair, or identifying features."
    ),
    "WARDROBE_DRIFT": (
        "Restore the exact locked wardrobe version and accessories. "
        "No clothing variation is permitted without a source-backed state transition."
    ),
    "PROP_STATE_ERROR": (
        "Restore every required prop, owner, condition, and physical position from the "
        "entry/exit state. Preserve object permanence."
    ),
    "LOCATION_DRIFT": (
        "Restore the canonical location geometry, fixed objects, layout, palette, "
        "weather, and motivated light sources. Do not redesign the environment."
    ),
    "ACTION_ERROR": (
        "Execute the required source-grounded action exactly. Do not replace it with "
        "a similar beat, extra action, or summarized motion."
    ),
    "CONTINUITY_ERROR": (
        "Match the accepted previous boundary exactly where dependency_mode is direct: "
        "character/prop positions, wardrobe, lighting, screen direction, and environment."
    ),
    "AUDIO_ERROR": (
        "Preserve canonical dialogue speaker/text/delivery and ambience locks. "
        "Do not invent speech. Keep technical audio within the locked loudness "
        "and true-peak targets."
    ),
    "VISUAL_QUALITY_ERROR": (
        "Repair the failed visual QC observations without changing source facts or the locked "
        "film world."
    ),
}


def build_repair_instruction(scene: Scene) -> str:
    categories = classify_qc_failures(scene)
    if not categories:
        return ""
    strong = scene.render_attempt >= 2
    rules = " ".join(_REPAIR_RULES[item] for item in categories)
    prefix = (
        "STRICT REPAIR PASS. The previous generated video failed production QC. "
        if strong
        else "TARGETED REPAIR PASS. The previous generated video failed production QC. "
    )
    return (
        prefix
        + "Keep the immutable Render Contract and source truth unchanged. "
        + rules
        + " Generate a genuinely new take; do not reuse the failed media or job result."
    )


def can_auto_retry(scene: Scene) -> bool:
    codes = _issue_codes(scene)
    non_retryable = (
        "VISION_UNAVAILABLE",
        "VISION_NOT_CONFIGURED",
        "FFMPEG_UNAVAILABLE",
        "AUDIO_MEASUREMENT_FAILED",
        "AUDIO_NORMALIZATION_FAILED",
        "LOUDNESS_UNAVAILABLE",
        "LOUDNESS_OUT_OF_RANGE",
        "TRUE_PEAK_TOO_HIGH",
        "AUDIO_SAMPLE_RATE_LOW",
        "VIDEO_FILE_MISSING",
        "VIDEO_PATH_INVALID",
        "FRAME_EXTRACTION_FAILED",
        "CONTINUITY_FRAMES_MISSING",
    )
    if any(code in non_retryable for code in codes):
        return False
    return (
        scene.acceptance.status == "Rejected"
        and scene.render_attempt < MAX_AUTO_RENDER_ATTEMPTS
        and bool(classify_qc_failures(scene))
    )
