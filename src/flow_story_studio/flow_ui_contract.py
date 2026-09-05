"""Flow UI contract helpers.

The Flow web app changes DOM structure and A/B cohorts frequently.  This module
keeps model selection policy deterministic and testable without a live browser.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

DEFAULT_FLOW_VIDEO_MODEL = "veo-3.1-lite-lower-priority"
DEFAULT_FLOW_VIDEO_MODEL_LABEL = "Veo 3.1 - Lite [Lower Priority]"

_FLOW_MODEL_ALIASES: dict[str, tuple[str, ...]] = {
    DEFAULT_FLOW_VIDEO_MODEL: (
        "Veo 3.1 - Lite [Lower Priority]",
        "Veo 3.1 Lite [Lower Priority]",
        "Veo 3.1 - Lite · Lower Priority",
        "Veo 3.1 Lite · Lower Priority",
        "Veo 3.1 Lite Lower Priority",
    ),
}

_DISALLOWED_CREDITED_MARKERS = (
    "fast",
    "quality",
    "omni",
)

MODEL_OPTION_ROLES = (
    "menuitem",
    "menuitemradio",
    "option",
    "radio",
)


class FlowUIContractError(RuntimeError):
    """Flow UI no longer satisfies the safe production contract."""


@dataclass(frozen=True)
class ModelCandidate:
    index: int
    text: str


def normalize_flow_label(value: str) -> str:
    text = value.casefold()
    text = text.replace("arrow_drop_down", " ")
    text = text.replace("expand_more", " ")
    text = text.replace("check", " ")
    text = re.sub(r"[\[\]{}()·•|/_–—-]+", " ", text)
    text = re.sub(r"[^a-z0-9.]+", " ", text)
    return " ".join(text.split())


def model_aliases(model: str) -> tuple[str, ...]:
    normalized = model.strip().casefold().replace(" ", "-")
    return _FLOW_MODEL_ALIASES.get(normalized, (model,))


def is_lower_priority_model_label(value: str) -> bool:
    normalized = normalize_flow_label(value)
    required = ("veo", "3.1", "lite", "lower", "priority")
    if not all(token in normalized.split() for token in required):
        return False
    return not any(marker in normalized.split() for marker in _DISALLOWED_CREDITED_MARKERS)


def model_matches_contract(model: str, visible_label: str) -> bool:
    normalized_model = model.strip().casefold().replace(" ", "-")
    if normalized_model == DEFAULT_FLOW_VIDEO_MODEL:
        return is_lower_priority_model_label(visible_label)
    wanted = [normalize_flow_label(item) for item in model_aliases(model)]
    actual = normalize_flow_label(visible_label)
    return actual in wanted


def choose_model_candidate(model: str, labels: list[str]) -> ModelCandidate:
    matches = [
        ModelCandidate(index=index, text=label)
        for index, label in enumerate(labels)
        if model_matches_contract(model, label)
    ]
    if not matches:
        raise FlowUIContractError(
            f"Required Flow model {model!r} is not present; refusing any paid-model fallback."
        )
    if len(matches) > 1:
        raise FlowUIContractError(
            f"Flow model selector is ambiguous for {model!r}; refusing to guess."
        )
    return matches[0]


def assert_safe_video_model(model: str, *, allow_paid: bool = False) -> str:
    normalized = model.strip().casefold().replace(" ", "-")
    if normalized == DEFAULT_FLOW_VIDEO_MODEL:
        return DEFAULT_FLOW_VIDEO_MODEL
    if allow_paid:
        return normalized
    raise FlowUIContractError(
        "Paid/credited Flow video models are disabled. "
        f"Use {DEFAULT_FLOW_VIDEO_MODEL!r} or explicitly enable paid models."
    )
