"""Provider-neutral contract for canonical reference generation."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..models import Project


class ReferenceProvider(Protocol):
    async def generate_reference_image(
        self,
        project: Project,
        reference_id: str,
        prompt: str,
        *,
        ingredient_files: list[Path] | None = None,
    ) -> str: ...
