from flow_story_studio.engines.analyzer import analyze_story
from flow_story_studio.engines.segmenter import narrative_text, segment_story, speaking_duration
from flow_story_studio.models import AnalyzeRequest, VideoSettings

STORY = (
    "Người đàn ông bước vào cửa hàng trong cơn mưa. "
    "Anh đặt chiếc điện thoại lên bàn và nhìn quanh. "
    "Người phụ nữ nói: “Tôi đã chờ anh rất lâu.” Sau đó, cả hai cùng nhìn ra cửa sổ. "
    "Ngoài đường, ánh đèn phản chiếu trên mặt đường ướt và câu chuyện tiếp tục."
)


def test_segment_story_respects_duration_budget() -> None:
    scenes = segment_story(STORY, 8)
    assert len(scenes) >= 3
    assert all(item.strip() for item in scenes)
    assert speaking_duration("một hai ba bốn năm") >= 4


def test_analysis_builds_complete_continuity_project() -> None:
    project = analyze_story(
        AnalyzeRequest(name="Demo", original_text=STORY, settings=VideoSettings())
    )
    assert project.scenes
    assert project.characters
    assert project.locations
    assert any(item.name == "Điện thoại" for item in project.props)
    assert project.scenes[0].flow_prompt.startswith("SCENE ID: SCENE_001")
    assert "Avoid:" in project.scenes[0].flow_prompt
    assert project.scenes[1].start_state == project.scenes[0].end_state
    assert project.continuity_score == 100
    assert all(scene.ai_locked for scene in project.scenes)


def test_markdown_front_matter_is_not_converted_to_visual_scenes() -> None:
    screenplay = """# KỊCH BẢN PHIM NGẮN: **CUỘC GỌI**

**Thể loại:** Tâm lý / Bí ẩn
**Thời lượng dự kiến:** 10 phút

## 1. NHÂN VẬT
Minh là lập trình viên 31 tuổi, sống khép kín.
Giọng: nam trầm. Ft. tiếng mưa.

## 3. KỊCH BẢN CHI TIẾT
### CẢNH 1: CĂN HỘ
Minh ngồi trước máy tính trong căn hộ tối. Điện thoại bất ngờ đổ chuông.

### CẢNH 2: CUỘC GỌI
Minh nhấc điện thoại và lắng nghe trong im lặng.
"""
    cleaned = narrative_text(screenplay)
    scenes = segment_story(screenplay, 8)

    assert "Thể loại" not in cleaned
    assert "lập trình viên 31 tuổi" not in cleaned
    assert "Giọng:" not in cleaned
    assert scenes
    assert all("KỊCH BẢN" not in scene for scene in scenes)
    assert any("[SCENE CONTEXT] CẢNH 1" in scene for scene in scenes)
