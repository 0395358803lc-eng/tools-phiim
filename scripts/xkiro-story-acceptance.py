# ruff: noqa: I001
"""Run a live xKiro screenplay acceptance audit against source-truth invariants."""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import os
import pathlib
import re
import typing
import unicodedata

from flow_story_studio.analysis_providers.xkiro import XKiroClient
from flow_story_studio.models import AnalyzeRequest, Project, VideoSettings
from flow_story_studio.scene_contracts import verify_scene_contract


TERM_ALTERNATIVES: dict[str, tuple[str, ...]] = {
    "realistic cinematic": ("realistic cinematic", "cinematic realism"),
    "ánh sáng tự nhiên": ("ánh sáng tự nhiên", "natural light", "natural lighting"),
    "màu lạnh": ("màu lạnh", "cold palette", "cool palette", "cool tones", "cold tones"),
    "ấm hơn": ("ấm hơn", "warm", "warmer", "warm tones"),
    "hồi tưởng": ("hồi tưởng", "flashback"),
    "sơ mi xám đậm": ("sơ mi xám đậm", "dark gray shirt", "dark grey shirt"),
    "áo khoác đen": ("áo khoác đen", "black jacket", "black coat"),
    "quần tối màu": ("quần tối màu", "dark pants", "dark trousers"),
    "dây thép": ("dây thép", "steel band", "steel strap"),
    "áo len xanh rêu": ("áo len xanh rêu", "moss green sweater", "olive green sweater"),
    "áo khoác kem": ("áo khoác kem", "cream coat", "cream jacket"),
    "tóc đen ngang vai": (
        "tóc đen ngang vai",
        "shoulder length black hair",
        "shoulder-length black hair",
    ),
    "nhân viên nhà ga": ("nhân viên nhà ga", "station employee", "station staff"),
    "áo sơ mi xanh nhạt": ("áo sơ mi xanh nhạt", "light blue shirt"),
    "áo khoác đồng phục sẫm màu": (
        "áo khoác đồng phục sẫm màu",
        "dark uniform jacket",
        "dark uniform coat",
    ),
    "xanh nhạt": ("xanh nhạt", "light blue", "pale blue"),
    "góc phải": ("góc phải", "right corner"),
    "rách": ("rách", "torn", "tear"),
    "mặt tròn": ("mặt tròn", "round face", "round dial"),
    "đen": ("đen", "black"),
    "bạc": ("bạc", "silver"),
    "led đỏ": ("led đỏ", "red led"),
    "vàng": ("vàng", "yellow"),
    "cán gỗ cong": ("cán gỗ cong", "curved wooden handle"),
    "tiếng xé giấy": ("tiếng xé giấy", "paper tearing", "sound of paper tearing"),
    "xé chiếc vé làm đôi": (
        "xé chiếc vé làm đôi",
        "tear the ticket in half",
        "tears the ticket in half",
        "tears it cleanly in two",
    ),
    "góc phải của chiếc vé xanh": (
        "góc phải của chiếc vé xanh",
        "top right corner of the blue ticket",
        "right corner of the blue ticket",
    ),
    "lần này anh nhớ đúng thứ tự rồi": ("lần này anh nhớ đúng thứ tự rồi",),
}


def fold(value: object) -> str:
    raw = str(value).casefold().replace("đ", "d")
    normalized = unicodedata.normalize("NFKD", raw)
    ascii_text = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return " ".join(re.findall(r"[a-z0-9]+", ascii_text))


def contains_term(text: str, term: str) -> bool:
    haystack = fold(text)
    alternatives = TERM_ALTERNATIVES.get(term.casefold(), (term,))
    return any(fold(candidate) in haystack for candidate in alternatives)


def scene_number(source_text: str) -> int | None:
    match = re.search(r"\b(?:canh|scene)\s+(\d{1,3})\b", fold(source_text))
    return int(match.group(1)) if match else None


def select_minimax_27_free(models: list[typing.Any]) -> typing.Any:
    exact_id = "minimax/minimax-m2.7:free"
    exact = next((model for model in models if model.id == exact_id), None)
    if exact is not None:
        if exact.access_tier != "free":
            raise RuntimeError(
                f"{exact_id} exists but access_tier={exact.access_tier!r}, expected 'free'"
            )
        return exact

    available = [
        f"{model.id} | {model.display_name} | {model.access_tier}"
        for model in models
        if model.access_tier == "free"
    ]
    raise RuntimeError(
        f"Không tìm thấy model bắt buộc {exact_id} trong catalog xKiro. "
        "Free models: " + "; ".join(available[:50])
    )


def find_character(project: Project, expected_name: str) -> typing.Any | None:
    target = fold(expected_name)
    for character in project.characters:
        if fold(character.name) == target:
            return character
    return None


def find_prop(project: Project, aliases: list[str]) -> typing.Any | None:
    alias_keys = [fold(value) for value in aliases]
    for prop in project.props:
        name = fold(prop.name)
        if any(alias in name or name in alias for alias in alias_keys):
            return prop
    return None


def reference_text(project: Project, entity_id: str) -> str:
    for reference in project.visual_bible.references:
        if reference.entity_id == entity_id:
            return " ".join(
                [
                    reference.name,
                    reference.lock_text,
                    " ".join(reference.reference_images),
                ]
            )
    return ""


def model_text(model: typing.Any) -> str:
    values = model.model_dump()
    parts: list[str] = []
    for value in values.values():
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, list):
            parts.extend(str(item) for item in value)
        elif isinstance(value, dict):
            parts.extend(str(item) for item in value.values())
    return " ".join(parts)


def output_text(scenes: list[typing.Any]) -> str:
    parts: list[str] = []
    for scene in scenes:
        parts.extend(
            [
                scene.title,
                scene.summary,
                scene.action,
                scene.camera,
                scene.lighting,
                scene.atmosphere,
                scene.visual_prompt,
                scene.flow_prompt,
                scene.start_state.time,
                scene.start_state.weather,
                scene.start_state.notes,
                scene.end_state.time,
                scene.end_state.weather,
                scene.end_state.notes,
            ]
        )
    return " ".join(parts)


def canonical_audio(project: Project, scenes: list[typing.Any]) -> list[tuple[str, str]]:
    by_id = {item.id: item.name for item in project.characters}
    events: list[tuple[str, str]] = []
    for scene in scenes:
        for dialogue in scene.dialogues:
            events.append((by_id.get(dialogue.character_id, dialogue.character_id), dialogue.text))
    return events


def canonical_audio_delivery(
    project: Project,
    scenes: list[typing.Any],
) -> list[tuple[str, str, str]]:
    by_id = {item.id: item.name for item in project.characters}
    events: list[tuple[str, str, str]] = []
    for scene in scenes:
        for dialogue in scene.dialogues:
            events.append(
                (
                    by_id.get(dialogue.character_id, dialogue.character_id),
                    dialogue.text,
                    dialogue.delivery,
                )
            )
    return events


def prop_ids_for_scene(scene: typing.Any) -> set[str]:
    values = set(scene.start_state.prop_positions) | set(scene.end_state.prop_positions)
    values.update(
        reference_id.removeprefix("VIS-")
        for reference_id in scene.visual_plan.prop_reference_ids
    )
    return values


def mentions_character(text: object, name: str) -> bool:
    raw = str(text)
    tokens = re.findall(r"[\wÀ-ỹ]+", name.strip(), re.UNICODE)
    if len(tokens) == 1 and len(tokens[0]) <= 3:
        forms = {name.strip(), name.strip().upper(), name.strip().title()}
        pattern = r"(?<!\w)(?:" + "|".join(re.escape(item) for item in forms if item) + r")(?!\w)"
        return re.search(pattern, raw, re.UNICODE) is not None
    target = fold(name)
    haystack = fold(raw)
    return bool(target and re.search(rf"(?<![a-z0-9]){re.escape(target)}(?![a-z0-9])", haystack))


def state_claims_absent(value: object) -> bool:
    folded = fold(value)
    markers = (
        "khong co mat",
        "khong o trong khung hinh",
        "khong xuat hien",
        "not in frame",
        "not present",
        "not in scene",
        "not visible",
        "off screen",
        "offscreen",
        "voice only",
    )
    return any(marker in folded for marker in markers)


def audit_project(
    project: Project,
    manifest: dict[str, typing.Any],
    selected_model: typing.Any,
) -> dict[str, typing.Any]:
    errors: list[dict[str, typing.Any]] = []
    warnings: list[dict[str, typing.Any]] = []
    diagnostics: list[dict[str, typing.Any]] = []
    scene_audit: list[dict[str, typing.Any]] = []
    provider_fallback_scenes: set[str] = set()

    def error(code: str, message: str, scene: int | None = None) -> None:
        item: dict[str, typing.Any] = {"code": code, "message": message}
        if scene is not None:
            item["scene"] = scene
        errors.append(item)

    def warning(code: str, message: str, scene: int | None = None) -> None:
        item: dict[str, typing.Any] = {"code": code, "message": message}
        if scene is not None:
            item["scene"] = scene
        warnings.append(item)

    def diagnostic(code: str, message: str, scene: int | None = None) -> None:
        item: dict[str, typing.Any] = {"code": code, "message": message}
        if scene is not None:
            item["scene"] = scene
        diagnostics.append(item)

    if project.settings.aspect_ratio != manifest["required_aspect_ratio"]:
        error(
            "ASPECT_RATIO",
            f"Expected {manifest['required_aspect_ratio']}, got {project.settings.aspect_ratio}",
        )
    if project.settings.analysis_model != selected_model.id:
        error(
            "MODEL_ID",
            f"Project model {project.settings.analysis_model} != selected {selected_model.id}",
        )
    if selected_model.access_tier != "free":
        error("MODEL_TIER", f"Selected model tier is {selected_model.access_tier}, expected free")

    style_text = f"{project.visual_style} {project.master_prompt}"
    for term in manifest["required_style_terms"]:
        if not contains_term(style_text, term):
            error("STYLE_LOCK", f"Missing style source-truth term: {term}")

    expected_characters = manifest["characters"]
    for expected_name, rule in expected_characters.items():
        character = find_character(project, expected_name)
        if character is None:
            error("CHARACTER_MISSING", f"Missing canonical character {expected_name}")
            continue
        text = f"{model_text(character)} {reference_text(project, character.id)}"
        for term in rule["required_terms"]:
            if not contains_term(text, term):
                error(
                    "CHARACTER_LOCK",
                    f"{expected_name} is missing identity/wardrobe fact: {term}",
                )

    prop_map: dict[str, typing.Any] = {}
    for prop_key, rule in manifest["props"].items():
        prop = find_prop(project, rule["aliases"])
        if prop is None:
            error("PROP_MISSING", f"Missing canonical prop {prop_key}")
            continue
        prop_map[prop_key] = prop
        text = f"{model_text(prop)} {reference_text(project, prop.id)}"
        for term in rule["required_terms"]:
            if not contains_term(text, term):
                error("PROP_LOCK", f"{prop_key} is missing locked fact: {term}")

    groups: dict[int, list[typing.Any]] = collections.defaultdict(list)
    unnumbered: list[str] = []
    for scene in sorted(project.scenes, key=lambda item: item.order):
        number = scene_number(scene.source_text)
        if number is None:
            unnumbered.append(scene.id)
        else:
            groups[number].append(scene)

    if unnumbered:
        error(
            "SCENE_MAPPING",
            "Production scenes missing screenplay scene number: " + ", ".join(unnumbered),
        )
    expected_count = int(manifest["screenplay_scene_count"])
    missing_groups = [number for number in range(1, expected_count + 1) if not groups[number]]
    extra_groups = sorted(number for number in groups if number < 1 or number > expected_count)
    if missing_groups:
        error("SCENE_MISSING", f"Missing screenplay scene groups: {missing_groups}")
    if extra_groups:
        error("SCENE_EXTRA", f"Unexpected screenplay scene groups: {extra_groups}")

    char_name_by_id = {item.id: item.name for item in project.characters}
    prop_name_by_id = {item.id: item.name for item in project.props}

    for number in range(1, expected_count + 1):
        scenes = groups.get(number, [])
        rule = manifest["scene_visual_rules"].get(str(number), {})
        required = {fold(value) for value in rule.get("required", [])}
        allowed = {fold(value) for value in rule.get("allowed", [])}
        forbidden = {fold(value) for value in rule.get("forbidden", [])}
        aggregate_visible: set[str] = set()
        shot_visible: list[list[str]] = []
        referenced_prop_ids: set[str] = set()

        for scene in scenes:
            names = [char_name_by_id.get(item, item) for item in scene.characters]
            shot_visible.append(names)
            folded_names = {fold(name) for name in names}
            aggregate_visible.update(folded_names)
            referenced_prop_ids.update(prop_ids_for_scene(scene))
            disallowed = folded_names - allowed
            if disallowed:
                error(
                    "VISUAL_CAST_EXTRA",
                    f"Disallowed visible cast in shot {scene.id}: {sorted(disallowed)}",
                    number,
                )
            bad_forbidden = folded_names & forbidden
            if bad_forbidden:
                error(
                    "VISUAL_CAST_FORBIDDEN",
                    f"Forbidden/offscreen cast became visible: {sorted(bad_forbidden)}",
                    number,
                )
            if not scene.ai_locked:
                error("AI_LOCK", f"Scene {scene.id} is not AI continuity locked", number)
            if not verify_scene_contract(scene):
                error(
                    "SCENE_CONTRACT",
                    f"{scene.id} Scene Packet contract hash is missing or stale",
                    number,
                )
            for item in scene.warnings:
                if "response incomplete; retained deterministic source-truth scene data" in item:
                    provider_fallback_scenes.add(scene.id)
                    diagnostic("XKIRO_SOURCE_TRUTH_FALLBACK", f"{scene.id}: {item}", number)
                elif (
                    "camera conflicted with source-grounded visual cast" in item
                    and "sanitized to match visible cast" in item
                ):
                    diagnostic("RESOLVED_CAMERA_SANITIZE", f"{scene.id}: {item}", number)
                else:
                    warning("SCENE_WARNING", f"{scene.id}: {item}", number)

            visible_ids = set(scene.characters)
            for state_name, state in (
                ("start", scene.start_state),
                ("end", scene.end_state),
            ):
                missing_positions = visible_ids - set(state.character_positions)
                missing_wardrobe = visible_ids - set(state.character_wardrobe)
                if missing_positions:
                    error(
                        "STATE_CHARACTER_POSITION_MISSING",
                        f"{scene.id} {state_name} state missing positions: "
                        f"{sorted(missing_positions)}",
                        number,
                    )
                if missing_wardrobe:
                    error(
                        "STATE_CHARACTER_WARDROBE_MISSING",
                        f"{scene.id} {state_name} state missing wardrobe: "
                        f"{sorted(missing_wardrobe)}",
                        number,
                    )
                for character_id in visible_ids:
                    position = state.character_positions.get(character_id, "")
                    wardrobe = state.character_wardrobe.get(character_id, "")
                    if state_claims_absent(position) or state_claims_absent(wardrobe):
                        error(
                            "STATE_VISIBLE_CHARACTER_ABSENT",
                            f"{scene.id} {state_name} state says visible character "
                            f"{character_id} is absent",
                            number,
                        )
                state_text = " ".join(
                    [
                        state.camera,
                        state.notes,
                        *state.character_positions.values(),
                        *state.character_wardrobe.values(),
                    ]
                )
                for forbidden_name in rule.get("forbidden", []):
                    if mentions_character(state_text, forbidden_name):
                        error(
                            "STATE_FORBIDDEN_CHARACTER_LEAK",
                            f"{scene.id} {state_name} state leaks forbidden character "
                            f"{forbidden_name}",
                            number,
                        )

            dependency_mode = scene.visual_plan.dependency_mode
            has_direct_claim = "Direct continuation of" in scene.flow_prompt
            has_canonical_claim = "Canonical cut/new beat" in scene.flow_prompt
            if dependency_mode == "direct" and not has_direct_claim:
                error(
                    "FLOW_DEPENDENCY_PROMPT",
                    f"{scene.id} is direct but Flow prompt lacks direct-continuation lock",
                    number,
                )
            if dependency_mode != "direct" and has_direct_claim:
                error(
                    "FLOW_DEPENDENCY_PROMPT",
                    f"{scene.id} is {dependency_mode} but Flow prompt claims direct continuation",
                    number,
                )
            if dependency_mode == "canonical" and not has_canonical_claim:
                error(
                    "FLOW_DEPENDENCY_PROMPT",
                    f"{scene.id} canonical cut lacks explicit canonical reset wording",
                    number,
                )

        missing_required = required - aggregate_visible
        if missing_required:
            error(
                "VISUAL_CAST_MISSING",
                f"Required visual cast absent: {sorted(missing_required)}",
                number,
            )

        for prop_key in rule.get("required_props", []):
            prop = prop_map.get(prop_key)
            if prop is None:
                continue
            generated = output_text(scenes)
            if prop.id not in referenced_prop_ids:
                aliases = manifest["props"][prop_key]["aliases"]
                if not any(contains_term(generated, alias) for alias in aliases):
                    error(
                        "PROP_SCENE_MISSING",
                        f"Required prop {prop_key} is not carried into generated scene data",
                        number,
                    )

        for prop_key in rule.get("forbidden_props", []):
            prop = prop_map.get(prop_key)
            if prop is not None and prop.id in referenced_prop_ids:
                error(
                    "PROP_SCENE_FORBIDDEN",
                    f"Explicitly absent prop {prop_key} is still referenced visually",
                    number,
                )

        actual_audio = canonical_audio(project, scenes)
        expected_audio = [
            (str(speaker), str(text))
            for speaker, text in manifest["audio"].get(str(number), [])
        ]
        actual_counter = collections.Counter(
            (fold(speaker), fold(text)) for speaker, text in actual_audio
        )
        expected_counter = collections.Counter(
            (fold(speaker), fold(text)) for speaker, text in expected_audio
        )
        if actual_counter != expected_counter:
            error(
                "AUDIO_SOURCE_TRUTH",
                f"Audio mismatch. Expected={expected_audio!r}, actual={actual_audio!r}",
                number,
            )
        actual_delivery = canonical_audio_delivery(project, scenes)
        expected_delivery = [
            (str(speaker), str(text), str(delivery))
            for speaker, text, delivery in manifest.get("audio_delivery", {}).get(
                str(number), []
            )
        ]
        actual_delivery_counter = collections.Counter(
            (fold(speaker), fold(text), delivery.casefold())
            for speaker, text, delivery in actual_delivery
        )
        expected_delivery_counter = collections.Counter(
            (fold(speaker), fold(text), delivery.casefold())
            for speaker, text, delivery in expected_delivery
        )
        if actual_delivery_counter != expected_delivery_counter:
            error(
                "AUDIO_DELIVERY_SOURCE_TRUTH",
                "Audio delivery mismatch. "
                f"Expected={expected_delivery!r}, actual={actual_delivery!r}",
                number,
            )
        for scene in scenes:
            if scene.voiceover.strip():
                error(
                    "AUDIO_VOICEOVER",
                    f"Unexpected narration/voiceover in {scene.id}: {scene.voiceover!r}",
                    number,
                )

        scene_audit.append(
            {
                "screenplay_scene": number,
                "production_shots": [scene.id for scene in scenes],
                "visible_cast_by_shot": shot_visible,
                "audio": actual_audio,
                "audio_delivery": actual_delivery,
                "prop_references": [
                    prop_name_by_id.get(item, item) for item in sorted(referenced_prop_ids)
                ],
                "dependency_modes": [scene.visual_plan.dependency_mode for scene in scenes],
                "contract_hashes": [scene.contract_hash for scene in scenes],
            }
        )

    timeline = manifest.get("timeline", {})
    for number in timeline.get("flashback_scenes", []):
        for scene in groups.get(int(number), []):
            if not fold(scene.start_state.time).startswith("flashback"):
                error(
                    "TIMELINE_DOMAIN",
                    f"Expected flashback time domain, got {scene.start_state.time!r}",
                    int(number),
                )
    for number in timeline.get("present_scenes", []):
        for scene in groups.get(int(number), []):
            if not fold(scene.start_state.time).startswith("present"):
                error(
                    "TIMELINE_DOMAIN",
                    f"Expected present time domain, got {scene.start_state.time!r}",
                    int(number),
                )
    for number in timeline.get("direct_scenes", []):
        scenes = groups.get(int(number), [])
        if scenes and any(scene.visual_plan.dependency_mode != "direct" for scene in scenes):
            error(
                "DIRECT_DEPENDENCY",
                "Authored direct continuation is not using dependency_mode=direct",
                int(number),
            )

    for fact in manifest.get("hard_facts", []):
        number = int(fact["scene"])
        generated = output_text(groups.get(number, []))
        for term in fact["terms"]:
            if not contains_term(generated, term):
                error(
                    "HARD_FACT",
                    f"Generated scene data does not preserve hard fact: {term}",
                    number,
                )

    warm_markers = ("ấm", "warm", "warmer", "golden")
    for number in manifest.get("flashback_scenes", []):
        generated = fold(output_text(groups.get(int(number), [])))
        if not any(fold(marker) in generated for marker in warm_markers):
            error(
                "FLASHBACK_COLOR",
                "Flashback does not carry the warmer visual treatment from source",
                int(number),
            )

    prompts = [scene.flow_prompt.strip() for scene in project.scenes if scene.flow_prompt.strip()]
    if len(prompts) != len(project.scenes):
        error("FLOW_PROMPT_BLANK", "At least one production scene has an empty Flow prompt")
    if len(prompts) != len(set(prompts)):
        error("FLOW_PROMPT_DUPLICATE", "Duplicate Flow prompts remain after finalization")
    for scene in project.scenes:
        if scene.source_text.strip() not in scene.flow_prompt:
            error(
                "SOURCE_BEAT_NOT_LOCKED",
                f"{scene.id} Flow prompt does not contain the verbatim screenplay source beat",
            )

    if project.continuity_score != 100:
        error("CONTINUITY_SCORE", f"Continuity score is {project.continuity_score}, expected 100")
    for item in project.continuity_warnings:
        if "response incomplete; retained deterministic source-truth scene data" in item:
            match = re.match(r"(SCENE_\d+):", item)
            if match:
                provider_fallback_scenes.add(match.group(1))
        elif (
            "camera conflicted with source-grounded visual cast" in item
            and "sanitized to match visible cast" in item
        ):
            diagnostic("RESOLVED_CAMERA_SANITIZE", item)
        else:
            warning("CONTINUITY_WARNING", item)

    strict_pass = not errors and not warnings
    return {
        "scope": "pre-render analysis acceptance",
        "strict_100_percent_pass": strict_pass,
        "verdict": "ACCEPTED" if strict_pass else "REJECTED",
        "model": {
            "id": selected_model.id,
            "display_name": selected_model.display_name,
            "access_tier": selected_model.access_tier,
        },
        "project": {
            "id": project.id,
            "production_scene_count": len(project.scenes),
            "screenplay_scene_groups": len(groups),
            "continuity_score": project.continuity_score,
            "continuity_warnings": project.continuity_warnings,
        },
        "provider": {
            "fallback_scene_ids": sorted(provider_fallback_scenes),
            "fallback_scene_count": len(provider_fallback_scenes),
            "direct_ai_enrichment_complete": not provider_fallback_scenes,
        },
        "errors": errors,
        "warnings": warnings,
        "diagnostics": diagnostics,
        "scene_audit": scene_audit,
        "note": (
            "This verdict covers final analysis/source-truth readiness before rendering. "
            "Provider fallback scenes are reported separately and do not fail "
            "production acceptance "
            "when the deterministic final state passes every source-truth gate. Rendered pixels, "
            "voices, timing and final MP4 continuity still require live video QC."
        ),
    }


async def run(args: argparse.Namespace) -> int:
    script = args.script.read_text(encoding="utf-8")
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    if not os.getenv("XKIRO_API_KEY", "").strip():
        raise RuntimeError("GitHub Actions secret XKIRO_API_KEY is not configured")

    checkpoint_root = args.output.parent / "checkpoints"
    client = XKiroClient(checkpoint_root=checkpoint_root)
    models = await client.list_models(free_only=False, refresh=True)
    selected = select_minimax_27_free(models)

    progress: list[dict[str, str]] = []

    def on_progress(message: str, level: str = "info") -> None:
        progress.append({"level": level, "message": message})
        print(f"[{level}] {message}", flush=True)

    request = AnalyzeRequest(
        name="Acceptance — Chiếc Vé Không Có Chuyến Tàu",
        original_text=script,
        settings=VideoSettings(
            aspect_ratio="16:9",
            resolution="1080p",
            style="realistic cinematic",
            custom_style=(
                "Ánh sáng tự nhiên; màu lạnh ở hiện tại; "
                "ấm hơn nhẹ ở mọi cảnh hồi tưởng."
            ),
            scene_duration=8,
            character_lock=True,
            location_lock=True,
            auto_continuity=True,
            quality_threshold=85,
            provider="mock",
            analysis_provider="xkiro",
            analysis_model=selected.id,
        ),
    )

    project = await client.analyze(request, progress=on_progress)
    args.project_output.parent.mkdir(parents=True, exist_ok=True)
    args.project_output.write_text(
        project.model_dump_json(indent=2),
        encoding="utf-8",
    )

    report = audit_project(project, manifest, selected)
    report["progress"] = progress
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0 if report["strict_100_percent_pass"] else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--script", type=pathlib.Path, required=True)
    parser.add_argument("--manifest", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--project-output", type=pathlib.Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        return asyncio.run(run(args))
    except Exception as exc:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        failure = {
            "scope": "pre-render analysis acceptance",
            "strict_100_percent_pass": False,
            "verdict": "ERROR",
            "errors": [{"code": type(exc).__name__, "message": str(exc)}],
        }
        args.output.write_text(
            json.dumps(failure, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(json.dumps(failure, ensure_ascii=False, indent=2), flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
