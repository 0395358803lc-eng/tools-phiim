"""Versioned migrations for persisted project JSON documents."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

CURRENT_PROJECT_SCHEMA_VERSION = 4


def migrate_project_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a migrated copy of a persisted project payload."""
    migrated = deepcopy(payload)
    version = int(migrated.get("schema_version", 1))
    if version > CURRENT_PROJECT_SCHEMA_VERSION:
        raise ValueError(
            f"Project schema {version} is newer than supported schema "
            f"{CURRENT_PROJECT_SCHEMA_VERSION}"
        )
    if version < 1:
        raise ValueError(f"Unsupported project schema version: {version}")

    if version == 1:
        for scene in migrated.get("scenes", []):
            scene.setdefault("ai_locked", False)
            scene.setdefault("ai_lock_reason", "")
        migrated["schema_version"] = 2
        version = 2

    if version == 2:
        migrated.setdefault("visual_bible", {"version": 1, "references": []})
        for scene in migrated.get("scenes", []):
            scene.setdefault(
                "visual_plan",
                {
                    "dependency_mode": "canonical",
                    "anchor_scene_id": "",
                    "character_reference_ids": [],
                    "location_reference_id": "",
                    "prop_reference_ids": [],
                    "lock_prompt": "",
                },
            )
        migrated["schema_version"] = 3
        version = 3

    if version == 3:
        visual_bible = migrated.setdefault("visual_bible", {"version": 1, "references": []})
        for reference in visual_bible.get("references", []):
            approved = str(reference.get("approved_reference") or "")
            reference.setdefault("status", "approved" if approved else "missing")
            reference.setdefault("source_scene_id", "")
        for scene in migrated.get("scenes", []):
            scene.setdefault("visual_qc", {"status": "Pending"})
            scene.setdefault("continuity_qc", {"status": "NotApplicable"})
            scene.setdefault("acceptance", {"status": "Pending"})
            if scene.get("status") == "Completed":
                scene["status"] = "Waiting"
        migrated["schema_version"] = 4
        version = 4

    if version != CURRENT_PROJECT_SCHEMA_VERSION:
        raise ValueError(f"Unable to migrate project schema version: {version}")
    return migrated
