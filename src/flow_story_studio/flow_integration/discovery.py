"""Detection of the vendored Flow CLI package."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _add_development_flow_cli_path() -> None:
    """Find the sibling source tree in development; frozen builds bundle it."""
    sibling = Path(__file__).resolve().parents[4] / "tool-phiim" / "src"
    if sibling.is_dir() and str(sibling) not in sys.path:
        sys.path.insert(0, str(sibling))


def _flow_cli_available() -> bool:
    _add_development_flow_cli_path()
    return importlib.util.find_spec("flow_cli") is not None


def _gflow_cli_available() -> bool:
    return importlib.util.find_spec("gflow_cli") is not None