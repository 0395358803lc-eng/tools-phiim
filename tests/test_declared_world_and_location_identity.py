from flow_story_studio.engines.analyzer import analyze_story
from flow_story_studio.models import AnalyzeRequest, VideoSettings


def test_declared_world_is_authoritative_and_location_qualifiers_stay_distinct() -> None:
    script = """
TARGET RUNTIME: 32 seconds

CHARACTERS
- ALEX, adult man.
- MAYA, adult woman.

PROPS
- Blue ticket.
- Yellow umbrella.

SCENE 1 — ALEX'S APARTMENT — NIGHT
Alex sits alone. MAYA (THROUGH THE PHONE) says, "Stay home."

SCENE 2 — STATION CONTROL ROOM — NIGHT
Alex enters the control room. A staff member is mentioned in narration but is not a character.

SCENE 3 — MAYA'S APARTMENT — NIGHT
Maya stands by the window with the yellow umbrella near the door.

SCENE 4 — OUTSIDE THE STATION — NIGHT
Alex walks into the rain.
"""
    project = analyze_story(
        AnalyzeRequest(
            name="declared world authority",
            original_text=script,
            settings=VideoSettings(scene_duration=8),
        )
    )

    assert [item.name.casefold() for item in project.characters] == ["alex", "maya"]
    assert [item.name.casefold() for item in project.props] == ["blue ticket", "yellow umbrella"]

    locations = [item.name.casefold() for item in project.locations]
    assert "alex's apartment" in locations
    assert "maya's apartment" in locations
    assert "station control room" in locations
    assert "outside the station" in locations
    assert len(locations) >= 4
