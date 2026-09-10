from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CORPUS_ROOT = ROOT / "acceptance" / "corpus"
MANIFEST = json.loads((CORPUS_ROOT / "manifest.json").read_text(encoding="utf-8"))

_spec = importlib.util.spec_from_file_location(
    "analysis_acceptance_suite",
    ROOT / "scripts" / "analysis-acceptance-suite.py",
)
assert _spec is not None and _spec.loader is not None
_suite = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_suite)


@pytest.mark.parametrize(
    "case",
    MANIFEST["cases"],
    ids=[item["file"] for item in MANIFEST["cases"]],
)
def test_analysis_acceptance_corpus(case: dict) -> None:
    result = _suite.run_case(CORPUS_ROOT, case)
    assert result["passed"], result["failures"]
