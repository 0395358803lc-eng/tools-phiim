from copy import deepcopy

from flow_story_studio.analysis_providers.finalization import finalize_project
from flow_story_studio.engines.analyzer import analyze_story
from flow_story_studio.film.beat_integrity import duplicate_scene_pairs
from flow_story_studio.film.validation import validate_project_hard_constraints
from flow_story_studio.models import AnalyzeRequest

SCRIPT = """
CHARACTERS
- ALEX, adult man.

SCENE 1 — ROOM — NIGHT
Alex opens the desk drawer and finds a sealed letter.

SCENE 2 — HALLWAY — NIGHT
Alex leaves the room, walks down the hallway, and hides the letter in his coat.
"""


def _project():
    return analyze_story(AnalyzeRequest(name="beat integrity", original_text=SCRIPT))


def test_hard_gate_rejects_ai_copying_distinct_source_beat() -> None:
    project = _project()
    first, second = project.scenes[:2]
    second.action = first.action
    second.summary = first.summary

    duplicates = duplicate_scene_pairs(project)
    verdict = validate_project_hard_constraints(project)

    assert duplicates
    assert verdict.is_valid is False
    assert any("duplicated production beat" in item for item in verdict.errors)


def test_finalization_restores_copied_ai_beat_from_source_draft() -> None:
    source_project = _project()
    ai_project = deepcopy(source_project)
    first, second = ai_project.scenes[:2]
    second.action = first.action
    second.summary = first.summary
    second.camera = first.camera

    repaired = finalize_project(ai_project, source_project)

    repaired_second = repaired.scenes[1]
    source_second = source_project.scenes[1]
    assert repaired_second.action == source_second.action
    assert repaired_second.summary == source_second.summary
    assert not duplicate_scene_pairs(repaired)
    assert any(
        "duplicated production beat" in warning
        for warning in repaired_second.warnings
    )
