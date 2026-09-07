from copy import deepcopy

from flow_story_studio.engines.analyzer import analyze_story
from flow_story_studio.film import (
    AuthorityLevel,
    build_canonical_film_model,
    build_scene_intents,
    classify_dependency,
    proposal_conflicts,
    resolve_value,
    validate_project_hard_constraints,
)
from flow_story_studio.film.qc import film_hard_blockers
from flow_story_studio.models import AnalyzeRequest
from flow_story_studio.scene_contracts import verify_scene_contract
from flow_story_studio.visual_bible import build_visual_bible

SCRIPT = """
CHARACTERS
- ALEX, adult man in a dark coat.

PROPS
- Blue paper ticket.

SCENE 1 — STATION — NIGHT
Alex stands beside the bench holding the blue paper ticket.

SCENE 2 — STATION — NIGHT — CONTINUOUS
Alex remains beside the same bench holding the same ticket.
"""


def _project():
    project = analyze_story(AnalyzeRequest(name="film-state", original_text=SCRIPT))
    assert len(project.scenes) >= 2
    first, second = project.scenes[:2]
    second.location_id = first.location_id
    second.start_state = deepcopy(first.end_state)
    return project


def test_direct_boundary_copies_exact_state() -> None:
    project = _project()
    first, second = project.scenes[:2]
    second.start_state.camera = "temporary next-shot camera"
    # Physical state is compatible; frame policy must replace the complete state.
    project = build_visual_bible(project)
    first, second = project.scenes[:2]
    assert second.visual_plan.dependency_mode == "direct"
    assert second.start_state == first.end_state


def test_new_entry_state_forces_canonical_reanchor() -> None:
    project = _project()
    first, second = project.scenes[:2]
    prop_id = project.props[0].id
    first.end_state.prop_positions = {}
    second.start_state.prop_positions = {prop_id: "newly revealed at scene start"}

    project = build_visual_bible(project)
    first, second = project.scenes[:2]
    assert second.visual_plan.dependency_mode == "canonical"
    assert second.start_state.prop_positions != first.end_state.prop_positions


def test_hard_validator_rejects_manual_false_direct() -> None:
    project = _project()
    first, second = project.scenes[:2]
    second.visual_plan.dependency_mode = "direct"
    second.start_state = deepcopy(first.end_state)
    second.start_state.prop_positions["PROP_FAKE"] = "invalid"

    verdict = validate_project_hard_constraints(project)
    assert verdict.is_valid is False
    assert any("unknown prop" in item for item in verdict.errors)
    assert any("direct boundary state mismatch" in item for item in verdict.errors)


def test_canonical_bridge_preserves_stable_entity_ids() -> None:
    project = _project()
    canonical = build_canonical_film_model(project)
    assert set(canonical.characters) == {item.id for item in project.characters}
    assert set(canonical.locations) == {item.id for item in project.locations}
    assert set(canonical.props) == {item.id for item in project.props}

    intents = build_scene_intents(project)
    assert [item.scene_id for item in intents] == [scene.id for scene in project.scenes]
    assert intents[0].required_location == project.scenes[0].location_id


def test_authority_resolver_never_overwrites_source_with_creative_value() -> None:
    assert proposal_conflicts(
        "source",
        AuthorityLevel.SOURCE_LOCKED,
        "creative",
        AuthorityLevel.CREATIVE_ALLOWED,
    )
    assert (
        resolve_value(
            "source",
            AuthorityLevel.SOURCE_LOCKED,
            "creative",
            AuthorityLevel.CREATIVE_ALLOWED,
        )
        == "source"
    )
    assert (
        resolve_value(
            "derived",
            AuthorityLevel.DERIVED_LOCKED,
            "source",
            AuthorityLevel.SOURCE_LOCKED,
        )
        == "source"
    )


def test_dependency_classifier_is_fail_closed() -> None:
    project = _project()
    first, second = project.scenes[:2]
    assert classify_dependency(first, second).value == "direct"
    second.characters = []
    assert classify_dependency(first, second).value == "canonical"


def test_analyzer_persists_film_model_and_immutable_render_contract() -> None:
    project = _project()
    assert project.film_model
    assert project.film_model["hard_gate"]["passed"] is True
    assert project.film_model["scene_intents"]

    scene = project.scenes[0]
    assert scene.orchestration
    assert scene.render_contract
    assert scene.render_contract_hash
    assert verify_scene_contract(scene)

    scene.render_contract["required_action"] = "tampered"
    assert verify_scene_contract(scene) is False


def test_audio_bible_is_canonical_and_embedded_in_prompt() -> None:
    project = _project()
    audio_bible = project.film_model["audio_bible"]
    assert set(audio_bible["voice_locks"]) == {item.id for item in project.characters}
    assert set(audio_bible["ambience_locks"]) == {item.id for item in project.locations}

    scene = project.scenes[0]
    audio_locks = scene.orchestration["audio_locks"]
    assert audio_locks["ambience_lock_id"] == scene.location_id
    assert "CANONICAL AUDIO BIBLE LOCKS:" in scene.render_prompt
    assert scene.render_contract["audio_locks"] == audio_locks


def test_orchestrator_seals_film_model_and_render_contracts() -> None:
    project = _project()
    project = build_visual_bible(project)
    from flow_story_studio.film.orchestrator import finalize

    project = finalize(project)
    assert project.film_model["hard_gate"]["passed"] is True
    assert project.film_model["scene_intents"]
    for scene in project.scenes:
        assert scene.render_contract
        assert len(scene.render_contract_hash) == 64
        assert scene.render_contract["scene_id"] == scene.id
        assert scene.render_contract["required_action"] == scene.action


def test_film_gate_rejects_stale_dialogue_contract() -> None:
    from flow_story_studio.film.orchestrator import finalize
    from flow_story_studio.film.qc import film_hard_blockers

    project = finalize(build_visual_bible(_project()))
    project.scenes[0].action += " changed after contract"
    blockers = film_hard_blockers(project)
    assert any("render contract action is stale" in item for item in blockers)


def test_film_hard_gate_detects_render_contract_tampering() -> None:
    project = _project()
    assert film_hard_blockers(project) == []

    scene = project.scenes[0]
    scene.render_contract["required_action"] = "changed after compilation"
    blockers = film_hard_blockers(project)
    assert any("render contract hash is stale" in item for item in blockers)
    assert any("render contract action is stale" in item for item in blockers)


def test_film_hard_gate_detects_dialogue_paraphrase() -> None:
    script = """
CHARACTERS
- ALEX, adult man.

SCENE 1 — ROOM — NIGHT
ALEX
Keep the door closed.
"""
    project = analyze_story(AnalyzeRequest(name="dialogue-gate", original_text=script))
    assert project.scenes[0].dialogues
    assert film_hard_blockers(project) == []

    project.scenes[0].dialogues[0].text = "Please shut the door."
    blockers = film_hard_blockers(project)
    assert any("canonical dialogue missing" in item for item in blockers)
    assert any("unexpected/paraphrased dialogue" in item for item in blockers)


def test_state_delta_is_recorded_for_each_scene() -> None:
    from flow_story_studio.film.orchestrator import prepare

    project = prepare(_project())
    for scene in project.scenes:
        assert "state_delta" in scene.orchestration
        assert isinstance(scene.orchestration["state_delta"], dict)


def test_runtime_state_commits_only_after_acceptance() -> None:
    import pytest

    from flow_story_studio.film.state_delta import (
        clear_accepted_runtime_state,
        commit_accepted_runtime_state,
    )

    scene = _project().scenes[0]
    with pytest.raises(ValueError, match="Production Acceptance"):
        commit_accepted_runtime_state(scene)

    scene.acceptance.status = "Accepted"
    scene.acceptance.score = 100
    commit_accepted_runtime_state(scene)
    assert scene.accepted_end_state == scene.end_state
    assert len(scene.accepted_state_hash) == 64

    clear_accepted_runtime_state(scene)
    assert scene.accepted_end_state is None
    assert scene.accepted_state_hash == ""


def test_film_model_hash_detects_post_seal_world_mutation() -> None:
    from flow_story_studio.film.orchestrator import finalize
    from flow_story_studio.film.qc import film_hard_blockers

    project = finalize(build_visual_bible(_project()))
    assert len(project.film_model_hash) == 64
    project.characters[0].clothing += " unauthorized mutation"
    blockers = film_hard_blockers(project)
    assert "canonical film model hash is stale" in blockers
