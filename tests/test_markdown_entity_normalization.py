from flow_story_studio.engines.analyzer import analyze_story
from flow_story_studio.models import AnalyzeRequest, VideoSettings


def test_markdown_bold_character_declarations_do_not_duplicate_speakers() -> None:
    script = """
**TARGET RUNTIME:** 24 seconds

## CHARACTERS
- **ALEX**, adult man.
- **MAYA**, adult woman.

## PROPS
- **Brass key**, worn and scratched.

## SCENE 1 — APARTMENT — NIGHT
ALEX picks up the brass key.

MAYA
Do not open that door.

## SCENE 2 — HALLWAY — CONTINUOUS
ALEX steps into the hallway while MAYA stays inside the apartment.
"""
    project = analyze_story(
        AnalyzeRequest(
            name="markdown normalization",
            original_text=script,
            settings=VideoSettings(scene_duration=8),
        )
    )

    names = [item.name.casefold() for item in project.characters]
    assert names == ["alex", "maya"]
    assert len(project.characters) == 2
    assert all("*" not in item.name for item in project.characters)
    assert all("*" not in item.name for item in project.props)
