"""Flow video model catalog shown by the integration API."""

from __future__ import annotations

from ..flow_ui_contract import DEFAULT_FLOW_VIDEO_MODEL, DEFAULT_FLOW_VIDEO_MODEL_LABEL
from ..models import FlowVideoModel

VIDEO_MODELS = [
    FlowVideoModel(
        id=DEFAULT_FLOW_VIDEO_MODEL,
        display_name=DEFAULT_FLOW_VIDEO_MODEL_LABEL,
        note="Mặc định an toàn: Lower Priority; không fallback sang model tốn credits",
    ),
    FlowVideoModel(id="veo-3.1-fast", display_name="Veo 3.1 Fast", note="Nhanh"),
    FlowVideoModel(id="veo-3.1", display_name="Veo 3.1 Quality", note="Chất lượng cao"),
    FlowVideoModel(id="veo-3-fast", display_name="Veo 3 Fast", note="Nhanh"),
    FlowVideoModel(id="veo-3", display_name="Veo 3 Quality", note="Chất lượng cao"),
    FlowVideoModel(id="veo-3-lite", display_name="Veo 3 Lite", note="Tiết kiệm credit"),
    FlowVideoModel(id="veo-3.1-lite", display_name="Veo 3.1 Lite", note="Frames mode"),
    FlowVideoModel(id="veo-2-fast", display_name="Veo 2 Fast", note="Tương thích"),
    FlowVideoModel(id="veo-2", display_name="Veo 2", note="Tương thích"),
]