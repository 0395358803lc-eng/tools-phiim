from __future__ import annotations

import asyncio
import json
from collections import Counter
from pathlib import Path

from platformdirs import user_data_dir

from flow_story_studio.analysis_providers.xkiro import XKiroClient, XKiroError
from flow_story_studio.models import AnalyzeRequest, VideoSettings
from flow_story_studio.service import StudioService
from flow_story_studio.storage import ProjectStorage

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
SCRIPT = DATA / "three_minute_continuity_test_v1.txt"


def unique_count(values: list[str]) -> int:
    return len({value.strip() for value in values if value and value.strip()})


def assess(project: object) -> dict[str, object]:
    scenes = project.scenes
    location_ids = {item.id for item in project.locations}
    character_ids = {item.id for item in project.characters}
    prop_ids = {item.id for item in project.props}

    bad_locations = [scene.id for scene in scenes if scene.location_id not in location_ids]
    bad_characters = [
        [scene.id, character_id]
        for scene in scenes
        for character_id in scene.characters
        if character_id not in character_ids
    ]
    bad_props: list[list[str]] = []
    for scene in scenes:
        for state in (scene.start_state, scene.end_state):
            for prop_id in state.prop_positions:
                if prop_id not in prop_ids:
                    bad_props.append([scene.id, prop_id])

    header_markers = ("TÃŠN PHIM:", "NHÃ‚N Váº¬T", "Äáº O Cá»¤")
    header_scenes = [
        scene.id
        for scene in scenes
        if any(marker in scene.source_text for marker in header_markers)
    ]
    orders = [scene.order for scene in scenes]
    expected_orders = list(range(1, len(scenes) + 1))
    duplicate_flow_prompts = [
        value
        for value, count in Counter(scene.flow_prompt for scene in scenes).items()
        if value and count > 1
    ]

    return {
        "project_id": project.id,
        "scene_count": len(scenes),
        "total_duration_sec": sum(scene.duration for scene in scenes),
        "all_ai_locked": all(scene.ai_locked for scene in scenes),
        "order_contiguous": orders == expected_orders,
        "unique_summary": unique_count([scene.summary for scene in scenes]),
        "unique_action": unique_count([scene.action for scene in scenes]),
        "unique_camera": unique_count([scene.camera for scene in scenes]),
        "unique_flow_prompt": unique_count([scene.flow_prompt for scene in scenes]),
        "duplicate_flow_prompt_count": len(duplicate_flow_prompts),
        "bad_location_refs": bad_locations,
        "bad_character_refs": bad_characters,
        "bad_prop_refs": bad_props,
        "header_scenes": header_scenes,
        "characters": [[item.id, item.name] for item in project.characters],
        "locations": [[item.id, item.name] for item in project.locations],
        "props": [[item.id, item.name] for item in project.props],
        "location_sequence": [scene.location_id for scene in scenes],
    }


async def main() -> int:
    secret_root = Path(user_data_dir("TH Media", "TH Media")) / "secrets"
    legacy_root = Path(user_data_dir("Flow Story Studio", "Flow Story Studio")) / "secrets"
    credential_root = (
        secret_root if secret_root.exists() or not legacy_root.exists() else legacy_root
    )
    client = XKiroClient(credential_path=credential_root / "xkiro-api-key.bin")
    client.set_checkpoint_root(DATA / "analysis-checkpoints")
    if not client.configured:
        print(json.dumps({"ok": False, "reason": "XKIRO_NOT_CONFIGURED"}, ensure_ascii=False))
        return 2

    models = await client.list_models(free_only=False, refresh=True)
    free_models = [
        item for item in models if item.access_tier == "free" or item.id.endswith(":free")
    ]
    preferred = next(
        (item for item in models if item.id == "minimax/minimax-m2.7-highspeed:free"),
        None,
    )
    selected_model = (
        preferred.id
        if preferred is not None
        else ((free_models or models)[0].id if (free_models or models) else "")
    )
    if not selected_model:
        print(json.dumps({"ok": False, "reason": "XKIRO_NO_MODELS"}, ensure_ascii=True))
        return 4

    request = AnalyzeRequest(
        name="Three-minute xKiro acceptance",
        original_text=SCRIPT.read_text(encoding="utf-8"),
        settings=VideoSettings(
            analysis_provider="xkiro",
            analysis_model=selected_model,
            provider="mock",
            scene_duration=8,
            character_lock=True,
            location_lock=True,
            auto_continuity=True,
        ),
    )
    service = StudioService(ProjectStorage(DATA / "projects"))
    try:
        project = await service.analyze_with_provider(request, client)
    except XKiroError as exc:
        print(
            json.dumps(
                {"ok": False, "reason": "XKIRO_ERROR", "message": str(exc)}, ensure_ascii=False
            )
        )
        return 3

    result = assess(project)
    result["ok"] = True
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
