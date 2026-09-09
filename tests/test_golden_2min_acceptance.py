from pathlib import Path

from flow_story_studio.engines.analyzer import analyze_story
from flow_story_studio.models import AnalyzeRequest, VideoSettings

FIXTURE = Path(__file__).parent / "fixtures" / "golden_2min_screenplay.md"


def _project():
    return analyze_story(
        AnalyzeRequest(
            name="Golden 2min acceptance",
            original_text=FIXTURE.read_text(encoding="utf-8"),
            settings=VideoSettings(
                scene_duration=8,
                analysis_provider="offline",
            ),
        )
    )


def _scene(project, number: int):
    needle = f"CẢNH {number} "
    return next(scene for scene in project.scenes if needle in scene.source_text)


def _events(scene, entity_id: str, action: str):
    return [
        event
        for event in scene.semantic_truth.prop_events
        if event.entity_id == entity_id and event.action == action
    ]


def test_golden_two_minute_screenplay_passes_all_32_production_invariants():
    project = _project()
    props = {item.name: item.id for item in project.props}
    chars = {item.name: item.id for item in project.characters}

    ticket = props["Chiếc vé tàu giấy màu xanh nhạt"]
    recorder = props["Máy ghi âm nhỏ màu bạc"]
    scarf = props["Chiếc khăn quàng màu đỏ"]
    key_prop = props["Chìa khóa đồng nhỏ có khắc số 17"]
    minh = chars["MINH"]
    lan = chars["LAN"]
    bao = chars["BẢO"]

    checks: list[tuple[str, bool]] = []

    def check(name: str, condition: bool) -> None:
        checks.append((name, bool(condition)))

    check("01 authored scene count", len(project.scenes) == 15)
    check("02 exact runtime", sum(scene.duration for scene in project.scenes) == 120)
    check(
        "03 semantic ready 100",
        project.semantic_readiness.status == "Ready"
        and project.semantic_readiness.score == 100
        and not project.semantic_readiness.blockers,
    )
    check(
        "04 continuity 100",
        project.continuity_score == 100 and not project.continuity_warnings,
    )
    check(
        "05 unique render contracts",
        len({scene.render_contract_hash for scene in project.scenes}) == len(project.scenes),
    )

    s1 = _scene(project, 1)
    check(
        "06 scene1 pickup and pocket",
        bool(_events(s1, ticket, "pick_up"))
        and bool(_events(s1, ticket, "put_away"))
        and s1.semantic_truth.exit_props[ticket].owner_id == minh
        and s1.semantic_truth.exit_props[ticket].container == "pocket",
    )
    check(
        "07 scene1 negatives absent",
        all(
            prop_id not in s1.semantic_truth.entry_props
            and prop_id not in s1.semantic_truth.exit_props
            for prop_id in (recorder, scarf)
        ),
    )

    s2 = _scene(project, 2)
    check(
        "08 scene2 ticket remains pocketed",
        ticket in s2.semantic_truth.entry_props
        and s2.semantic_truth.entry_props[ticket].owner_id == minh
        and s2.semantic_truth.entry_props[ticket].container == "pocket",
    )
    check(
        "09 scene2 negative props absent",
        all(
            prop_id not in s2.semantic_truth.entry_props
            and prop_id not in s2.semantic_truth.exit_props
            for prop_id in (recorder, scarf, key_prop)
        ),
    )

    s3 = _scene(project, 3)
    ticket_actions = [
        event.action for event in s3.semantic_truth.prop_events if event.entity_id == ticket
    ]
    check(
        "10 scene3 full transient lifecycle",
        ticket_actions == ["place", "pick_up", "transfer", "put_away"],
    )

    s4 = _scene(project, 4)
    check(
        "11 scene4 recorder seat then pickup",
        recorder in s4.semantic_truth.entry_props
        and s4.semantic_truth.entry_props[recorder].owner_id == ""
        and ":seat" in s4.semantic_truth.entry_props[recorder].location_id
        and bool(_events(s4, recorder, "pick_up"))
        and s4.semantic_truth.exit_props[recorder].owner_id == minh,
    )

    s5 = _scene(project, 5)
    check(
        "12 scene5 dialogue-only recorder absent",
        recorder not in s5.semantic_truth.entry_props
        and recorder not in s5.semantic_truth.exit_props,
    )
    check(
        "13 scene5 scarf on chair",
        scarf in s5.semantic_truth.entry_props
        and s5.semantic_truth.entry_props[scarf].owner_id == ""
        and ":seat" in s5.semantic_truth.entry_props[scarf].location_id,
    )

    s6 = _scene(project, 6)
    check(
        "14 scene6 current recorder physical",
        recorder in s6.semantic_truth.entry_props
        and s6.semantic_truth.entry_props[recorder].owner_id == minh
        and s6.semantic_truth.entry_props[recorder].scope == "physical_world",
    )
    check(
        "15 scene6 CCTV props do not leak",
        scarf not in s6.semantic_truth.entry_props
        and key_prop not in s6.semantic_truth.entry_props
        and s6.semantic_truth.perceptual_entities.get(scarf) == "cctv"
        and s6.semantic_truth.perceptual_entities.get(key_prop) == "cctv",
    )

    s7 = _scene(project, 7)
    check(
        "16 scene7 afternoon not 23:17",
        s7.semantic_truth.temporal.daypart == "afternoon"
        and "afternoon" in s7.start_state.time
        and "23:17" not in s7.start_state.time,
    )
    check(
        "17 scene7 key Lan to Bao",
        any(
            event.source_owner_id == lan and event.target_owner_id == bao
            for event in _events(s7, key_prop, "transfer")
        ),
    )

    s8 = _scene(project, 8)
    fragment_id = f"{ticket}::right_corner_fragment"
    check(
        "18 scene8 whole ticket before tear",
        ticket in s8.semantic_truth.entry_props
        and s8.semantic_truth.entry_props[ticket].part == "whole"
        and s8.semantic_truth.entry_props[ticket].condition == "intact"
        and s8.semantic_truth.entry_props[ticket].owner_id == lan,
    )
    check(
        "19 scene8 remainder and fragment split correctly",
        ticket in s8.semantic_truth.exit_props
        and s8.semantic_truth.exit_props[ticket].owner_id == minh
        and s8.semantic_truth.exit_props[ticket].condition == "missing_right_corner"
        and s8.semantic_truth.exit_props[ticket].container == "pocket"
        and fragment_id in s8.semantic_truth.exit_part_instances
        and s8.semantic_truth.exit_part_instances[fragment_id].owner_id == lan
        and s8.semantic_truth.exit_part_instances[fragment_id].part == "right_corner_fragment",
    )
    check(
        "20 scene8 key stored in drawer",
        key_prop in s8.semantic_truth.exit_props
        and ":drawer" in s8.semantic_truth.exit_props[key_prop].location_id
        and s8.semantic_truth.exit_props[key_prop].owner_id == "",
    )

    s9 = _scene(project, 9)
    check(
        "21 scene9 recorder Lan to Minh",
        any(
            event.source_owner_id == lan and event.target_owner_id == minh
            for event in _events(s9, recorder, "transfer")
        ),
    )
    check(
        "22 scene9 scarf stays with Lan",
        scarf in s9.semantic_truth.entry_props
        and s9.semantic_truth.entry_props[scarf].owner_id == lan
        and not _events(s9, scarf, "transfer"),
    )

    s10 = _scene(project, 10)
    check(
        "23 scene10 ticket missing corner pocketed",
        ticket in s10.semantic_truth.entry_props
        and s10.semantic_truth.entry_props[ticket].condition == "missing_right_corner"
        and s10.semantic_truth.entry_props[ticket].container == "pocket",
    )

    s11 = _scene(project, 11)
    check(
        "24 scene11 one damaged remainder before split",
        ticket in s11.semantic_truth.entry_props
        and s11.semantic_truth.entry_props[ticket].condition == "missing_right_corner"
        and s11.semantic_truth.entry_props[ticket].piece_count == 1,
    )
    check(
        "25 scene11 two pieces on floor after split",
        ticket in s11.semantic_truth.exit_props
        and s11.semantic_truth.exit_props[ticket].condition == "torn_in_two"
        and s11.semantic_truth.exit_props[ticket].piece_count == 2
        and ":floor" in s11.semantic_truth.exit_props[ticket].location_id,
    )

    s12 = _scene(project, 12)
    check(
        "26 scene12 right-corner fragment only",
        ticket not in s12.semantic_truth.entry_props
        and fragment_id in s12.semantic_truth.entry_part_instances
        and s12.semantic_truth.entry_part_instances[fragment_id].part == "right_corner_fragment"
        and ":table" in s12.semantic_truth.entry_part_instances[fragment_id].location_id
        and s12.semantic_truth.entry_part_instances[fragment_id].owner_id == "",
    )
    check(
        "27 scene12 key absent despite dialogue",
        key_prop not in s12.semantic_truth.entry_props
        and key_prop not in s12.semantic_truth.exit_props,
    )

    s13 = _scene(project, 13)
    check(
        "28 scene13 CCTV objects stay nonphysical",
        all(
            prop_id not in s13.semantic_truth.entry_props
            for prop_id in (scarf, key_prop, ticket, recorder)
        )
        and s13.semantic_truth.perceptual_entities.get(scarf) == "cctv"
        and s13.semantic_truth.perceptual_entities.get(key_prop) == "cctv",
    )

    s14 = _scene(project, 14)
    check(
        "29 scene14 key drawer to Bao to Minh",
        key_prop in s14.semantic_truth.entry_props
        and ":drawer" in s14.semantic_truth.entry_props[key_prop].location_id
        and any(
            event.source_owner_id == bao and event.target_owner_id == minh
            for event in _events(s14, key_prop, "transfer")
        )
        and s14.semantic_truth.exit_props[key_prop].owner_id == minh,
    )
    check(
        "30 scene14 recorder remains remote",
        recorder not in s14.semantic_truth.entry_props
        and recorder not in s14.semantic_truth.exit_props,
    )

    s15 = _scene(project, 15)
    check(
        "31 scene15 Minh carries key only",
        key_prop in s15.semantic_truth.entry_props
        and s15.semantic_truth.entry_props[key_prop].owner_id == minh
        and ticket not in s15.semantic_truth.entry_props
        and not s15.semantic_truth.entry_part_instances,
    )
    check(
        "32 station lobby aliases unified",
        s3.location_id == s10.location_id == s15.location_id,
    )

    failures = [name for name, ok in checks if not ok]
    assert len(checks) == 32
    assert not failures, "Golden 2-minute invariant failures: " + " | ".join(failures)
