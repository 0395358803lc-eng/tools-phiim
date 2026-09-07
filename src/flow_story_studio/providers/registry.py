"""Neutral provider registry for render backends."""

from __future__ import annotations

from .base import VideoProvider
from .mock import MockProvider
from .unavailable import UnavailableProvider


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, VideoProvider] = {}

    def register(self, name: str, provider: VideoProvider) -> None:
        self._providers[name] = provider

    def get(self, name: str) -> VideoProvider | None:
        return self._providers.get(name)

    def names(self) -> list[str]:
        return sorted(self._providers)

    def configured_names(self) -> list[str]:
        configured: list[str] = []
        for name, provider in self._providers.items():
            if name == "unconfigured":
                continue
            probe = getattr(provider, "is_configured", None)
            if callable(probe) and not bool(probe()):
                continue
            configured.append(name)
        return sorted(configured)


def build_default_registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register("mock", MockProvider())
    registry.register("unconfigured", UnavailableProvider())
    return registry
