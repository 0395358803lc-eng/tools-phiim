import pytest

from flow_story_studio.providers.unavailable import (
    RenderProviderUnavailable,
    UnavailableProvider,
)


@pytest.mark.asyncio
async def test_unavailable_provider_is_controlled() -> None:
    provider = UnavailableProvider()
    health = await provider.health()
    assert health["configured"] is False
    assert health["provider"] == "unconfigured"
    with pytest.raises(RenderProviderUnavailable):
        await provider.generate(None, None)  # type: ignore[arg-type]
