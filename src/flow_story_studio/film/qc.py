"""Film-level hard gates that run before final video merge."""
from __future__ import annotations

from collections import Counter

from ..models import Project
from .bridge import build_canonical_film_model
from .contracts import stable_hash
from .validation import validate_project_hard_constraints


def canonical_dialogue_multiset(project: Project) -> Counter[str]:
    expected: Counter[str] = Counter()
    model = project.film_model or {}
    bible = model.get("audio_bible", {}) if isinstance(model, dict) else {}
    locks = bible.get("dialogue_locks", {}) if isinstance(bible, dict) else {}
    if isinstance(locks, dict):
        for value in locks.values():
            expected[str(value)] += 1
    return expected


def project_dialogue_multiset(project: Project) -> Counter[str]:
    actual: Counter[str] = Counter()
    for scene in project.scenes:
        for item in scene.dialogues:
            actual[f"{item.character_id}|{item.delivery}|{item.text}"] += 1
    return actual


def film_hard_blockers(project: Project) -> list[str]:
    blockers: list[str] = []
    verdict = validate_project_hard_constraints(project)
    blockers.extend(verdict.errors)

    current_model = build_canonical_film_model(project).model_dump(mode="json")
    current_model_hash = stable_hash(current_model)
    if not project.film_model_hash:
        blockers.append("canonical film model hash is missing")
    elif project.film_model_hash != current_model_hash:
        blockers.append("canonical film model hash is stale")

    expected = canonical_dialogue_multiset(project)
    actual = project_dialogue_multiset(project)
    if expected and expected != actual:
        missing = expected - actual
        unexpected = actual - expected
        if missing:
            blockers.append(
                "canonical dialogue missing: "
                + ", ".join(f"{key} x{count}" for key, count in sorted(missing.items()))
            )
        if unexpected:
            blockers.append(
                "unexpected/paraphrased dialogue: "
                + ", ".join(f"{key} x{count}" for key, count in sorted(unexpected.items()))
            )

    for scene in project.scenes:
        if not scene.render_contract_hash:
            blockers.append(f"{scene.id}: immutable render contract is missing")
        elif not scene.render_contract:
            blockers.append(f"{scene.id}: render contract payload is missing")
        elif stable_hash(scene.render_contract) != scene.render_contract_hash:
            blockers.append(f"{scene.id}: immutable render contract hash is stale")
        if scene.render_contract:
            if scene.render_contract.get("scene_id") != scene.id:
                blockers.append(f"{scene.id}: render contract scene identity mismatch")
            if scene.render_contract.get("required_action") != scene.action:
                blockers.append(f"{scene.id}: render contract action is stale")
            expected_dialogue = [
                item.model_dump(mode="json") for item in scene.dialogues
            ]
            if scene.render_contract.get("required_dialogue") != expected_dialogue:
                blockers.append(f"{scene.id}: render contract dialogue is stale")
    return blockers


def film_hard_gate_passes(project: Project) -> bool:
    return not film_hard_blockers(project)
