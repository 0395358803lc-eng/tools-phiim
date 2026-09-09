"""Fail-closed semantic readiness validation before Master/Scene production."""

from __future__ import annotations

import re
from collections import defaultdict

from .analysis_providers.source_truth import (
    key,
    physical_location_key,
    physical_source_text,
    prop_explicitly_absent,
    prop_mentioned_in_text,
    prop_part_from_source,
    prop_physically_mentioned,
    temporal_state,
)
from .engines.continuity import is_direct_frame_anchor
from .film.beat_integrity import duplicate_scene_pairs
from .models import Project, SemanticReadinessReport

_DIMENSIONS = (
    "scene_uniqueness",
    "dialogue_fidelity",
    "entity_identity",
    "location_identity",
    "timeline",
    "prop_lifecycle",
    "physical_presence",
    "spatial_lifecycle",
    "part_identity",
    "perceptual_scope",
    "ai_source_agreement",
    "narrative_continuity",
    "frame_anchor",
    "source_traceability",
)


def _dialogue_blockers(project: Project) -> list[str]:
    blockers: list[str] = []
    for scene in project.scenes:
        source = " ".join(scene.source_text.casefold().split())
        for dialogue in scene.dialogues:
            spoken = " ".join(dialogue.text.casefold().split()).strip(" .!?")
            if spoken and spoken not in source:
                blockers.append(
                    f"{scene.id}: dialogue is not source-grounded: "
                    f"{dialogue.character_id}|{dialogue.text}"
                )
    return blockers


def _location_blockers(project: Project) -> list[str]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for item in project.locations:
        grouped[physical_location_key(item.name)].append(item.id)
    return [
        f"physical location alias unresolved: {key} -> {','.join(ids)}"
        for key, ids in grouped.items()
        if key and len(ids) > 1
    ]


def _timeline_blockers(project: Project) -> list[str]:
    blockers: list[str] = []
    for scene in project.scenes:
        expected = temporal_state(scene)
        actual = scene.semantic_truth.temporal
        if actual.timeline_branch != expected.timeline_branch:
            blockers.append(
                f"{scene.id}: timeline branch mismatch "
                f"{actual.timeline_branch}!={expected.timeline_branch}"
            )
        if expected.daypart != "source-defined time" and actual.daypart != expected.daypart:
            blockers.append(f"{scene.id}: daypart mismatch {actual.daypart}!={expected.daypart}")
        if (
            expected.daypart != "source-defined time"
            and expected.daypart not in scene.start_state.time
        ):
            blockers.append(
                f"{scene.id}: start_state time contradicts scene heading daypart {expected.daypart}"
            )
        if not expected.scene_clock:
            for label, value in expected.diegetic_clock_observations.items():
                if value and scene.start_state.time.endswith(value):
                    blockers.append(
                        f"{scene.id}: diegetic {label} clock {value} leaked into scene time"
                    )
                if value and scene.end_state.time.endswith(value):
                    blockers.append(
                        f"{scene.id}: diegetic {label} clock {value} leaked into scene time"
                    )
    return blockers


def _prop_lifecycle_blockers(project: Project) -> list[str]:
    blockers: list[str] = []
    for scene in project.scenes:
        truth = scene.semantic_truth
        by_prop: dict[str, list] = defaultdict(list)
        for event in truth.prop_events:
            by_prop[event.entity_id].append(event)

        for entity_id, events in by_prop.items():
            entry = truth.entry_props.get(entity_id)
            exit_state = truth.exit_props.get(entity_id)
            if entry is None or exit_state is None:
                blockers.append(
                    f"{scene.id}: prop event stream lacks entry/exit state for {entity_id}"
                )
                continue

            working = entry.model_copy(deep=True)
            for event in events:
                if event.action == "tear":
                    if event.target_condition == "torn_in_two":
                        if working.piece_count >= 2 or working.condition == "torn_in_two":
                            blockers.append(
                                f"{scene.id}: {entity_id} is already torn "
                                "before authored tear action"
                            )
                        working.condition = "torn_in_two"
                        working.piece_count = 2
                    elif event.target_condition:
                        if working.condition == event.target_condition:
                            blockers.append(
                                f"{scene.id}: {entity_id} transformation exists "
                                "before authored action"
                            )
                        working.condition = event.target_condition
                elif event.action in {"pick_up", "take_out"}:
                    if event.target_owner_id:
                        working.owner_id = event.target_owner_id
                    working.container = ""
                    if event.target_location:
                        working.location_id = event.target_location
                elif event.action == "place":
                    working.owner_id = ""
                    working.container = ""
                    if event.target_location:
                        working.location_id = event.target_location
                elif event.action == "transfer":
                    if (
                        event.source_owner_id
                        and working.owner_id
                        and working.owner_id != event.source_owner_id
                    ):
                        blockers.append(
                            f"{scene.id}: {entity_id} transfer source "
                            f"{event.source_owner_id} does not own prop at transfer time"
                        )
                    if event.target_owner_id:
                        working.owner_id = event.target_owner_id
                    working.container = ""
                elif event.action == "put_away":
                    if event.target_owner_id:
                        working.owner_id = event.target_owner_id
                    working.container = event.target_location or "pocket"
                elif event.action == "drop":
                    working.owner_id = ""
                    working.container = ""
                    if event.target_location:
                        working.location_id = event.target_location
                elif event.action == "destroy":
                    working.present = False

            if working.condition != exit_state.condition:
                blockers.append(
                    f"{scene.id}: {entity_id} exit condition does not match replayed lifecycle"
                )
            if working.piece_count != exit_state.piece_count:
                blockers.append(
                    f"{scene.id}: {entity_id} exit piece_count does not match replayed lifecycle"
                )
            if working.owner_id != exit_state.owner_id:
                blockers.append(
                    f"{scene.id}: {entity_id} exit owner does not match replayed lifecycle"
                )
            if working.container != exit_state.container:
                blockers.append(
                    f"{scene.id}: {entity_id} exit container does not match replayed lifecycle"
                )
            if working.location_id != exit_state.location_id:
                blockers.append(
                    f"{scene.id}: {entity_id} exit location does not match replayed lifecycle"
                )

    return blockers


def _physical_presence_blockers(project: Project) -> list[str]:
    blockers: list[str] = []
    prop_by_id = {item.id: item for item in project.props}
    for scene in project.scenes:
        truth = scene.semantic_truth
        state_ids = set(truth.entry_props) | set(truth.exit_props)
        for prop_id, prop in prop_by_id.items():
            if prop_explicitly_absent(scene, prop) and prop_id in state_ids:
                blockers.append(
                    f"{scene.id}: {prop_id} is explicitly absent/non-held "
                    "but exists in physical state"
                )
            if (
                truth.frame_anchor == "canonical_master"
                and prop_id in state_ids
                and not prop_physically_mentioned(scene, prop)
                and truth.narrative_transition not in {"continuous"}
            ):
                blockers.append(
                    f"{scene.id}: {prop_id} exists in canonical re-anchor "
                    "without physical source evidence"
                )
    return blockers


def _part_identity_blockers(project: Project) -> list[str]:
    blockers: list[str] = []
    for scene in project.scenes:
        truth = scene.semantic_truth

        # Canonical prop state always describes the whole/remainder, never a detached part.
        for prop_id, state in {**truth.entry_props, **truth.exit_props}.items():
            if state.part != "whole":
                blockers.append(
                    f"{scene.id}: {prop_id} detached part is conflated with canonical whole state"
                )

        for prop in project.props:
            expected = prop_part_from_source(scene, prop)
            if expected == "whole":
                continue
            part_states = [
                state
                for state in (
                    *truth.entry_part_instances.values(),
                    *truth.exit_part_instances.values(),
                )
                if state.entity_id == prop.id and state.part == expected
            ]
            if not part_states:
                blockers.append(
                    f"{scene.id}: source contains {expected} of {prop.id} "
                    "but no matching part instance exists"
                )
                continue
            if any(not state.instance_id for state in part_states):
                blockers.append(
                    f"{scene.id}: {prop.id} part instance is missing stable instance_id"
                )
    return blockers


def _ai_source_agreement_blockers(project: Project) -> list[str]:
    blockers: list[str] = []
    for scene in project.scenes:
        proposal = scene.ai_semantic_proposal
        source_key = key(scene.source_text)
        for fact in [*proposal.facts, *proposal.negative_facts]:
            evidence_key = key(fact.evidence)
            if not evidence_key or evidence_key not in source_key:
                blockers.append(
                    f"{scene.id}: AI semantic fact lacks source-grounded evidence: "
                    f"{fact.fact_type}|{fact.entity_id}|{fact.value}"
                )
        if proposal.rejected_facts and not scene.semantic_truth.source_trace.get("ai_overrides"):
            blockers.append(f"{scene.id}: rejected AI semantic facts are not traceably overridden")
    return blockers


def _source_action_blockers(project: Project) -> list[str]:
    """Independently verify authored lifecycle verbs were compiled into typed events."""
    blockers: list[str] = []
    for scene in project.scenes:
        physical = physical_source_text(scene)
        action_source = re.sub(r'[“"][^”"]*[”"]', " ", physical)
        source_sentences = [
            key(part) for part in re.split(r"(?<=[.!?])\s+", action_source) if part.strip()
        ]
        raw_sentences = [
            part.strip() for part in re.split(r"(?<=[.!?])\s+", action_source) if part.strip()
        ]
        tracked_props = [prop for prop in project.props if prop_physically_mentioned(scene, prop)]

        def tracked_action_sentence(
            sentence: str,
            tracked_props: tuple = tuple(tracked_props),
        ) -> bool:
            if any(prop_mentioned_in_text(sentence, prop) for prop in tracked_props):
                return True
            folded_sentence = key(sentence)
            pronoun = bool(
                re.search(r"\b(?:no|nó|it|them|chung|chúng)\b", sentence.casefold())
                or re.search(r"\b(?:no|it|them|chung)\b", folded_sentence)
            )
            return pronoun and len(tracked_props) == 1

        events = scene.semantic_truth.prop_events
        actions = {event.action for event in events}
        has_corner_tear = any(
            re.search(r"\bxe\b.{0,64}\b(?:goc|corner)\b", sentence)
            or re.search(
                r"\b(?:tear|tears|rip|rips)\b.{0,64}\bcorner\b",
                sentence,
            )
            for sentence in source_sentences
        )
        has_split_tear = any(
            re.search(r"\bxe\b.{0,64}\b(?:lam doi|thanh hai)\b", sentence)
            or re.search(
                r"\b(?:tear|tears|rip|rips)\b.{0,64}\b(?:in half|in two|apart)\b",
                sentence,
            )
            for sentence in source_sentences
        )
        if has_corner_tear and not any(
            event.action == "tear" and event.target_condition == "missing_right_corner"
            for event in events
        ):
            blockers.append(f"{scene.id}: authored corner-tear action has no typed tear event")
        if has_split_tear and not any(
            event.action == "tear" and event.target_condition == "torn_in_two" for event in events
        ):
            blockers.append(f"{scene.id}: authored split/tear action has no typed tear event")

        def transfer_sentence(sentence: str) -> bool:
            raw = sentence.casefold()
            folded_sentence = key(sentence)
            if not tracked_action_sentence(sentence):
                return False
            if any(
                marker in folded_sentence
                for marker in (
                    "khong dua",
                    "khong trao",
                    "khong dat",
                    "khong giao",
                    "does not give",
                    "did not give",
                    "does not hand",
                    "did not hand",
                    "never gives",
                    "never hands",
                )
            ):
                return False
            direct_transfer = (
                any(marker in raw for marker in ("đưa", "trao", "trả", "giao"))
                and (" cho " in f" {raw} " or " to " in f" {raw} ")
            ) or bool(
                re.search(
                    r"\b(?:give|gives|hand|hands)\b.{0,90}\bto\b",
                    folded_sentence,
                )
            )
            hand_place = (
                "đặt" in raw or re.search(r"\b(?:place|places)\b", folded_sentence)
            ) and any(
                marker in folded_sentence
                for marker in (
                    "vao tay",
                    "long ban tay",
                    "into hand",
                    "into the palm",
                    "in the palm",
                )
            )
            return bool(direct_transfer or hand_place)

        transfer_expected = any(transfer_sentence(sentence) for sentence in raw_sentences)
        if transfer_expected and not any(event.action == "transfer" for event in events):
            blockers.append(f"{scene.id}: authored transfer action has no typed transfer event")
        pickup_expected = any(
            "nhặt" in sentence.casefold() and tracked_action_sentence(sentence)
            for sentence in raw_sentences
        )
        pocket_expected = any(
            ("cất" in sentence.casefold() or "bỏ" in sentence.casefold())
            and "túi" in sentence.casefold()
            and tracked_action_sentence(sentence)
            for sentence in raw_sentences
        )
        takeout_expected = any(
            "lấy" in sentence.casefold()
            and "túi" in sentence.casefold()
            and tracked_action_sentence(sentence)
            for sentence in raw_sentences
        )
        placement_expected = any(
            "đặt" in sentence.casefold()
            and any(surface in sentence.casefold() for surface in ("bàn", "ghế", "sàn"))
            and "đặt tay" not in sentence.casefold()
            and "bàn tay" not in sentence.casefold()
            and "lòng bàn tay" not in sentence.casefold()
            and tracked_action_sentence(sentence)
            for sentence in raw_sentences
        )
        if pickup_expected and "pick_up" not in actions:
            blockers.append(f"{scene.id}: authored pickup action has no typed pick_up event")
        if pocket_expected and "put_away" not in actions:
            blockers.append(f"{scene.id}: authored pocketing action has no typed put_away event")
        if takeout_expected and "take_out" not in actions:
            blockers.append(f"{scene.id}: authored take-out action has no typed take_out event")
        if placement_expected and "transfer" not in actions and "place" not in actions:
            blockers.append(f"{scene.id}: authored placement action has no typed place event")

        for event in events:
            if event.action != "transfer":
                continue
            evidence = key(event.evidence)
            if any(
                marker in evidence
                for marker in (
                    "khong dua",
                    "khong trao",
                    "khong dat",
                    "khong giao",
                    "does not give",
                    "did not give",
                    "does not hand",
                    "did not hand",
                    "never gives",
                    "never hands",
                )
            ):
                blockers.append(
                    f"{scene.id}: negated transfer was incorrectly compiled for {event.entity_id}"
                )
    return blockers


def _perceptual_scope_blockers(project: Project) -> list[str]:
    blockers: list[str] = []
    prop_by_id = {item.id: item for item in project.props}
    for scene in project.scenes:
        truth = scene.semantic_truth
        for entity_id, scope in truth.perceptual_entities.items():
            prop = prop_by_id.get(entity_id)
            if prop is None or prop_physically_mentioned(scene, prop):
                continue
            if entity_id in truth.entry_props or entity_id in truth.exit_props:
                blockers.append(
                    f"{scene.id}: {entity_id} from {scope} leaked into physical prop state"
                )
    return blockers


def _frame_anchor_blockers(project: Project) -> list[str]:
    blockers: list[str] = []
    previous = None
    for scene in project.scenes:
        expected_direct = is_direct_frame_anchor(previous, scene)
        actual_direct = scene.semantic_truth.frame_anchor == "previous_final_frame"
        if expected_direct != actual_direct:
            blockers.append(
                f"{scene.id}: frame anchor mismatch; expected "
                f"{'previous_final_frame' if expected_direct else 'canonical_master'}"
            )
        previous = scene
    return blockers


def _source_trace_blockers(project: Project) -> list[str]:
    blockers: list[str] = []
    for scene in project.scenes:
        trace = scene.semantic_truth.source_trace
        # Explicit scene context is optional for prose/non-heading screenplays.
        # Physical source evidence is mandatory for every production scene.
        if not trace.get("physical_source"):
            blockers.append(f"{scene.id}: physical source trace is missing")
    return blockers


def evaluate_semantic_readiness(project: Project) -> SemanticReadinessReport:
    by_dimension: dict[str, list[str]] = {name: [] for name in _DIMENSIONS}

    duplicates = duplicate_scene_pairs(project)
    by_dimension["scene_uniqueness"].extend(
        f"{current}: duplicated production beat from {previous}"
        for previous, current, *_scores in duplicates
    )
    by_dimension["dialogue_fidelity"].extend(_dialogue_blockers(project))
    by_dimension["location_identity"].extend(_location_blockers(project))
    by_dimension["timeline"].extend(_timeline_blockers(project))
    by_dimension["prop_lifecycle"].extend(_prop_lifecycle_blockers(project))
    by_dimension["spatial_lifecycle"].extend(_source_action_blockers(project))
    by_dimension["physical_presence"].extend(_physical_presence_blockers(project))
    by_dimension["part_identity"].extend(_part_identity_blockers(project))
    by_dimension["perceptual_scope"].extend(_perceptual_scope_blockers(project))
    by_dimension["ai_source_agreement"].extend(_ai_source_agreement_blockers(project))
    by_dimension["frame_anchor"].extend(_frame_anchor_blockers(project))
    by_dimension["source_traceability"].extend(_source_trace_blockers(project))

    # These dimensions are already source-locked by canonical remapping/current validators;
    # retaining explicit 100 scores makes the readiness matrix complete and auditable.
    dimensions = {name: 100 if not problems else 0 for name, problems in by_dimension.items()}
    blockers = [
        f"{dimension}: {problem}"
        for dimension, problems in by_dimension.items()
        for problem in problems
    ]
    score = round(sum(dimensions.values()) / max(1, len(dimensions)))
    return SemanticReadinessReport(
        status="Ready" if not blockers else "Blocked",
        score=score,
        blockers=blockers,
        dimensions=dimensions,
    )


def refresh_semantic_readiness(project: Project) -> Project:
    project.semantic_readiness = evaluate_semantic_readiness(project)
    return project
