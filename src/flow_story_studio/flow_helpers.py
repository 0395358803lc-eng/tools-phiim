"""Pure helpers for Google Flow integration."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .models import Scene


def flow_prompt(scene: Scene) -> str:
    prompt = scene.flow_prompt.strip()
    if len(prompt) <= 4_000:
        return prompt
    start = prompt[:3100].rstrip()
    ending = prompt[-820:].lstrip()
    return f"{start}\n\n[CRITICAL END/NEGATIVE CONSTRAINTS]\n{ending}"


def reference_path(data_root: Path, value: str) -> str | None:
    if not value or value.startswith(("http://", "https://")):
        return None
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = data_root / candidate
    try:
        resolved = candidate.resolve()
        resolved.relative_to((data_root / "references").resolve())
    except (OSError, ValueError):
        return None
    return str(resolved) if resolved.is_file() else None


def job_identifiers(job: Any) -> set[str]:
    identifiers: set[str] = set()
    for attribute in (
        "workflow_id",
        "media_id",
        "resource_name",
        "operation_name",
    ):
        value = getattr(job, attribute, None)
        if value:
            text = str(value)
            identifiers.add(text)
            identifiers.add(text.rstrip("/").rsplit("/", 1)[-1])
    try:
        raw = json.dumps(getattr(job, "raw", {}) or {})
        identifiers.update(
            re.findall(
                r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
                raw,
            )
        )
    except (TypeError, ValueError):
        pass
    return {item.lower() for item in identifiers if item}


def select_video_candidate(
    candidates: list[dict[str, str]], identifiers: set[str]
) -> dict[str, str] | None:
    unique: dict[str, dict[str, str]] = {
        item.get("src", ""): item for item in candidates if item.get("src")
    }
    items = list(unique.values())
    if not items:
        return None
    scored: list[tuple[int, dict[str, str]]] = []
    for item in items:
        candidate_ids = {
            str(item.get("tile_id", "")).lower(),
            str(item.get("media_key", "")).lower(),
        }
        score = sum(
            1
            for expected in identifiers
            for actual in candidate_ids
            if expected and actual and (expected in actual or actual in expected)
        )
        scored.append((score, item))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    if scored[0][0] > 0 and (len(scored) == 1 or scored[0][0] > scored[1][0]):
        return scored[0][1]
    return items[0] if len(items) == 1 else None
