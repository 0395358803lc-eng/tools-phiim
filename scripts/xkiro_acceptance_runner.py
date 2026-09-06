"""E2E acceptance runner for TH Media xKiro screenplay analysis.

Implements the automated acceptance procedure defined in the run guide:

    acceptance/Hướng_dẫn_kỹ_thuật_—_Nghiệm_thu_E2E_tự_động_với.md

The runner boots the real desktop code path (DesktopSession + WebView window),
drives the internal HTTP API, waits for the analysis job to reach a terminal
state, reads the full canonical Project, and exports a self-contained
inspection folder with manifest, hashes, reports and a secret scan.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

EXIT_OK = 0
EXIT_GENERAL = 1
EXIT_APP_STARTUP = 2
EXIT_XKIRO_UNAVAILABLE = 3
EXIT_MODEL_UNAVAILABLE = 4
EXIT_ANALYSIS_FAILED = 5
EXIT_VALIDATION_FAILED = 6
EXIT_EXPORT_FAILED = 7
EXIT_SECRET_SCAN = 8

RUN_PATTERN = "run-%Y%m%d-%H%M%S"
SUBDIRS = (
    "00-environment",
    "01-input",
    "02-analysis-job",
    "03-project",
    "04-analysis-data",
    "05-scenes",
    "06-validation",
    "07-metrics",
)

SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("x-flow-studio-session", re.compile(r"X-Flow-Studio-Session", re.I)),
    ("authorization-header", re.compile(r"Authorization\s*:", re.I)),
    ("bearer-token", re.compile(r"Bearer\s+[A-Za-z0-9._\-]{8,}")),
    ("xkiro-api-key", re.compile(r"sk-(?:xt-)?[A-Za-z0-9]{16,}")),
    ("flow-cookie", re.compile(r"(?:flow\s*-?\s*cookie|httponly)", re.I)),
    ("secrets-path", re.compile(r"data[/\\]secrets", re.I)),
]

TERM_ALTERNATIVES: dict[str, tuple[str, ...]] = {
    "realistic cinematic": ("realistic cinematic", "cinematic realism"),
    "ánh sáng tự nhiên": ("ánh sáng tự nhiên", "natural light", "natural lighting"),
    "màu lạnh": ("màu lạnh", "cold palette", "cool palette", "cold tones", "cool tones"),
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

SECRET_VALUE_KEYS = {"api_key", "cookie", "authorization", "session_token"}
API_KEY_PATTERN = re.compile(r"sk-(?:xt-)?[A-Za-z0-9]{16,}")
SECRET_HEADER_PATTERN = re.compile(r"(?:Authorization|X-Flow-Studio-Session)\s*:\s*\S+", re.I)


def utc_iso() -> str:
    return datetime.now(UTC).isoformat()


def run_timestamp() -> str:
    return time.strftime(RUN_PATTERN, time.localtime())


def fold(value: object) -> str:
    raw = str(value).casefold().replace("đ", "d")
    normalized = unicodedata.normalize("NFKD", raw)
    ascii_text = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return " ".join(re.findall(r"[a-z0-9]+", ascii_text))


def contains_term(text: str, term: str) -> bool:
    haystack = fold(text)
    alternatives = TERM_ALTERNATIVES.get(term.casefold(), (term,))
    return any(fold(candidate) in haystack for candidate in alternatives)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sanitize_payload(payload: Any) -> Any:
    if isinstance(payload, dict):
        return {
            key: ("[REDACTED]" if key in SECRET_VALUE_KEYS else sanitize_payload(value))
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [sanitize_payload(item) for item in payload]
    if isinstance(payload, str):
        return API_KEY_PATTERN.sub("[REDACTED]", SECRET_HEADER_PATTERN.sub("[REDACTED]", payload))
    return payload


def make_run(root: Path) -> Path:
    run_dir = root / run_timestamp()
    run_dir.mkdir(parents=True, exist_ok=True)
    for subdir in SUBDIRS:
        (run_dir / subdir).mkdir(parents=True, exist_ok=True)
    return run_dir


def build_manifest(output_dir: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path in sorted(output_dir.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(output_dir).as_posix()
        if relative == "manifest.json":
            continue
        entries.append(
            {
                "relative_path": relative,
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    return entries


def secret_scan(output_dir: Path) -> list[dict[str, str]]:
    hits: list[dict[str, str]] = []
    for path in sorted(output_dir.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            continue
        relative = path.relative_to(output_dir).as_posix()
        for name, pattern in SECRET_PATTERNS:
            if pattern.search(text):
                hits.append({"path": relative, "pattern": name})
    return hits


def resolve_model(models: list[Any], model_id: str) -> Any:
    for model in models:
        if str(model.get("id", "")) == model_id:
            return model
    return None


def check_model(models: list[Any], model_id: str) -> int:
    return EXIT_OK if resolve_model(models, model_id) is not None else EXIT_MODEL_UNAVAILABLE


def job_outcome(job: dict[str, Any]) -> int | None:
    status = str(job.get("status", ""))
    if status == "completed":
        project = job.get("project") or {}
        if isinstance(project, dict) and project.get("id"):
            return EXIT_OK
        return EXIT_ANALYSIS_FAILED
    if status in {"failed", "cancelled"}:
        return EXIT_ANALYSIS_FAILED
    return None


def canonical_equals(canonical: Any, stored: Any) -> dict[str, Any]:
    canonical_keys = set(canonical) if isinstance(canonical, dict) else set()
    stored_keys = set(stored) if isinstance(stored, dict) else set()
    differing = sorted(
        key for key in canonical_keys | stored_keys if canonical.get(key) != stored.get(key)
    )
    return {"equal": not differing, "differing_fields": differing}


def git_refs(repo: Path) -> dict[str, Any]:
    def run(*args: str) -> list[str]:
        try:
            result = subprocess.run(
                ["git", *args], cwd=repo, capture_output=True, text=True, timeout=15
            )
        except (OSError, subprocess.SubprocessError):
            return []
        return result.stdout.strip().splitlines() if result.returncode == 0 else []

    branch = run("rev-parse", "--abbrev-ref", "HEAD")
    commit = run("rev-parse", "HEAD")
    dirty_lines = run("status", "--porcelain")
    return {
        "branch": branch[0] if branch else "",
        "commit": commit[0] if commit else "",
        "dirty": bool(dirty_lines),
        "dirty_files": dirty_lines,
    }


def scene_id_map(project: dict[str, Any]) -> dict[str, str]:
    return {item["id"]: item["name"] for item in project.get("characters", [])}


def scene_text(scene: dict[str, Any]) -> str:
    parts = [
        scene.get("id", ""),
        str(scene.get("order", "")),
        scene.get("title", ""),
        scene.get("source_text", ""),
        scene.get("summary", ""),
        scene.get("action", ""),
        scene.get("camera", ""),
        scene.get("lighting", ""),
        scene.get("atmosphere", ""),
        scene.get("voiceover", ""),
        scene.get("visual_prompt", ""),
        scene.get("flow_prompt", ""),
    ]
    for state_key in ("start_state", "end_state"):
        state = scene.get(state_key, {})
        if isinstance(state, dict):
            parts.extend(str(value) for value in state.values())
    plan = scene.get("visual_plan", {})
    if isinstance(plan, dict):
        parts.append(plan.get("lock_prompt", ""))
    return "\n".join(str(part) for part in parts)


def prop_ids_for_scene(scene: dict[str, Any]) -> set[str]:
    prop_ids: set[str] = set()
    for state_key in ("start_state", "end_state"):
        state = scene.get(state_key, {})
        if isinstance(state, dict):
            prop_ids.update(state.get("prop_positions", {}).keys())
    plan = scene.get("visual_plan", {})
    if isinstance(plan, dict):
        prop_ids.update(plan.get("prop_reference_ids", []))
    return {value.removeprefix("VIS-") for value in prop_ids}


def export_domains(project: dict[str, Any], out_dir: Path) -> None:
    data_dir = out_dir / "04-analysis-data"
    write_json(data_dir / "story-bible.json", project.get("story_bible", {}))
    write_json(data_dir / "characters.json", project.get("characters", []))
    write_json(data_dir / "locations.json", project.get("locations", []))
    write_json(data_dir / "props.json", project.get("props", []))
    write_json(
        data_dir / "timeline.json",
        {"timeline": project.get("timeline", [])},
    )
    write_json(data_dir / "visual-bible.json", project.get("visual_bible", {}))
    write_json(data_dir / "scenes.json", project.get("scenes", []))

    film_model = project.get("film_model", {})
    write_json(data_dir / "canonical-film-model.json", film_model)
    if isinstance(film_model, dict):
        write_json(data_dir / "audio-bible.json", film_model.get("audio_bible", {}))
        write_json(data_dir / "scene-intents.json", film_model.get("scene_intents", []))
    write_text(
        data_dir / "film-model-hash.txt",
        str(project.get("film_model_hash", "")),
    )

    scenes = project.get("scenes", [])
    render_contracts = {
        str(scene.get("id", "")): {
            "render_contract_hash": scene.get("render_contract_hash", ""),
            "render_contract": scene.get("render_contract", {}),
        }
        for scene in scenes
        if isinstance(scene, dict)
    }
    accepted_states = {
        str(scene.get("id", "")): {
            "accepted_state_hash": scene.get("accepted_state_hash", ""),
            "accepted_end_state": scene.get("accepted_end_state"),
            "last_frame_file": scene.get("last_frame_file", ""),
        }
        for scene in scenes
        if isinstance(scene, dict)
    }
    write_json(data_dir / "render-contracts.json", render_contracts)
    write_json(data_dir / "accepted-states.json", accepted_states)
    write_json(
        data_dir / "continuity.json",
        {
            "continuity_score": project.get("continuity_score", 0),
            "continuity_warnings": project.get("continuity_warnings", []),
        },
    )
    write_text(data_dir / "master-prompt.txt", str(project.get("master_prompt", "")))


def export_scene_files(scenes: list[dict[str, Any]], out_dir: Path) -> int:
    scenes_dir = out_dir / "05-scenes"
    count = 0
    for index, scene in enumerate(scenes, start=1):
        write_json(scenes_dir / f"{index:04d}-{scene.get('id', 'scene')}.json", scene)
        count += 1
    return count


def export_scene_index(
    scenes: list[dict[str, Any]],
    out_dir: Path,
    character_by_id: dict[str, str] | None = None,
) -> None:
    by_id = character_by_id or {}
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        [
            "index",
            "scene_id",
            "summary",
            "location_id",
            "characters",
            "duration",
            "status",
            "continuity_start",
            "continuity_end",
        ]
    )
    for index, scene in enumerate(scenes, start=1):
        names = [by_id.get(item, item) for item in scene.get("characters", [])]
        writer.writerow(
            [
                index,
                scene.get("id", ""),
                str(scene.get("summary", "")).replace("\n", " "),
                scene.get("location_id", ""),
                "; ".join(names),
                scene.get("duration", ""),
                scene.get("status", ""),
                str(scene.get("start_state", {}).get("time", "")),
                str(scene.get("end_state", {}).get("time", "")),
            ]
        )
    write_text(out_dir / "05-scenes" / "scene-index.csv", buffer.getvalue())


def continuity_report(project: dict[str, Any]) -> dict[str, Any]:
    scenes = list(project.get("scenes", []))
    ordered = sorted(scenes, key=lambda item: int(item.get("order", 0)))
    broken: list[dict[str, Any]] = []
    boundary_count = 0
    for previous, current in zip(ordered, ordered[1:], strict=False):
        mode = str(current.get("visual_plan", {}).get("dependency_mode", "canonical"))
        if mode != "direct":
            continue
        boundary_count += 1
        prev_state = previous.get("end_state", {})
        current_state = current.get("start_state", {})
        mismatched = sorted(
            key
            for key in set(prev_state) | set(current_state)
            if prev_state.get(key) != current_state.get(key)
        )
        if mismatched:
            broken.append(
                {
                    "from_scene": previous.get("id", ""),
                    "from_order": previous.get("order", ""),
                    "to_scene": current.get("id", ""),
                    "to_order": current.get("order", ""),
                    "dependency_mode": mode,
                    "mismatched_fields": mismatched,
                }
            )
    checked = len(ordered) - 1
    return {
        "continuity_score": project.get("continuity_score", 0),
        "continuity_warnings": project.get("continuity_warnings", []),
        "scene_count": len(ordered),
        "boundary_count": boundary_count,
        "direct_boundaries_checked": checked,
        "broken_boundaries": broken,
        "chain_ok": not broken,
    }


def model_text(items: list[dict[str, Any]], entity_id: str, name: str) -> str:
    parts = [name]
    for item in items:
        if item.get("id") != entity_id:
            continue
        for value in item.values():
            if isinstance(value, str):
                parts.append(value)
            elif isinstance(value, list):
                parts.extend(str(part) for part in value)
            elif isinstance(value, dict):
                parts.extend(str(part) for part in value.values())
    return " ".join(parts)


def visual_bible_text(project: dict[str, Any], entity_id: str) -> str:
    for reference in project.get("visual_bible", {}).get("references", []):
        if reference.get("entity_id") != entity_id:
            continue
        return " ".join(
            [
                str(reference.get("name", "")),
                str(reference.get("lock_text", "")),
                " ".join(reference.get("reference_images", [])),
            ]
        )
    return ""


def find_matching_character(project: dict[str, Any], expected_name: str) -> dict[str, Any] | None:
    target = fold(expected_name)
    for character in project.get("characters", []):
        if fold(character.get("name", "")) == target:
            return character
    return None


def find_matching_prop(project: dict[str, Any], aliases: list[str]) -> dict[str, Any] | None:
    alias_keys = [fold(value) for value in aliases]
    for prop in project.get("props", []):
        name = fold(prop.get("name", ""))
        if any(alias in name or name in alias for alias in alias_keys):
            return prop
    return None


def semantic_report(project: dict[str, Any], source_truth: dict[str, Any]) -> dict[str, Any]:
    character_items: list[str] = []
    prop_items: list[str] = []
    scene_items: list[str] = []
    audio_items: list[str] = []
    timeline_items: list[str] = []
    hard_fact_items: list[str] = []
    warnings: list[str] = []

    total_character_assertions = 0
    total_prop_assertions = 0
    total_scene_assertions = 0
    total_audio_assertions = 0
    total_timeline_assertions = 0
    total_hard_fact_assertions = 0

    for expected_name, rule in source_truth.get("characters", {}).items():
        entity = find_matching_character(project, expected_name)
        required_terms = rule.get("required_terms", [])
        total_character_assertions += len(required_terms)
        if entity is None:
            character_items.append(f"MISSING character {expected_name}")
            continue
        character_text = model_text(
            project.get("characters", []), entity["id"], entity.get("name", "")
        )
        text = f"{character_text} {visual_bible_text(project, entity['id'])}"
        for term in required_terms:
            if not contains_term(text, term):
                character_items.append(
                    f"character {expected_name} missing locked fact: {term}"
                )

    for prop_key, rule in source_truth.get("props", {}).items():
        entity = find_matching_prop(project, rule.get("aliases", [prop_key]))
        required_terms = rule.get("required_terms", [])
        total_prop_assertions += len(required_terms)
        if entity is None:
            prop_items.append(f"MISSING prop {prop_key}")
            continue
        prop_text = model_text(
            project.get("props", []), entity["id"], entity.get("name", "")
        )
        text = f"{prop_text} {visual_bible_text(project, entity['id'])}"
        for term in required_terms:
            if not contains_term(text, term):
                prop_items.append(f"prop {prop_key} missing locked fact: {term}")

    character_by_id = scene_id_map(project)
    char_name_by_id = {item["id"]: item["name"] for item in project.get("characters", [])}
    ordered = sorted(project.get("scenes", []), key=lambda item: int(item.get("order", 0)))
    for scene in ordered:
        number = scene.get("order", 0)
        rule = source_truth.get("scene_visual_rules", {}).get(str(number), {})
        names_in_scene = [char_name_by_id.get(item, item) for item in scene.get("characters", [])]
        folded_names = {fold(name) for name in names_in_scene}
        required = {fold(value) for value in rule.get("required", [])}
        allowed = {fold(value) for value in rule.get("allowed", [])}
        forbidden = {fold(value) for value in rule.get("forbidden", [])}
        total_scene_assertions += bool(required) + bool(allowed) + bool(forbidden)
        if required - folded_names:
            scene_items.append(
                f"scene {number} missing required cast: "
                f"{sorted(required - folded_names)}"
            )
        if folded_names - allowed:
            scene_items.append(
                f"scene {number} has disallowed cast: {sorted(folded_names - allowed)}"
            )
        if folded_names & forbidden:
            scene_items.append(
                f"scene {number} has forbidden cast: {sorted(folded_names & forbidden)}"
            )
        referenced = prop_ids_for_scene(scene)
        for prop_key in rule.get("required_props", []):
            prop_rule = source_truth.get("props", {}).get(prop_key)
            if not prop_rule:
                continue
            entity = find_matching_prop(project, prop_rule.get("aliases", [prop_key]))
            if entity is None:
                continue
            if entity["id"] not in referenced:
                scene_items.append(f"scene {number} does not carry required prop {prop_key}")
        for prop_key in rule.get("forbidden_props", []):
            prop_rule = source_truth.get("props", {}).get(prop_key)
            if not prop_rule:
                continue
            entity = find_matching_prop(project, prop_rule.get("aliases", [prop_key]))
            if entity is not None and entity["id"] in referenced:
                scene_items.append(f"scene {number} references explicitly absent prop {prop_key}")

    expected_delivery = source_truth.get("audio_delivery", {})
    for scene in ordered:
        number = scene.get("order", 0)
        expected_scene_delivery = expected_delivery.get(str(number), [])
        total_audio_assertions += len(expected_scene_delivery)
        actual: list[tuple[str, str, str]] = []
        for dialogue in scene.get("dialogues", []):
            actual.append(
                (
                    character_by_id.get(dialogue.get("character_id", ""), ""),
                    dialogue.get("text", ""),
                    dialogue.get("delivery", "onscreen"),
                )
            )
        expected = [
            (str(speaker), str(text), str(delivery))
            for speaker, text, delivery in expected_scene_delivery
        ]
        if actual != expected:
            audio_items.append(
                f"scene {number} dialogue delivery mismatch: "
                f"{len(actual)} actual vs {len(expected)} expected"
            )

    timeline_map = source_truth.get("timeline", {})
    flashback = {int(value) for value in timeline_map.get("flashback_scenes", [])}
    present = {int(value) for value in timeline_map.get("present_scenes", [])}
    total_timeline_assertions = len(flashback) + len(present)
    for scene in ordered:
        number = scene.get("order", 0)
        domain = fold(scene.get("start_state", {}).get("time", ""))
        scene_time = scene.get("start_state", {}).get("time", "")
        if number in flashback and not domain.startswith("flashback"):
            timeline_items.append(
                f"scene {number} expected flashback time domain, got {scene_time!r}"
            )
        if number in present and not domain.startswith("present"):
            timeline_items.append(
                f"scene {number} expected present time domain, got {scene_time!r}"
            )

    for fact in source_truth.get("hard_facts", []):
        number = int(fact["scene"])
        total_hard_fact_assertions += len(fact.get("terms", []))
        scene = next(
            (item for item in ordered if int(item.get("order", 0)) == number), None
        )
        if scene is None:
            hard_fact_items.append(f"missing scene {number} for hard facts")
            continue
        text = scene_text(scene)
        for term in fact.get("terms", []):
            if not contains_term(text, term):
                hard_fact_items.append(f"scene {number} does not preserve hard fact: {term}")

    warm_markers = ("ấm", "warm", "warmer", "golden")
    for number in sorted(flashback):
        scene = next((item for item in ordered if int(item.get("order", 0)) == number), None)
        if scene is None:
            continue
        generated = fold(scene_text(scene))
        if not any(fold(marker) in generated for marker in warm_markers):
            warnings.append(f"flashback scene {number} lacks warmer visual treatment")

    passed_checks = (
        character_items
        == prop_items
        == scene_items
        == audio_items
        == timeline_items
        == hard_fact_items
        == []
    )
    total_assertions_possible = (
        total_character_assertions
        + total_prop_assertions
        + total_scene_assertions
        + total_audio_assertions
        + total_timeline_assertions
        + total_hard_fact_assertions
    )
    error_count = sum(
        len(items)
        for items in (
            character_items,
            prop_items,
            scene_items,
            audio_items,
            timeline_items,
            hard_fact_items,
        )
    )
    verdict = "PASS" if passed_checks else "FAIL"
    return {
        "scope": "source-truth comparison",
        "verdict": verdict,
        "pass": passed_checks,
        "checks": {
            "characters": character_items,
            "props": prop_items,
            "scenes": scene_items,
            "audio_delivery": audio_items,
            "timeline": timeline_items,
            "hard_facts": hard_fact_items,
        },
        "warnings": warnings,
        "total_assertions_possible": total_assertions_possible,
        "error_count": error_count,
        "warning_count": len(warnings),
    }


def collect_environment(*, args: argparse.Namespace, input_path: Path) -> dict[str, Any]:
    git = git_refs(args.repo_root)
    return {
        "timestamp_utc": utc_iso(),
        "hostname": socket.gethostname(),
        "os_name": os.name,
        "platform": platform.system(),
        "platform_release": platform.release(),
        "platform_version": platform.version(),
        "machine": platform.machine(),
        "python_version": platform.python_version(),
        "python_executable": sys.executable,
        "application_version": args.app_version,
        "git_branch": git["branch"],
        "git_commit": git["commit"],
        "git_dirty": git["dirty"],
        "input_path": str(input_path),
        "input_sha256": args.input_sha256,
        "input_size_bytes": args.input_size,
        "input_lines": args.input_lines,
        "workspace": "",
        "output": "",
        "xkiro_configured": False,
        "xkiro_key_source": "",
        "analysis_provider": "xkiro",
        "analysis_model": args.model_id,
        "analysis_kind": "live-xkiro",
        "source_truth_path": str(args.source_truth) if args.source_truth else "",
    }


def write_git_files(output_dir: Path, repo: Path) -> None:
    metadata = git_refs(repo)
    status_lines = [
        f"branch: {metadata['branch']}",
        f"commit: {metadata['commit']}",
        f"dirty: {metadata['dirty']}",
    ]
    status_lines.extend(metadata["dirty_files"])
    write_text(output_dir / "00-environment" / "git-status.txt", "\n".join(status_lines) + "\n")
    write_text(
        output_dir / "00-environment" / "git-commit.txt",
        f"{metadata['commit']}\n",
    )


def write_environment(output_dir: Path, environment: dict[str, Any]) -> None:
    write_json(output_dir / "00-environment" / "environment.json", environment)


def build_result(outcome: dict[str, Any]) -> dict[str, Any]:
    return dict(outcome)


def build_report(result: dict[str, Any], timing: dict[str, Any]) -> str:
    lines = [
        "# XKiro E2E Acceptance Report",
        "",
        f"Run ID: {result['run_id']}",
        f"Timestamp: {result['timestamp_utc']}",
        f"Git commit: {result.get('git_commit', '')}",
        "",
        "## Input",
        f"File: {result.get('input_path', '')}",
        f"SHA-256: {result.get('input_sha256', '')}",
        f"Characters: {result.get('input_size', 0)}",
        f"Lines: {result.get('input_lines', 0)}",
        "",
        "## Application",
        f"Desktop started: {result.get('application_started', False)}",
        f"Backend health: {result.get('backend_ready', False)}",
        f"Workspace: {result.get('workspace', '')}",
        "",
        "## xKiro",
        f"Connected: {result.get('xkiro_connected', False)}",
        f"Key source: {result.get('xkiro_key_source', '')}",
        f"Model: {result.get('analysis_model', '')}",
        f"Provider: {result.get('analysis_provider', '')}",
        "",
        "## Analysis",
        f"Job ID: {result.get('analysis_job_id', '')}",
        f"Status: {result.get('analysis_job_status', '')}",
        f"Duration: {result.get('analysis_seconds', 0)}s",
        f"Scene count: {result.get('scene_count', 0)}",
        f"Continuity score: {result.get('continuity_score', '')}",
        "",
        "## Validation",
        f"Source truth: {result.get('source_truth', '')}",
        f"Continuity: {result.get('continuity_chain_ok', '')}",
        f"Characters: {result.get('semantic_checks_characters', '')}",
        f"Locations: {result.get('semantic_locations', '')}",
        f"Props: {result.get('semantic_checks_props', '')}",
        f"Timeline: {result.get('semantic_checks_timeline', '')}",
        f"Dialogue delivery: {result.get('semantic_checks_audio', '')}",
        "",
        "## Export",
        f"Canonical project: {result.get('canonical_project_exported', False)}",
        f"Scene files: {result.get('scene_files', 0)}",
        f"Manifest: {result.get('manifest_entries', 0)}",
        f"Secrets found: {result.get('secrets_exported', False)}",
        "",
        "## Result",
        result.get("status", "FAIL"),
        "",
        "## Remaining issues",
    ]
    lines.extend(f"- {item}" for item in result.get("remaining_issues", []))
    lines.append("")
    lines.append("## Timing")
    for key, value in timing.items():
        lines.append(f"- {key}: {value}")
    lines.append("")
    return "\n".join(lines)


def request_json(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    session_token: str | None = None,
    payload: Any = None,
    timeout: float = 30.0,
) -> tuple[int, dict[str, Any]]:
    headers: dict[str, str] = {"Accept": "application/json"}
    if session_token:
        headers["X-Flow-Studio-Session"] = session_token
    response = client.request(
        method,
        url,
        headers=headers,
        json=payload if payload is not None else None,
        timeout=timeout,
    )
    try:
        body = response.json()
    except ValueError:
        body = {"raw": response.text[:2000]}
    return response.status_code, body


def orchestrate(
    *,
    base_url: str,
    session_token: str,
    args: argparse.Namespace,
    workspace_dir: Path,
    output_dir: Path,
) -> tuple[int, dict[str, Any], dict[str, Any]]:
    model_id = args.model_id
    environment = collect_environment(args=args, input_path=args.input_path)
    environment["workspace"] = str(workspace_dir)
    environment["output"] = str(output_dir)
    timing: dict[str, Any] = {}
    issues: list[str] = []
    exit_code = EXIT_OK
    event_path = output_dir / "02-analysis-job" / "job-events.jsonl"

    started_wall = time.monotonic()
    timing["application_start_seconds"] = round(time.monotonic() - started_wall, 3)
    timing["backend_ready_seconds"] = 0.0
    timing["xkiro_status_seconds"] = 0.0
    timing["analysis_seconds"] = 0.0
    timing["export_seconds"] = 0.0

    client = httpx.Client(base_url=base_url, timeout=30.0)
    result: dict[str, Any] = {
        "run_id": output_dir.name,
        "timestamp_utc": utc_iso(),
        "status": "FAIL",
        "application_started": False,
        "backend_ready": False,
        "workspace": str(workspace_dir),
        "output": str(output_dir),
        "xkiro_connected": False,
        "xkiro_key_source": "",
        "analysis_provider": "xkiro",
        "analysis_model": model_id,
        "analysis_job_id": "",
        "analysis_job_status": "",
        "analysis_seconds": 0,
        "project_id": "",
        "scene_count": 0,
        "continuity_score": "",
        "continuity_chain_ok": "",
        "canonical_project_exported": False,
        "storage_project_exported": False,
        "scene_files": 0,
        "manifest_entries": 0,
        "secrets_exported": False,
        "secret_scan_hits": [],
        "semantic_validation": "PARTIAL",
        "semantic_checks_characters": 0,
        "semantic_checks_props": 0,
        "semantic_checks_scenes": 0,
        "semantic_checks_audio": 0,
        "semantic_checks_timeline": 0,
        "semantic_checks_hard_facts": 0,
        "semantic_locations": "n/a",
        "source_truth": "",
        "input_path": str(args.input_path),
        "input_sha256": args.input_sha256,
        "input_size": args.input_size,
        "input_lines": args.input_lines,
        "git_commit": environment["git_commit"],
        "remaining_issues": issues,
    }
    for key, value in environment.items():
        if key not in result:
            result[key] = value

    health_ok = False
    try:
        status_code, _ = request_json(client, "GET", "/api/health")
        health_ok = status_code == 200
    except httpx.HTTPError:
        health_ok = False
    result["application_started"] = True
    result["backend_ready"] = health_ok
    timing["backend_ready_seconds"] = round(time.monotonic() - started_wall, 3)
    if not health_ok:
        exit_code = EXIT_APP_STARTUP
        issues.append("Backend health check failed.")

    xkiro_wall = time.monotonic()
    status_payload: dict[str, Any] = {}
    models: list[Any] = []
    if exit_code == EXIT_OK:
        try:
            status_code, status_payload = request_json(
                client, "GET", "/api/ai/xkiro/status?include_models=true", timeout=45
            )
        except httpx.HTTPError:
            status_code = 0
        timing["xkiro_status_seconds"] = round(time.monotonic() - xkiro_wall, 3)
        if status_code != 200 or not status_payload.get("configured"):
            exit_code = EXIT_XKIRO_UNAVAILABLE
            issues.append("xKiro credential is not configured.")
        else:
            result["xkiro_connected"] = True
            result["xkiro_key_source"] = status_payload.get("source", "")
            models = status_payload.get("models", [])
            environment["xkiro_configured"] = True
            environment["xkiro_key_source"] = status_payload.get("source", "")
            write_json(
                output_dir / "00-environment" / "xkiro-models.json",
                [
                    {
                        "id": model.get("id", ""),
                        "display_name": model.get("display_name", ""),
                        "owned_by": model.get("owned_by", ""),
                        "access_tier": model.get("access_tier", ""),
                        "context_length": model.get("context_length"),
                        "max_output_tokens": model.get("max_output_tokens"),
                        "capabilities": {
                            key: bool(value)
                            for key, value in model.get("capabilities", {}).items()
                        },
                    }
                    for model in models
                ],
            )

    if exit_code == EXIT_OK:
        model_guard = check_model(models, model_id)
        if model_guard != EXIT_OK:
            exit_code = model_guard
            issues.append(f"Model not found in xKiro catalog: {model_id}")

    if exit_code == EXIT_OK and args.source_truth:
        shutil.copyfile(args.source_truth, output_dir / "01-input" / "source-truth.json")
        result["source_truth"] = str(args.source_truth)

    job: dict[str, Any] = {}
    if exit_code == EXIT_OK:
        request_payload: dict[str, Any] = {
            "name": "xkiro-e2e-acceptance",
            "original_text": args.original_text,
            "settings": {
                "analysis_provider": "xkiro",
                "analysis_model": model_id,
                "provider": "mock",
                "aspect_ratio": "16:9",
                "resolution": "1080p",
                "style": "Cinematic",
                "custom_style": "",
                "scene_duration": 8,
                "character_lock": True,
                "location_lock": True,
                "auto_continuity": True,
                "quality_threshold": 85,
            },
        }
        status_code, job = request_json(
            client,
            "POST",
            "/api/analysis/jobs",
            session_token=session_token,
            payload=request_payload,
            timeout=60,
        )
        if status_code != 202:
            exit_code = EXIT_ANALYSIS_FAILED
            issues.append(f"Analysis job creation failed with HTTP {status_code}.")
        else:
            result["analysis_job_status"] = job.get("status", "")
            result["analysis_job_id"] = job.get("id", "")
            write_json(output_dir / "02-analysis-job" / "job-created.json", sanitize_payload(job))
            with event_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {"status": job.get("status"), "at": utc_iso()}, ensure_ascii=False
                    )
                    + "\n"
                )

    polling_wall = time.monotonic()
    if exit_code == EXIT_OK:
        terminal = {"completed", "failed", "cancelled"}
        previous_status = str(job.get("status", ""))
        deadline = polling_wall + args.job_timeout_minutes * 60
        while str(job.get("status", "")) not in terminal:
            if time.monotonic() >= deadline:
                issues.append(
                    f"Analysis job exceeded {args.job_timeout_minutes} minute timeout."
                )
                exit_code = EXIT_ANALYSIS_FAILED
                break
            time.sleep(args.poll_interval)
            try:
                status_code, job = request_json(
                    client, "GET", f"/api/analysis/jobs/{result['analysis_job_id']}"
                )
            except httpx.HTTPError:
                status_code = 0
                job = {}
            if status_code != 200:
                continue
            current_status = str(job.get("status", ""))
            if current_status != previous_status:
                with event_path.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(
                            {"status": current_status, "at": utc_iso()},
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                previous_status = current_status
        timing["analysis_seconds"] = round(time.monotonic() - polling_wall, 3)
        result["analysis_seconds"] = round(timing["analysis_seconds"], 1)
        result["analysis_job_status"] = str(job.get("status", ""))
        write_json(output_dir / "02-analysis-job" / "job-final.json", sanitize_payload(job))
        logs = job.get("logs", []) if isinstance(job, dict) else []
        write_json(output_dir / "02-analysis-job" / "job-logs.json", sanitize_payload(logs))
        termination = job_outcome(job)
        if termination != EXIT_OK:
            exit_code = termination
            error_text = str(job.get("error", "")).strip()
            if error_text:
                issues.append(f"Analysis job failed: {error_text}")

    project: dict[str, Any] = {}
    stored_project: dict[str, Any] = {}
    if exit_code == EXIT_OK:
        project_id = str(job.get("project", {}).get("id", ""))
        result["project_id"] = project_id
        status_code, project = request_json(
            client, "GET", f"/api/projects/{project_id}", timeout=60
        )
        if status_code != 200:
            exit_code = EXIT_ANALYSIS_FAILED
            issues.append(f"Full canonical project could not be read (HTTP {status_code}).")
        else:
            write_json(output_dir / "03-project" / "project-full.json", project)
            result["canonical_project_exported"] = True
            storage_path = workspace_dir / "projects" / f"{project_id}.json"
            if not storage_path.is_file():
                exit_code = EXIT_VALIDATION_FAILED
                issues.append("Persistence file missing in workspace projects.")
            else:
                shutil.copyfile(
                    storage_path, output_dir / "03-project" / "project-storage-original.json"
                )
                stored_project = read_json(
                    output_dir / "03-project" / "project-storage-original.json"
                )
                result["storage_project_exported"] = True
                comparison = canonical_equals(project, stored_project)
                if not comparison["equal"]:
                    exit_code = EXIT_VALIDATION_FAILED
                    issues.append(
                        "Canonical API response and storage representation differ: "
                        + ", ".join(comparison["differing_fields"])
                    )
                result["scene_count"] = len(project.get("scenes", []))
                result["continuity_score"] = project.get("continuity_score", "")

    export_wall = time.monotonic()
    if project:
        export_domains(project, output_dir)
        result["scene_files"] = export_scene_files(project.get("scenes", []), output_dir)
        export_scene_index(project.get("scenes", []), output_dir, scene_id_map(project))
        continuity = continuity_report(project)
        write_json(output_dir / "06-validation" / "continuity-report.json", continuity)
        result["continuity_chain_ok"] = continuity["chain_ok"]
        if not continuity["chain_ok"]:
            if exit_code == EXIT_OK:
                exit_code = EXIT_VALIDATION_FAILED
            issues.append(
                f"Direct continuation chain has "
                f"{len(continuity['broken_boundaries'])} broken boundaries."
            )
        semantic: dict[str, Any] | None = None
        if args.source_truth:
            semantic = semantic_report(project, read_json(args.source_truth))
            write_json(output_dir / "06-validation" / "semantic-validation.json", semantic)
            result["semantic_validation"] = semantic["verdict"]
            checks = semantic["checks"]
            result["semantic_checks_characters"] = len(checks["characters"])
            result["semantic_checks_props"] = len(checks["props"])
            result["semantic_checks_scenes"] = len(checks["scenes"])
            result["semantic_checks_audio"] = len(checks["audio_delivery"])
            result["semantic_checks_timeline"] = len(checks["timeline"])
            result["semantic_checks_hard_facts"] = len(checks["hard_facts"])
            if semantic["error_count"]:
                if exit_code == EXIT_OK:
                    exit_code = EXIT_VALIDATION_FAILED
                issues.append(
                    f"Source-truth comparison found {semantic['error_count']} mismatches "
                    f"({semantic['warning_count']} warnings); review for the independent verdict."
                )
    timing["export_seconds"] = round(time.monotonic() - export_wall, 3)

    timing["total_seconds"] = round(time.monotonic() - started_wall, 3)
    write_environment(output_dir, environment)
    write_git_files(output_dir, args.repo_root)
    write_json(output_dir / "07-metrics" / "timing.json", timing)

    result["status"] = "PASS" if exit_code == EXIT_OK else "FAIL"
    result["remaining_issues"] = issues
    for key, value in timing.items():
        result[f"timing_{key}"] = value
    write_json(output_dir / "result.json", result)
    write_text(output_dir / "REPORT.md", build_report(result, timing))

    manifest_entries = build_manifest(output_dir)
    write_json(output_dir / "manifest.json", {"files": manifest_entries})
    result["manifest_entries"] = len(manifest_entries)
    write_json(output_dir / "result.json", result)
    write_text(output_dir / "REPORT.md", build_report(result, timing))

    scan_hits = secret_scan(output_dir)
    if scan_hits:
        exit_code = EXIT_SECRET_SCAN
        issues.append(
            f"Secret scan found patterns in {len(scan_hits)} file(s); inspect and clean."
        )
        result["secret_scan_hits"] = scan_hits
        result["secrets_exported"] = True
        result["status"] = "FAIL"
        result["remaining_issues"] = issues
        write_json(output_dir / "result.json", result)
        write_text(output_dir / "REPORT.md", build_report(result, timing))
    return exit_code, result, environment


def run_live(args: argparse.Namespace) -> int:
    import webview

    from flow_story_studio.desktop import DesktopSession

    workspace_dir = make_run(args.workspace_root)
    output_dir = make_run(args.output_root)
    session = DesktopSession(webview)
    started_wall = time.monotonic()
    try:
        url = session._start_backend(workspace_dir)
    except Exception as exc:
        session._shutdown()
        raise RuntimeError(f"Application startup failed: {exc}") from exc
    base_url = urlsplit(url)._replace(fragment="").geturl()
    session_token = url.split("#session=", 1)[1] if "#session=" in url else ""
    timing_state: dict[str, Any] = {}

    outcome: dict[str, Any] = {}

    def automation() -> None:
        timing_state["application_start_seconds"] = round(time.monotonic() - started_wall, 3)
        try:
            exit_code, result, _ = orchestrate(
                base_url=base_url,
                session_token=session_token,
                args=args,
                workspace_dir=workspace_dir,
                output_dir=output_dir,
            )
            outcome["exit_code"] = exit_code
            outcome["result"] = result
        except Exception as exc:
            outcome["exit_code"] = EXIT_GENERAL
            outcome["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                window.destroy()
            except Exception:
                pass

    window = webview.create_window(
        "TH Media",
        url,
        width=1100,
        height=700,
        min_size=(680, 520),
        background_color="#0b0d12",
    )
    try:
        webview.start(func=automation, gui="edgechromium", debug=False)
    except Exception as exc:
        session._shutdown()
        raise RuntimeError(f"Desktop UI could not start: {exc}") from exc
    finally:
        session._shutdown()
    if "error" in outcome:
        raise RuntimeError(outcome["error"])
    return int(outcome.get("exit_code", EXIT_GENERAL))


def run_export_existing(args: argparse.Namespace) -> int:
    project_id = args.export_existing_project
    candidates = list(Path(args.workspace_root).glob(f"**/projects/{project_id}.json"))
    if not candidates:
        print(f"No persisted project {project_id} found under {args.workspace_root}", flush=True)
        return EXIT_EXPORT_FAILED
    project = read_json(candidates[0])
    workspace_dir = candidates[0].parents[1]
    output_dir = make_run(args.output_root)
    original_text = str(project.get("original_text", ""))
    args.original_text = original_text
    args.input_path = output_dir / "01-input" / "screenplay-original.md"
    args.input_sha256 = sha256_bytes(original_text.encode("utf-8"))
    args.input_size = len(original_text.encode("utf-8"))
    args.input_lines = original_text.count("\n") + 1
    write_text(output_dir / "01-input" / "screenplay-original.md", original_text)
    write_text(
        output_dir / "01-input" / "screenplay.sha256",
        f"{args.input_sha256}  screenplay-original.md\n",
    )
    write_json(
        output_dir / "02-analysis-job" / "job-created.json",
        {"status": "local-export", "project_id": project_id},
    )
    write_json(
        output_dir / "02-analysis-job" / "job-final.json",
        {"status": "local-export", "project_id": project_id},
    )
    write_json(
        output_dir / "02-analysis-job" / "job-logs.json",
        ["Re-exported from workspace projects; no live xKiro call was made."],
    )
    args.repo_root = Path(__file__).resolve().parents[1]
    environment = collect_environment(args=args, input_path=args.input_path)
    environment["analysis_kind"] = "local-export"
    environment["workspace"] = str(workspace_dir)
    environment["output"] = str(output_dir)
    environment["xkiro_configured"] = False
    environment["xkiro_key_source"] = "none"
    write_environment(output_dir, environment)
    write_git_files(output_dir, args.repo_root)

    exit_code = EXIT_OK
    issues: list[str] = []
    result: dict[str, Any] = {
        "run_id": output_dir.name,
        "timestamp_utc": utc_iso(),
        "status": "FAIL",
        "application_started": False,
        "backend_ready": False,
        "workspace": str(workspace_dir),
        "output": str(output_dir),
        "xkiro_connected": False,
        "xkiro_key_source": "none",
        "analysis_provider": "xkiro",
        "analysis_model": args.model_id,
        "analysis_job_id": project_id,
        "analysis_job_status": "local-export",
        "analysis_seconds": 0,
        "project_id": project_id,
        "scene_count": len(project.get("scenes", [])),
        "continuity_score": project.get("continuity_score", ""),
        "continuity_chain_ok": "",
        "canonical_project_exported": True,
        "storage_project_exported": True,
        "scene_files": 0,
        "manifest_entries": 0,
        "secrets_exported": False,
        "secret_scan_hits": [],
        "semantic_validation": "PARTIAL",
        "semantic_checks_characters": 0,
        "semantic_checks_props": 0,
        "semantic_checks_scenes": 0,
        "semantic_checks_audio": 0,
        "semantic_checks_timeline": 0,
        "semantic_checks_hard_facts": 0,
        "semantic_locations": "n/a",
        "source_truth": "",
        "input_path": str(args.input_path),
        "input_sha256": args.input_sha256,
        "input_size": args.input_size,
        "input_lines": args.input_lines,
        "git_commit": environment["git_commit"],
        "remaining_issues": issues,
    }
    for key, value in environment.items():
        if key not in result:
            result[key] = value

    timing: dict[str, Any] = {"local_export": True}
    export_wall = time.monotonic()
    if project:
        export_domains(project, output_dir)
        result["scene_files"] = export_scene_files(project.get("scenes", []), output_dir)
        export_scene_index(project.get("scenes", []), output_dir, scene_id_map(project))
        continuity = continuity_report(project)
        write_json(output_dir / "06-validation" / "continuity-report.json", continuity)
        result["continuity_chain_ok"] = continuity["chain_ok"]
        if not continuity["chain_ok"]:
            issues.append(
                f"Direct continuation chain has "
                f"{len(continuity['broken_boundaries'])} broken boundaries."
            )
        if args.source_truth:
            semantic = semantic_report(project, read_json(args.source_truth))
            write_json(output_dir / "06-validation" / "semantic-validation.json", semantic)
            result["semantic_validation"] = semantic["verdict"]
            checks = semantic["checks"]
            result["semantic_checks_characters"] = len(checks["characters"])
            result["semantic_checks_props"] = len(checks["props"])
            result["semantic_checks_scenes"] = len(checks["scenes"])
            result["semantic_checks_audio"] = len(checks["audio_delivery"])
            result["semantic_checks_timeline"] = len(checks["timeline"])
            result["semantic_checks_hard_facts"] = len(checks["hard_facts"])
    timing["total_seconds"] = round(time.monotonic() - export_wall, 3)
    write_json(output_dir / "03-project" / "project-full.json", project)
    write_json(output_dir / "07-metrics" / "timing.json", timing)

    result["status"] = "PASS" if exit_code == EXIT_OK else "FAIL"
    result["remaining_issues"] = issues
    write_json(output_dir / "result.json", result)
    write_text(output_dir / "REPORT.md", build_report(result, timing))

    manifest_entries = build_manifest(output_dir)
    write_json(output_dir / "manifest.json", {"files": manifest_entries})
    result["manifest_entries"] = len(manifest_entries)
    scan_hits = secret_scan(output_dir)
    if scan_hits:
        result["secret_scan_hits"] = scan_hits
        result["secrets_exported"] = True
        write_json(output_dir / "result.json", result)
        write_text(output_dir / "REPORT.md", build_report(result, timing))
        exit_code = EXIT_SECRET_SCAN
    print(f"Re-exported project {project_id} to {output_dir}", flush=True)
    return exit_code


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="TH Media xKiro E2E acceptance runner")
    parser.add_argument("--input", type=Path, help="Screenplay Markdown source")
    parser.add_argument("--source-truth", type=Path, help="Source-truth JSON fixture")
    parser.add_argument("--model", default="minimax/minimax-m3:free", help="xKiro model ID")
    parser.add_argument("--workspace-root", type=Path, help="Acceptance workspace root")
    parser.add_argument("--output-root", type=Path, help="Acceptance output root")
    parser.add_argument("--export-existing-project", help="Re-export an existing project id")
    parser.add_argument("--poll-interval", type=float, default=3.0, help="Job poll interval")
    parser.add_argument("--job-timeout-minutes", type=int, default=240, help="Job timeout")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        from importlib.metadata import version

        args.app_version = version("flow-story-studio")
    except Exception:
        args.app_version = "1.8.3"
    args.workspace_root = args.workspace_root or Path(
        r"C:\Users\Admin\Desktop\phim\xkiro-acceptance-workspace"
    )
    args.output_root = args.output_root or Path(
        r"C:\Users\Admin\Desktop\phim\xkiro-acceptance-results"
    )
    args.repo_root = Path(__file__).resolve().parents[1]

    if args.export_existing_project:
        args.model_id = args.model
        return run_export_existing(args)

    fixtures_root = Path(__file__).resolve().parents[1] / "acceptance" / "fixtures"
    args.input = args.input or fixtures_root / "chiec-ve-khong-co-chuyen-tau.md"
    args.source_truth = args.source_truth or fixtures_root / "chiec-ve-source-truth.json"
    if not args.input.is_file():
        print(f"Input screenplay not found: {args.input}", flush=True)
        return EXIT_GENERAL
    if not args.source_truth.is_file():
        print(f"Source-truth fixture not found: {args.source_truth}", flush=True)
        return EXIT_GENERAL
    original = args.input.read_bytes()
    args.original_text = original.decode("utf-8")
    args.input_path = args.input
    args.input_sha256 = sha256_bytes(original)
    args.input_size = len(original)
    args.input_lines = args.original_text.count("\n") + 1
    args.model_id = args.model
    args.workspace_root.mkdir(parents=True, exist_ok=True)
    args.output_root.mkdir(parents=True, exist_ok=True)
    return run_live(args)


if __name__ == "__main__":
    raise SystemExit(main())