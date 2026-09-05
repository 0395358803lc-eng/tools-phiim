"""Shared error types for the embedded Google Flow integration."""

from __future__ import annotations

from collections.abc import Callable

from ..models import Project, Scene


class FlowIntegrationError(RuntimeError):
    """A user-safe embedded Flow error."""


RenderCheckpoint = Callable[[Project, Scene], None]