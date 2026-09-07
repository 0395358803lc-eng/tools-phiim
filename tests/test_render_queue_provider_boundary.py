from types import SimpleNamespace

from flow_story_studio.providers.unavailable import UnavailableProvider
from flow_story_studio.render_queue import RenderQueue
from flow_story_studio.storage import ProjectStorage


def test_render_queue_resolves_unknown_provider_to_unavailable(tmp_path) -> None:
    queue = RenderQueue(ProjectStorage(tmp_path / "projects"))
    project = SimpleNamespace(settings=SimpleNamespace(provider="future-renderer"))
    provider = queue._get_provider(project)  # type: ignore[arg-type]
    assert isinstance(provider, UnavailableProvider)
    assert queue.is_provider_configured("unconfigured") is False
    assert queue.is_provider_configured("mock") is True
