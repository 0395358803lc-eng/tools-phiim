"""Unit tests for the xKiro E2E acceptance runner helpers (no live API)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_RUNNER_PATH = Path(__file__).resolve().parents[1] / "scripts" / "xkiro_acceptance_runner.py"


def load_runner():
    spec = importlib.util.spec_from_file_location("xkiro_acceptance_runner", _RUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


runner = load_runner()


def test_make_run_creates_isolated_run_directory(tmp_path: Path) -> None:
    run_dir = runner.make_run(tmp_path)

    assert run_dir.parent == tmp_path
    assert run_dir.name.startswith("run-")
    for subdir in runner.SUBDIRS:
        assert (run_dir / subdir).is_dir()


def test_sha256_generation_is_deterministic(tmp_path: Path) -> None:
    payload = "Chiếc vé không có chuyến tàu\n".encode()
    first = runner.sha256_bytes(payload)
    second = runner.sha256_bytes(payload)
    assert first == second
    assert len(first) == 64

    path = tmp_path / "screenplay.md"
    path.write_bytes(payload)
    assert runner.sha256_file(path) == first


def test_manifest_generation_hashes_all_except_itself(tmp_path: Path) -> None:
    runner.write_json(tmp_path / "01-input" / "screenplay.sha256", {"example": "x"})
    runner.write_text(tmp_path / "nested" / "note.txt", "payload")
    runner.write_json(tmp_path / "result.json", {"status": "PASS"})

    entries = runner.build_manifest(tmp_path)
    paths = {entry["relative_path"] for entry in entries}

    assert "01-input/screenplay.sha256" in paths
    assert "nested/note.txt" in paths
    assert "result.json" in paths
    assert "manifest.json" not in paths
    for entry in entries:
        target = tmp_path / entry["relative_path"]
        assert entry["size_bytes"] == target.stat().st_size
        assert entry["sha256"] == runner.sha256_file(target)


def test_scene_export_writes_numbered_files_and_index(tmp_path: Path) -> None:
    scenes = [
        {
            "id": "SCENE_001",
            "order": 1,
            "summary": "Khải ở căn hộ",
            "location_id": "LOC_APARTMENT",
            "characters": ["char_khai"],
            "duration": 8,
            "status": "Accepted",
            "start_state": {"time": "Present - 23:17", "camera": "Wide"},
            "end_state": {"time": "Present - 23:17", "camera": "Medium"},
        },
        {
            "id": "SCENE_002",
            "order": 2,
            "summary": "Khải nhận cuộc gọi",
            "location_id": "LOC_APARTMENT",
            "characters": ["char_khai"],
            "duration": 8,
            "status": "Waiting",
            "start_state": {"time": "Present - 23:40", "camera": "Medium"},
            "end_state": {"time": "Present - 23:40", "camera": "Close"},
        },
    ]
    count = runner.export_scene_files(scenes, tmp_path)
    exporter = runner.export_scene_index(
        scenes, tmp_path, {"char_khai": "KHẢI"}
    )
    assert exporter is None
    assert count == 2
    assert (tmp_path / "05-scenes" / "0001-SCENE_001.json").is_file()
    assert (tmp_path / "05-scenes" / "0002-SCENE_002.json").is_file()

    csv_columns = "index,scene_id,summary,location_id,characters,duration,status"
    header = f"{csv_columns},continuity_start,continuity_end"
    index = (tmp_path / "05-scenes" / "scene-index.csv").read_text(encoding="utf-8")
    assert header in index
    assert "1,SCENE_001," in index
    assert "KHẢI" in index
    assert "Present - 23:17" in index


def test_continuity_report_chains_direct_boundaries() -> None:
    end_state = {"time": "Present - 23:40", "camera": "Wide", "notes": "Character at door"}
    project_ok = {
        "continuity_score": 100,
        "continuity_warnings": [],
        "scenes": [
            {
                "id": "S1",
                "order": 1,
                "end_state": end_state,
                "visual_plan": {"dependency_mode": "canonical"},
            },
            {
                "id": "S2",
                "order": 2,
                "start_state": dict(end_state),
                "visual_plan": {"dependency_mode": "direct"},
            },
            {
                "id": "S3",
                "order": 3,
                "start_state": {"time": "Present", "camera": "Wide", "notes": ""},
                "visual_plan": {"dependency_mode": "canonical"},
            },
        ],
    }
    report_ok = runner.continuity_report(project_ok)
    assert report_ok["boundary_count"] == 1
    assert report_ok["broken_boundaries"] == []
    assert report_ok["chain_ok"] is True

    project_broken = {
        "continuity_score": 90,
        "continuity_warnings": ["mismatch"],
        "scenes": [
            {
                "id": "S1",
                "order": 1,
                "end_state": {"time": "Present - 23:40", "camera": "Wide"},
                "visual_plan": {"dependency_mode": "canonical"},
            },
            {
                "id": "S2",
                "order": 2,
                "start_state": {"time": "Present - 23:40", "camera": "Close"},
                "visual_plan": {"dependency_mode": "direct"},
            },
        ],
    }
    report_broken = runner.continuity_report(project_broken)
    assert report_broken["chain_ok"] is False
    assert len(report_broken["broken_boundaries"]) == 1
    assert report_broken["broken_boundaries"][0]["mismatched_fields"] == ["camera"]


def test_secret_scan_detects_credentials_and_pass_on_clean(tmp_path: Path) -> None:
    clean_hits = runner.secret_scan(tmp_path)
    assert clean_hits == []

    (tmp_path / "03-project" / "leak.json").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "03-project" / "leak.json").write_text(
        '{"header": "Authorization: Bearer sk-xt-9f8a7b6c5d4e3f2a1b0c"}',
        encoding="utf-8",
    )

    hits = runner.secret_scan(tmp_path)
    patterns = {hit["pattern"] for hit in hits}
    assert "authorization-header" in patterns
    assert "bearer-token" in patterns
    assert "xkiro-api-key" in patterns
    assert hits[0]["path"].startswith("03-project/")


def test_job_outcome_handles_terminal_states() -> None:
    assert runner.job_outcome({"status": "queued"}) is None
    assert runner.job_outcome({"status": "running"}) is None
    assert runner.job_outcome({"status": "completed", "project": {"id": "p1"}}) == runner.EXIT_OK
    assert (
        runner.job_outcome({"status": "completed", "project": None})
        == runner.EXIT_ANALYSIS_FAILED
    )
    assert runner.job_outcome({"status": "completed"}) == runner.EXIT_ANALYSIS_FAILED
    assert runner.job_outcome({"status": "failed", "error": "x"}) == runner.EXIT_ANALYSIS_FAILED
    assert runner.job_outcome({"status": "cancelled"}) == runner.EXIT_ANALYSIS_FAILED


def test_model_not_found_guard() -> None:
    models = [
        {"id": "minimax/minimax-m3:free", "access_tier": "free"},
        {"id": "google/gemini-2.5-flash", "access_tier": "premium"},
    ]
    assert runner.check_model(models, "minimax/minimax-m3:free") == runner.EXIT_OK
    assert runner.check_model(models, "does/not-exist") == runner.EXIT_MODEL_UNAVAILABLE
    assert runner.resolve_model(models, "google/gemini-2.5-flash")["access_tier"] == "premium"
    assert runner.resolve_model(models, "missing") is None


def test_canonical_storage_equality_detects_semantic_delta() -> None:
    same_other = {"a": 1, "b": "x"}
    comparison = runner.canonical_equals({"a": 1, "b": "x"}, same_other)
    assert comparison["equal"] is True
    assert comparison["differing_fields"] == []

    delta = runner.canonical_equals({"a": 1, "b": "y"}, {"a": 1, "b": "x"})
    assert delta["equal"] is False
    assert delta["differing_fields"] == ["b"]


def test_sanitize_payload_redacts_secrets() -> None:
    payload = {
        "api_key": "sk-xt-9f8a7b6c5d4e3f2a1b0c",
        "settings": {"model": "minimax/minimax-m3:free"},
        "logs": ["Authorization: Bearer sk-xt-0123456789abcdef"],
        "note": "cookie=abcdef123456",
    }
    cleaned = runner.sanitize_payload(payload)

    assert cleaned["api_key"] == "[REDACTED]"
    cleaned_text = json.dumps(cleaned)
    assert "sk-xt-" not in cleaned_text
    assert "Authorization" not in cleaned_text
    assert cleaned["settings"]["model"] == "minimax/minimax-m3:free"


def test_semantic_report_covers_source_truth_sections(tmp_path: Path) -> None:
    source_truth = {
        "characters": {"KHẢI": {"required_terms": ["35", "sơ mi xám đậm"]}},
        "props": {
            "vé": {
                "aliases": ["vé tàu", "chiếc vé"],
                "required_terms": ["xanh nhạt", "rách"],
            }
        },
        "scene_visual_rules": {
            "1": {"required": ["KHẢI"], "allowed": ["KHẢI"], "forbidden": ["AN"]}
        },
        "timeline": {"flashback_scenes": [2], "present_scenes": [1]},
        "audio_delivery": {"1": [["KHẢI", "Em đang ở đâu?", "onscreen"]]},
        "hard_facts": [{"scene": 1, "terms": ["23:17"]}],
    }
    project = {
        "characters": [
            {
                "id": "char_khai",
                "name": "KHẢI",
                "estimated_age": "35",
                "clothing": "sơ mi xám đậm",
                "gender": "Nam",
            }
        ],
        "props": [
            {
                "id": "prop_ve",
                "name": "chiếc vé xanh",
                "description": "Vé tàu màu xanh nhạt đã rách",
                "state": "rách",
            }
        ],
        "scenes": [
            {
                "id": "SCENE_001",
                "order": 1,
                "title": "CĂN HỘ CỦA KHẢI",
                "summary": "Khải gọi điện lúc 23:17",
                "source_text": "23:17 KHẢI gọi",
                "action": "căn hộ",
                "camera": "Wide",
                "lighting": "là",
                "atmosphere": "tĩnh",
                "visual_prompt": "realistic",
                "render_prompt": "x",
                "characters": ["char_khai"],
                "start_state": {"time": "Present - 23:17", "character_positions": {}},
                "end_state": {"time": "Present - 23:17", "character_positions": {}},
                "visual_plan": {"dependency_mode": "canonical"},
                "dialogues": [
                    {"character_id": "char_khai", "text": "Em đang ở đâu?", "delivery": "onscreen"}
                ],
            },
            {
                "id": "SCENE_002",
                "order": 2,
                "title": "SÂN GA",
                "summary": "Nhớ lại",
                "source_text": "Flashback",
                "action": "",
                "camera": "",
                "lighting": "",
                "atmosphere": "",
                "visual_prompt": "",
                "render_prompt": "",
                "characters": [],
                "start_state": {"time": "Flashback - sáu tháng trước", "character_positions": {}},
                "end_state": {"time": "Flashback", "character_positions": {}},
                "visual_plan": {"dependency_mode": "canonical"},
                "dialogues": [],
            },
        ],
        "visual_bible": {"references": []},
    }

    report = runner.semantic_report(project, source_truth)
    assert report["verdict"] == "PASS"
    assert report["pass"] is True
    assert report["error_count"] == 0

    broken = runner.semantic_report(
        project, {"characters": {}, "props": {}, "scene_visual_rules": {}}
    )
    assert isinstance(broken["checks"]["characters"], list)