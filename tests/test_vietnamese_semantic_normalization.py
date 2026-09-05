from flow_story_studio.analysis_providers.semantic_orchestrator import semantic_key


def test_vietnamese_d_stroke_is_semantically_normalized() -> None:
    assert semantic_key("Điện thoại đang gọi đến") == "dien thoai dang goi den"
