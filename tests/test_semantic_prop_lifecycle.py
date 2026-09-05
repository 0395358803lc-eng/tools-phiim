from flow_story_studio.analysis_providers.semantic_orchestrator import (
    _is_direct_continuation,
    mentioned_props,
    safe_prop_states,
)
from flow_story_studio.engines.analyzer import analyze_story
from flow_story_studio.models import AnalyzeRequest, VideoSettings


def _project():
    script = """
TARGET RUNTIME: 32 seconds

CHARACTERS
- ALEX, adult man.

PROPS
- Yellow umbrella.
- Blue paper ticket.

SCENE 1 — PLATFORM — FLASHBACK
Alex holds the yellow umbrella and the blue paper ticket.

SCENE 2 — PLATFORM — PRESENT
Alex holds the blue paper ticket. The yellow umbrella is not here.

SCENE 3 — HALL — PRESENT
Alex tears the blue paper ticket in half.

SCENE 4 — HALL — CONTINUOUS
Alex walks away with empty hands.
"""
    return analyze_story(
        AnalyzeRequest(
            name="semantic prop lifecycle",
            original_text=script,
            settings=VideoSettings(scene_duration=8),
        )
    )


def test_prop_negation_and_temporal_boundary_are_source_grounded() -> None:
    project = _project()
    umbrella = next(item for item in project.props if "umbrella" in item.name.casefold())
    ticket = next(item for item in project.props if "ticket" in item.name.casefold())
    first, second, *_ = project.scenes

    assert not _is_direct_continuation(first, second)
    assert umbrella.id not in mentioned_props(second, project.props)
    assert ticket.id in mentioned_props(second, project.props)


def test_torn_prop_becomes_fragments_and_persists_until_explicit_disposal() -> None:
    project = _project()
    ticket = next(item for item in project.props if "ticket" in item.name.casefold())
    third, fourth = project.scenes[2], project.scenes[3]

    start, end = safe_prop_states(
        third,
        project.props,
        {},
        direct_continuation=False,
    )
    assert ticket.id in start
    assert ticket.id in end
    assert "two physical pieces" in end[ticket.id]

    next_start, _ = safe_prop_states(
        fourth,
        project.props,
        end,
        direct_continuation=_is_direct_continuation(third, fourth),
    )
    assert ticket.id in next_start
    assert "two physical pieces" in next_start[ticket.id]
