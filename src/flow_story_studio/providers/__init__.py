"""Provider-neutral render and reference contracts."""

from .base import RenderResult, VideoProvider
from .google_flow_browser import GoogleFlowBrowserProvider
from .mock import MockProvider
from .reference import ReferenceProvider
from .registry import ProviderRegistry, build_default_registry
from .unavailable import RenderProviderUnavailable, UnavailableProvider

__all__ = [
    "GoogleFlowBrowserProvider",
    "MockProvider",
    "ProviderRegistry",
    "ReferenceProvider",
    "RenderProviderUnavailable",
    "RenderResult",
    "UnavailableProvider",
    "VideoProvider",
    "build_default_registry",
]
