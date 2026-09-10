#!/usr/bin/env python3
"""Deterministic multi-screenplay Analysis acceptance suite."""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from flow_story_studio.engines.analyzer import analyze_story  # noqa: E402
from flow_story_studio.models import AnalyzeRequest, Project  # noqa: E402


def key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value).casefold().replace("đ", "d"))
    ascii_text = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return " ".join(re.findall(r"[a-z0-9]+", ascii_text))


def entity_id(project: Project, name: str) -> str:
    wanted = key(name)
    matches = [
        item.id
        for item in [*project.characters, *project.props, *project.locations]
        if key(item.name) == wanted
    ]
    if len(matches) != 1:
        raise AssertionError(f"entity {name!r}: expected exactly one match, got {matches}")
    return matches[0]


def character_id(project: Project, name: str) -> str:
    wanted = key(name)
    matches = [item.id for item in project.characters if key(item.name) == wanted]
    if len(matches) != 1:
        raise AssertionError(f"character {name!r}: expected exactly one match, got {matches}")
    return matches[0]


def prop_id(project: Project, name: str) -> str:
    wanted = key(name)
    matches = [item.id for item in project.props if key(item.name) == wanted]
    if len(matches) != 1:
        raise AssertionError(f"prop {name!r}: expected exactly one match, got {matches}")
    return matches[0]


def scene(project: Project, number: int):
    try:
        return next(item for item in project.scenes if item.order == number)
    except StopIteration as exc:
        raise AssertionError(f"scene {number}: not found") from exc


def assert_project_invariants(project: Project, case: dict[str, Any]) -> None:
    if "scene_count" in case:
        assert len(project.scenes) == case["scene_count"], (
            f"scene_count expected {case['scene_count']}, got {len(project.scenes)}"
        )
    if "min_scene_count" in case:
        assert len(project.scenes) >= case["min_scene_count"], (
            f"scene_count expected >= {case['min_scene_count']}, got {len(project.scenes)}"
        )
    if "duration" in case:
        actual = sum(item.duration for item in project.scenes)
        assert actual == case["duration"], f"duration expected {case['duration']}, got {actual}"
    if "unique_render_prompts" in case:
        actual = len({item.render_prompt for item in project.scenes})
        assert actual == case["unique_render_prompts"], (
            f"unique render prompts expected {case['unique_render_prompts']}, got {actual}"
        )
    if "unique_visual_prompts" in case:
        actual = len({item.visual_prompt for item in project.scenes})
        assert actual == case["unique_visual_prompts"], (
            f"unique visual prompts expected {case['unique_visual_prompts']}, got {actual}"
        )

    assert project.continuity_score == 100, f"continuity_score={project.continuity_score}"
    assert project.semantic_readiness.status == "Ready", (
        f"semantic readiness={project.semantic_readiness.status}: "
        f"{project.semantic_readiness.blockers}"
    )
    assert project.semantic_readiness.score == 100, (
        f"semantic readiness score={project.semantic_readiness.score}"
    )
    hard_gate = project.film_model.get("hard_gate") or {}
    assert hard_gate.get("passed") is True, f"hard gate failed: {hard_gate}"
    analysis_gate = project.film_model.get("analysis_gate") or {}
    assert analysis_gate.get("passed") is True, f"analysis gate failed: {analysis_gate}"
    assert analysis_gate.get("scene_count") == len(project.scenes)
    assert all(
        bool((item.orchestration.get("analysis_gate") or {}).get("passed"))
        for item in project.scenes
    ), "one or more per-scene analysis gates failed"
    assert all(item.ai_locked for item in project.scenes), "one or more scenes are not AI locked"


def check(project: Project, spec: dict[str, Any]) -> None:
    kind = spec["type"]
    current = scene(project, spec["scene"]) if "scene" in spec else None

    if kind == "dependency":
        assert current.visual_plan.dependency_mode == spec["value"], (
            f"scene {spec['scene']} dependency={current.visual_plan.dependency_mode}, "
            f"expected {spec['value']}"
        )
        return
    if kind == "transition":
        assert current.semantic_truth.narrative_transition == spec["value"], (
            f"scene {spec['scene']} transition={current.semantic_truth.narrative_transition}, "
            f"expected {spec['value']}"
        )
        return
    if kind == "orchestration_transition":
        actual = current.orchestration.get("transition_mode")
        assert actual == spec["value"], (
            f"scene {spec['scene']} orchestration transition={actual}, expected {spec['value']}"
        )
        return
    if kind in {"character_present", "character_absent"}:
        cid = character_id(project, spec["name"])
        present = cid in current.characters
        assert present is (kind == "character_present"), (
            f"scene {spec['scene']} character {spec['name']} presence={present}"
        )
        return
    if kind == "dialogue_delivery":
        cid = character_id(project, spec["character"])
        deliveries = [
            item.delivery for item in current.dialogues if item.character_id == cid
        ]
        assert spec["delivery"] in deliveries, (
            f"scene {spec['scene']} {spec['character']} deliveries={deliveries}, "
            f"expected {spec['delivery']}"
        )
        return
    if kind == "temporal":
        temporal = current.semantic_truth.temporal
        for field in ("timeline_branch", "daypart", "scene_clock"):
            if field in spec:
                actual = getattr(temporal, field)
                assert actual == spec[field], (
                    f"scene {spec['scene']} temporal.{field}={actual!r}, expected {spec[field]!r}"
                )
        return
    if kind == "prop_exists":
        prop_id(project, spec["prop"])
        return
    if kind == "prop_absent":
        pid = prop_id(project, spec["prop"])
        truth = current.semantic_truth
        assert pid not in truth.entry_props and pid not in truth.exit_props, (
            f"scene {spec['scene']} prop {spec['prop']} unexpectedly physical"
        )
        return
    if kind == "prop_state":
        pid = prop_id(project, spec["prop"])
        states = (
            current.semantic_truth.entry_props
            if spec["phase"] == "entry"
            else current.semantic_truth.exit_props
        )
        assert pid in states, (
            f"scene {spec['scene']} missing {spec['phase']} state for {spec['prop']}"
        )
        state = states[pid]
        comparisons = {
            "owner": "owner_id",
            "visibility": "visibility",
            "container": "container",
            "condition": "condition",
            "piece_count": "piece_count",
            "scope": "scope",
        }
        for expected_name, attr in comparisons.items():
            if expected_name not in spec:
                continue
            expected = spec[expected_name]
            if expected_name == "owner":
                expected = character_id(project, expected)
            actual = getattr(state, attr)
            assert actual == expected, (
                f"scene {spec['scene']} {spec['phase']} {spec['prop']} "
                f"{attr}={actual!r}, expected {expected!r}"
            )
        return
    if kind in {"prop_event", "prop_event_anywhere"}:
        pid = prop_id(project, spec["prop"])
        candidates = (
            current.semantic_truth.prop_events
            if kind == "prop_event"
            else [
                event
                for item in project.scenes
                for event in item.semantic_truth.prop_events
            ]
        )
        candidates = [
            item
            for item in candidates
            if item.entity_id == pid and item.action == spec["action"]
        ]
        for owner_field, attr in (
            ("source_owner", "source_owner_id"),
            ("target_owner", "target_owner_id"),
        ):
            if owner_field in spec:
                wanted = character_id(project, spec[owner_field])
                candidates = [item for item in candidates if getattr(item, attr) == wanted]
        if "target_condition" in spec:
            candidates = [
                item for item in candidates if item.target_condition == spec["target_condition"]
            ]
        assert candidates, f"missing matching {spec['action']} event for {spec['prop']}"
        return
    if kind == "part_present":
        pid = prop_id(project, spec["prop"])
        parts = (
            current.semantic_truth.entry_part_instances
            if spec["phase"] == "entry"
            else current.semantic_truth.exit_part_instances
        )
        assert any(
            state.entity_id == pid and state.part == spec["part"] and state.present
            for state in parts.values()
        ), f"scene {spec['scene']} missing part {spec['part']} for {spec['prop']}"
        return
    if kind == "perceptual_scope":
        eid = entity_id(project, spec["entity"])
        actual = current.semantic_truth.perceptual_entities.get(eid)
        assert actual == spec["scope"], (
            f"scene {spec['scene']} perceptual scope {spec['entity']}={actual}, "
            f"expected {spec['scope']}"
        )
        return
    if kind in {"wardrobe_contains", "wardrobe_excludes"}:
        cid = character_id(project, spec["character"])
        state = current.start_state if spec["phase"] == "start" else current.end_state
        wardrobe = key(state.character_wardrobe.get(cid, ""))
        needle = key(spec["text"])
        contains = needle in wardrobe
        assert contains is (kind == "wardrobe_contains"), (
            f"scene {spec['scene']} {spec['phase']} wardrobe={wardrobe!r}, "
            f"expected {'to contain' if kind == 'wardrobe_contains' else 'to exclude'} "
            f"{needle!r}"
        )
        return
    if kind == "all_scene_gates_pass":
        assert all(
            bool((item.orchestration.get("analysis_gate") or {}).get("passed"))
            for item in project.scenes
        )
        return
    if kind == "location_count":
        assert len(project.locations) == spec["value"], (
            f"location_count={len(project.locations)}, expected {spec['value']}"
        )
        return

    raise AssertionError(f"unknown check type: {kind}")


def run_case(corpus_root: Path, case: dict[str, Any]) -> dict[str, Any]:
    path = corpus_root / case["file"]
    failures: list[str] = []
    project: Project | None = None
    try:
        project = analyze_story(
            AnalyzeRequest(name=path.stem, original_text=path.read_text(encoding="utf-8"))
        )
        assert_project_invariants(project, case)
        for spec in case.get("checks", []):
            check(project, spec)
    except Exception as exc:
        failures.append(f"{type(exc).__name__}: {exc}")

    return {
        "file": case["file"],
        "passed": not failures,
        "failures": failures,
        "scene_count": len(project.scenes) if project else 0,
        "duration": sum(item.duration for item in project.scenes) if project else 0,
        "continuity_score": project.continuity_score if project else 0,
        "semantic_readiness": project.semantic_readiness.status if project else "ERROR",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--manifest",
        type=Path,
        default=ROOT / "acceptance" / "corpus" / "manifest.json",
    )
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    corpus_root = args.manifest.parent
    selected = set(args.case)
    cases = [
        item for item in manifest["cases"]
        if not selected or item["file"] in selected or Path(item["file"]).stem in selected
    ]

    results = [run_case(corpus_root, item) for item in cases]
    passed = sum(1 for item in results if item["passed"])
    summary = {
        "passed": passed == len(results),
        "case_count": len(results),
        "passed_count": passed,
        "failed_count": len(results) - passed,
        "results": results,
    }

    if args.as_json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        for item in results:
            mark = "PASS" if item["passed"] else "FAIL"
            print(
                f"{mark:4} {item['file']} "
                f"scenes={item['scene_count']} duration={item['duration']} "
                f"continuity={item['continuity_score']} "
                f"readiness={item['semantic_readiness']}"
            )
            for failure in item["failures"]:
                print(f"     - {failure}")
        print(f"\n{passed}/{len(results)} corpus cases passed")

    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
