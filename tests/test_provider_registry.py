from flow_story_studio.providers.mock import MockProvider
from flow_story_studio.providers.registry import ProviderRegistry, build_default_registry


def test_default_registry_keeps_neutral_boundary() -> None:
    registry = build_default_registry()
    assert registry.names() == ["mock", "unconfigured"]
    assert registry.configured_names() == ["mock"]


def test_registry_accepts_future_provider_without_core_changes() -> None:
    registry = ProviderRegistry()
    provider = MockProvider()
    registry.register("future-renderer", provider)
    assert registry.get("future-renderer") is provider
