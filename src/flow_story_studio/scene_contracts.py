"""Immutable Scene Packet contract hashing for render-time safety."""

from __future__ import annotations

import hashlib
import json

from .models import Project, Scene

SCENE_CONTRACT_VERSION = 1


def _contract_payload(scene: Scene) -> dict[str, object]:
    """Return only semantic/compiled fields that define one production scene."""
    return {
        "id": scene.id,
        "order": scene.order,
        "title": scene.title,
        "source_text": scene.source_text,
        "summary": scene.summary,
        "characters": scene.characters,
        "location_id": scene.location_id,
        "action": scene.action,
        "camera": scene.camera,
        "lighting": scene.lighting,
        "atmosphere": scene.atmosphere,
        "duration": scene.duration,
        "visual_prompt": scene.visual_prompt,
        "flow_prompt": scene.flow_prompt,
        "voiceover": scene.voiceover,
        "dialogues": [item.model_dump(mode="json") for item in scene.dialogues],
        "start_state": scene.start_state.model_dump(mode="json"),
        "end_state": scene.end_state.model_dump(mode="json"),
        "visual_plan": scene.visual_plan.model_dump(mode="json"),
    }


def compute_scene_contract_hash(scene: Scene) -> str:
    payload = json.dumps(
        _contract_payload(scene),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def seal_scene_contract(scene: Scene) -> str:
    scene.contract_version = SCENE_CONTRACT_VERSION
    scene.contract_hash = compute_scene_contract_hash(scene)
    return scene.contract_hash


def seal_project_contracts(project: Project) -> Project:
    for scene in project.scenes:
        seal_scene_contract(scene)
    return project


def invalidate_scene_contract(scene: Scene) -> None:
    scene.contract_hash = ""


def verify_scene_contract(scene: Scene) -> bool:
    return (
        scene.contract_version == SCENE_CONTRACT_VERSION
        and bool(scene.contract_hash)
        and scene.contract_hash == compute_scene_contract_hash(scene)
    )


def invalid_contract_scene_ids(project: Project) -> list[str]:
    return [scene.id for scene in project.scenes if not verify_scene_contract(scene)]
