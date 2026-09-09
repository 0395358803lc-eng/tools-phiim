"""Fail-closed scene dependency classification."""
from __future__ import annotations

from ..engines.continuity import is_direct_continuation
from ..models import Scene
from .canonical import DependencyMode


def classify_dependency(previous: Scene | None, current: Scene) -> DependencyMode:
    if previous is None:
        return DependencyMode.OPENING
    if previous.location_id != current.location_id:
        return DependencyMode.LOCATION_TRANSITION

    context = _context(current.source_text)
    previous_context = _context(previous.source_text)
    if "flashback" in context and "flashback" not in previous_context:
        return DependencyMode.FLASHBACK
    if "song song" in context or "parallel" in context:
        return DependencyMode.PARALLEL
    if _has_time_jump(previous, current):
        return DependencyMode.TIME_JUMP
    # Narrative dependency is authored by the screenplay. Exact previous-frame
    # reuse is a separate visual decision handled by is_direct_frame_anchor().
    # Never downgrade an authored continuous beat to a canonical cut merely because
    # the current boundary state still needs reconciliation.
    if not is_direct_continuation(previous, current):
        return DependencyMode.CANONICAL
    return DependencyMode.DIRECT


def _context(source_text: str) -> str:
    text = source_text.casefold()
    if "[scene context]" not in text:
        return ""
    text = text.split("[scene context]", 1)[1]
    if "[end context]" in text:
        text = text.split("[end context]", 1)[0]
    return text


def _has_time_jump(previous: Scene, current: Scene) -> bool:
    before = previous.end_state.time.strip()
    after = current.start_state.time.strip()
    if not before or not after or before == after:
        return False
    context = _context(current.source_text)
    continuity_words = ("liên tục", "continuous")
    return not any(word in context for word in continuity_words)
