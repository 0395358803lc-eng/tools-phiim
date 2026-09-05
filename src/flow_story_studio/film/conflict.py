"""Authority-aware conflict resolution."""
from __future__ import annotations

from typing import Any

from .canonical import AuthorityLevel

_RANK = {
    AuthorityLevel.CREATIVE_ALLOWED: 1,
    AuthorityLevel.DERIVED_LOCKED: 2,
    AuthorityLevel.SOURCE_LOCKED: 3,
}


def resolve_value(
    current_value: Any,
    current_authority: AuthorityLevel,
    proposed_value: Any,
    proposed_authority: AuthorityLevel,
) -> Any:
    """Return the highest-authority value; never silently override a stronger fact."""
    if proposed_value == current_value:
        return current_value
    if _RANK[proposed_authority] > _RANK[current_authority]:
        return proposed_value
    return current_value


def proposal_conflicts(
    current_value: Any,
    current_authority: AuthorityLevel,
    proposed_value: Any,
    proposed_authority: AuthorityLevel,
) -> bool:
    return (
        proposed_value != current_value
        and _RANK[proposed_authority] < _RANK[current_authority]
    )
