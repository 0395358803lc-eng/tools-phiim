from flow_story_studio.analysis_providers.audio_finalization import (
    finalize_audio,
    parse_screenplay_audio,
)
from flow_story_studio.engines.analyzer import analyze_story
from flow_story_studio.models import AnalyzeRequest, Dialogue, VideoSettings


def _script() -> str:
    return """
TARGET RUNTIME: 40 seconds

CHARACTERS
- ALEX, adult man.
- MAYA, adult woman.

SCENE 1 — APARTMENT — NIGHT
MAYA — INCOMING CALL
Alex looks at the phone but does not answer yet.

ALEX
I am here.

MAYA (THROUGH THE PHONE)
Do not leave.

Maya is not in the apartment.

SCENE 2 — PLATFORM — NIGHT
VOICE OF ALEX IN THE RECORDER
If I hear this, I forgot again.

VOICE OF MAYA IN THE RECORDER
Do not board the train.

SCENE 3 — OFFICE — NIGHT
ALEX
I remember now.

MAYA
Then stay there.

Maya looks at the door.

SCENE 4 — HALL — NIGHT
A text message from Alex: "This is written text, not spoken dialogue."

SCENE 5 — ROOM — NIGHT
NARRATOR (V.O.)
The lights finally go out.
"""


def test_parse_screenplay_audio_classifies_spoken_channels_without_quotes() -> None:
    project = analyze_story(
        AnalyzeRequest(
            name="audio parser",
            original_text=_script(),
            settings=VideoSettings(scene_duration=8),
        )
    )
    events = parse_screenplay_audio(project.original_text, project.characters)
    spoken = [
        (event.scene_number, event.speaker_id, event.text, event.kind, event.delivery)
        for event in events
    ]

    alex = next(item for item in project.characters if item.name.casefold() == "alex")
    maya = next(item for item in project.characters if item.name.casefold() == "maya")

    assert (1, alex.id, "I am here.", "dialogue", "onscreen") in spoken
    assert (1, maya.id, "Do not leave.", "dialogue", "phone") in spoken
    assert (2, alex.id, "If I hear this, I forgot again.", "dialogue", "recorded") in spoken
    assert (2, maya.id, "Do not board the train.", "dialogue", "recorded") in spoken
    assert (3, alex.id, "I remember now.", "dialogue", "onscreen") in spoken
    assert (3, maya.id, "Then stay there.", "dialogue", "onscreen") in spoken
    assert any(
        event.kind == "voiceover" and event.text == "The lights finally go out." for event in events
    )
    assert all("written text" not in event.text for event in events)
    assert all("Maya looks at the door" not in event.text for event in events)
    assert all("looks at the phone" not in event.text for event in events)


def test_finalize_audio_overwrites_ai_hallucinations_and_preserves_source_speakers() -> None:
    project = analyze_story(
        AnalyzeRequest(
            name="audio finalizer",
            original_text=_script(),
            settings=VideoSettings(scene_duration=8),
        )
    )
    alex = next(item for item in project.characters if item.name.casefold() == "alex")
    maya = next(item for item in project.characters if item.name.casefold() == "maya")

    for scene in project.scenes:
        scene.voiceover = "AI invented narration and action text."
        scene.dialogues = [Dialogue(character_id=maya.id, text="AI invented dialogue.")]
        scene.warnings.append("Voiceover có thể dài hơn thời lượng cảnh")

    finalized = finalize_audio(project)

    scene1 = next(scene for scene in finalized.scenes if "SCENE 1" in scene.source_text)
    scene2 = next(scene for scene in finalized.scenes if "SCENE 2" in scene.source_text)
    scene3 = next(scene for scene in finalized.scenes if "SCENE 3" in scene.source_text)
    scene4 = next(scene for scene in finalized.scenes if "SCENE 4" in scene.source_text)
    scene5 = next(scene for scene in finalized.scenes if "SCENE 5" in scene.source_text)

    assert [(item.character_id, item.text) for item in scene1.dialogues] == [
        (alex.id, "I am here."),
        (maya.id, "Do not leave."),
    ]
    assert [(item.character_id, item.text) for item in scene2.dialogues] == [
        (alex.id, "If I hear this, I forgot again."),
        (maya.id, "Do not board the train."),
    ]
    assert [item.delivery for item in scene1.dialogues] == ["onscreen", "phone"]
    assert [item.delivery for item in scene2.dialogues] == ["recorded", "recorded"]
    assert [(item.character_id, item.text) for item in scene3.dialogues] == [
        (alex.id, "I remember now."),
        (maya.id, "Then stay there."),
    ]
    assert scene4.dialogues == []
    assert scene4.voiceover == ""
    assert scene5.dialogues == []
    assert scene5.voiceover == "The lights finally go out."
    assert all(
        "Voiceover" not in warning for scene in finalized.scenes for warning in scene.warnings
    )


def test_vietnamese_caller_id_is_not_speaker_dialogue() -> None:
    script = """
TARGET RUNTIME: 8 seconds

CHARACTERS
- KHẢI, adult man.
- AN, adult woman.

SCENE 1 — APARTMENT — NIGHT
Màn hình hiện:

**AN — CUỘC GỌI ĐẾN**

Khải nhìn điện thoại nhưng chưa bắt máy.

AN (QUA ĐIỆN THOẠI)
Anh đừng đến nhà ga tối nay.

KHẢI
Em đang ở đâu?
"""
    project = analyze_story(
        AnalyzeRequest(
            name="vietnamese caller id",
            original_text=script,
            settings=VideoSettings(scene_duration=8),
        )
    )
    events = parse_screenplay_audio(project.original_text, project.characters)
    assert [event.text for event in events] == [
        "Anh đừng đến nhà ga tối nay.",
        "Em đang ở đâu?",
    ]

def test_flow_prompt_contains_explicit_source_grounded_delivery_channels() -> None:
    project = analyze_story(
        AnalyzeRequest(
            name="audio delivery prompt",
            original_text=_script(),
            settings=VideoSettings(scene_duration=8),
        )
    )
    scene1 = next(scene for scene in project.scenes if "SCENE 1" in scene.source_text)
    scene2 = next(scene for scene in project.scenes if "SCENE 2" in scene.source_text)

    assert "[delivery: phone;" in scene1.flow_prompt
    assert "[delivery: recorded;" in scene2.flow_prompt
    assert 'MAYA: "Do not leave."' in scene1.flow_prompt
    assert 'ALEX: "If I hear this, I forgot again."' in scene2.flow_prompt
