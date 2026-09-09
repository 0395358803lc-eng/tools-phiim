from flow_story_studio.engines.analyzer import analyze_story
from flow_story_studio.models import AnalyzeRequest, VideoSettings


def _project():
    script = """
TARGET RUNTIME: 40 seconds

NHÂN VẬT
- KHẢI, nam, 35 tuổi.
- AN, nữ, 31 tuổi.

ĐẠO CỤ
- Chiếc vé tàu giấy màu xanh nhạt.
- Máy ghi âm nhỏ màu bạc.
- Chiếc ô màu vàng.

CẢNH 1 — SÂN GA — CHIỀU — FLASHBACK
Khải nhìn đồng hồ đeo tay. Đồng hồ chỉ 23:17.

CẢNH 2 — SẢNH NHÀ GA CŨ — CHIỀU — FLASHBACK
An lấy chiếc vé tàu giấy màu xanh nhạt từ túi áo. Cô xé một góc nhỏ ở phía phải.
Cô đưa chiếc vé cho Khải.

CẢNH 3 — SẢNH NHÀ GA CŨ — FLASHBACK — LIÊN TỤC
An đặt máy ghi âm nhỏ màu bạc vào lòng bàn tay Khải.
Chiếc ô màu vàng nằm cạnh chân An. An không đưa chiếc ô cho Khải.

CẢNH 4 — PHÒNG TRỰC NHÀ GA — ĐÊM — HIỆN TẠI
Camera an ninh tiếp tục chạy. Khải trong video cũ đứng cạnh một vị trí trống.
Trên ghế cạnh anh có chiếc ô màu vàng. Nhưng không có ai cầm nó.
Khải hiện tại cầm máy ghi âm nhỏ màu bạc trong tay.

CẢNH 5 — SẢNH NHÀ GA — ĐÊM — HIỆN TẠI
Khải lấy chiếc vé tàu giấy màu xanh nhạt có góc phải bị rách ra khỏi túi.
Sau đó Khải xé chiếc vé làm đôi. Hai mảnh vé rơi xuống sàn.
"""
    return analyze_story(
        AnalyzeRequest(
            name="semantic readiness regression",
            original_text=script,
            settings=VideoSettings(scene_duration=8),
        )
    )


def test_diegetic_watch_clock_does_not_override_scene_daypart():
    project = _project()
    scene = project.scenes[0]

    assert scene.semantic_truth.temporal.daypart == "afternoon"
    assert scene.semantic_truth.temporal.diegetic_clock_observations["watch"] == "23:17"
    assert scene.start_state.time == "Flashback — afternoon"
    assert scene.end_state.time == "Flashback — afternoon"


def test_prop_transformations_are_applied_after_entry_state():
    project = _project()
    ticket = next(item for item in project.props if "vé" in item.name.casefold())
    scene = project.scenes[1]

    entry = scene.semantic_truth.entry_props[ticket.id]
    exit_state = scene.semantic_truth.exit_props[ticket.id]
    assert entry.condition == "intact"
    assert exit_state.condition == "missing_right_corner"
    assert exit_state.owner_id in scene.characters


def test_transfer_event_changes_recorder_owner_and_ignores_negated_umbrella_transfer():
    project = _project()
    recorder = next(item for item in project.props if "ghi âm" in item.name.casefold())
    umbrella = next(item for item in project.props if "ô" in item.name.casefold())
    scene = project.scenes[2]

    entry = scene.semantic_truth.entry_props[recorder.id]
    exit_state = scene.semantic_truth.exit_props[recorder.id]
    assert entry.owner_id != exit_state.owner_id
    assert exit_state.owner_id in scene.characters
    assert not any(
        event.entity_id == umbrella.id and event.action == "transfer"
        for event in scene.semantic_truth.prop_events
    )


def test_cctv_prop_does_not_leak_into_physical_room_state():
    project = _project()
    umbrella = next(item for item in project.props if "ô" in item.name.casefold())
    scene = project.scenes[3]

    assert scene.semantic_truth.perceptual_entities[umbrella.id] == "cctv"
    assert umbrella.id not in scene.semantic_truth.entry_props
    assert umbrella.id not in scene.semantic_truth.exit_props


def test_ticket_is_not_pre_torn_before_authored_tear_in_half():
    project = _project()
    ticket = next(item for item in project.props if "vé" in item.name.casefold())
    scene = project.scenes[4]

    entry = scene.semantic_truth.entry_props[ticket.id]
    exit_state = scene.semantic_truth.exit_props[ticket.id]
    assert entry.condition == "missing_right_corner"
    assert entry.piece_count == 1
    assert exit_state.condition == "torn_in_two"
    assert exit_state.piece_count == 2


def test_location_aliases_collapse_to_one_physical_master():
    project = _project()
    station_lobbies = [item for item in project.locations if "sảnh nhà ga" in item.name.casefold()]
    assert len(station_lobbies) == 1
    assert project.scenes[1].location_id == project.scenes[4].location_id


def test_semantic_readiness_gate_is_ready_for_regression_script():
    project = _project()
    assert project.semantic_readiness.status == "Ready", project.semantic_readiness.blockers
    assert project.semantic_readiness.score == 100


def test_present_timeline_prop_condition_persists_across_location_cut():
    script = """
ĐẠO CỤ
- Chiếc vé tàu giấy màu xanh nhạt.

CẢNH 1 — SÂN GA — ĐÊM — HIỆN TẠI
Khải nhìn chiếc vé tàu giấy màu xanh nhạt. Góc phải của chiếc vé bị rách.

CẢNH 2 — SẢNH NHÀ GA — ĐÊM — HIỆN TẠI
Khải lấy chiếc vé tàu giấy màu xanh nhạt ra khỏi túi. Sau đó Khải xé chiếc vé làm đôi.
"""
    project = analyze_story(
        AnalyzeRequest(
            name="present prop ledger",
            original_text=script,
            settings=VideoSettings(scene_duration=8),
        )
    )
    ticket = next(item for item in project.props if "vé" in item.name.casefold())
    tear_scene = next(
        scene
        for scene in project.scenes
        if any(
            event.entity_id == ticket.id
            and event.action == "tear"
            and event.target_condition == "torn_in_two"
            for event in scene.semantic_truth.prop_events
        )
    )
    assert tear_scene.semantic_truth.entry_props[ticket.id].condition == "missing_right_corner"
    assert tear_scene.semantic_truth.entry_props[ticket.id].piece_count == 1
    assert tear_scene.semantic_truth.exit_props[ticket.id].condition == "torn_in_two"
    assert tear_scene.semantic_truth.exit_props[ticket.id].piece_count == 2


def test_bare_vietnamese_ve_word_does_not_spawn_ticket():
    script = """
ĐẠO CỤ
- Chiếc vé tàu giấy màu xanh nhạt.

CẢNH 1 — ĐƯỜNG PHỐ — ĐÊM
Khải mỉm cười. Anh nói: Về nhà.
"""
    project = analyze_story(
        AnalyzeRequest(
            name="ticket alias regression",
            original_text=script,
            settings=VideoSettings(scene_duration=8),
        )
    )
    ticket = next(item for item in project.props if "vé" in item.name.casefold())
    assert all(ticket.id not in scene.semantic_truth.entry_props for scene in project.scenes)
    assert all(ticket.id not in scene.semantic_truth.exit_props for scene in project.scenes)


def test_negative_only_prop_mentions_never_create_physical_state():
    script = """
ĐẠO CỤ
- Máy ghi âm nhỏ màu bạc.
- Chiếc ô màu vàng.

CẢNH 1 — HÀNH LANG — ĐÊM
Khải đi nhanh qua hành lang. Anh không cầm máy ghi âm. Anh không mang chiếc ô màu vàng.
"""
    project = analyze_story(
        AnalyzeRequest(
            name="negative possession regression",
            original_text=script,
            settings=VideoSettings(scene_duration=8),
        )
    )
    prop_ids = {item.id for item in project.props}
    scene = project.scenes[0]
    assert not (prop_ids & set(scene.semantic_truth.entry_props))
    assert not (prop_ids & set(scene.semantic_truth.exit_props))


def test_dialogue_only_prop_mention_does_not_spawn_prop():
    script = """
NHÂN VẬT
- AN, nữ, 31 tuổi.

ĐẠO CỤ
- Máy ghi âm nhỏ màu bạc.

CẢNH 1 — CĂN HỘ CỦA AN — ĐÊM
An ngồi bên cửa sổ và cầm điện thoại.
AN
Anh đã tìm thấy máy ghi âm chưa?
An nhắm mắt.
"""
    project = analyze_story(
        AnalyzeRequest(
            name="dialogue prop regression",
            original_text=script,
            settings=VideoSettings(scene_duration=8),
        )
    )
    recorder = next(item for item in project.props if "ghi âm" in item.name.casefold())
    assert all(recorder.id not in scene.semantic_truth.entry_props for scene in project.scenes)
    assert all(recorder.id not in scene.semantic_truth.exit_props for scene in project.scenes)


def test_pickup_place_and_pocket_actions_are_typed_events():
    script = """
ĐẠO CỤ
- Chiếc vé tàu giấy màu xanh nhạt.

CẢNH 1 — CĂN HỘ — ĐÊM
Một chiếc vé tàu giấy màu xanh nhạt nằm dưới sàn. Khải nhặt nó lên.
Khải đặt chiếc vé lên bàn. Sau đó Khải bỏ chiếc vé vào túi áo.
"""
    project = analyze_story(
        AnalyzeRequest(
            name="spatial lifecycle regression",
            original_text=script,
            settings=VideoSettings(scene_duration=8),
        )
    )
    ticket = next(item for item in project.props if "vé" in item.name.casefold())
    actions = [
        event.action
        for scene in project.scenes
        for event in scene.semantic_truth.prop_events
        if event.entity_id == ticket.id
    ]
    assert "pick_up" in actions
    assert "place" in actions
    assert "put_away" in actions


def test_ticket_fragment_is_not_the_whole_ticket():
    script = """
ĐẠO CỤ
- Chiếc vé tàu giấy màu xanh nhạt.

CẢNH 1 — CĂN HỘ CỦA AN — ĐÊM
An đứng bên cửa sổ. Trên bàn là một mảnh giấy nhỏ.
Cận cảnh: GÓC PHẢI CỦA CHIẾC VÉ XANH. An nhìn mảnh giấy.
"""
    project = analyze_story(
        AnalyzeRequest(
            name="part identity regression",
            original_text=script,
            settings=VideoSettings(scene_duration=8),
        )
    )
    ticket = next(item for item in project.props if "vé" in item.name.casefold())
    instance_id = f"{ticket.id}::right_corner_fragment"
    scene = next(
        scene
        for scene in project.scenes
        if instance_id in scene.semantic_truth.entry_part_instances
    )
    assert ticket.id not in scene.semantic_truth.entry_props
    assert (
        scene.semantic_truth.entry_part_instances[instance_id].part
        == "right_corner_fragment"
    )
    assert scene.semantic_truth.entry_part_instances[instance_id].condition == "fragment"


def test_quoted_message_does_not_compile_as_physical_transfer():
    script = """
ĐẠO CỤ
- Chiếc vé tàu giấy màu xanh nhạt.

CẢNH 1 — THANG MÁY — ĐÊM
Khải cầm chiếc vé tàu giấy màu xanh nhạt. Điện thoại rung.
Một tin nhắn hiện lên: “Anh đã quên người đưa vé cho anh.”
Khải nhìn hình phản chiếu của mình.
"""
    project = analyze_story(
        AnalyzeRequest(
            name="quoted message regression",
            original_text=script,
            settings=VideoSettings(scene_duration=8),
        )
    )
    assert not any(
        event.action == "transfer"
        for scene in project.scenes
        for event in scene.semantic_truth.prop_events
    )


def test_ai_semantic_fact_requires_source_evidence_and_cannot_override_truth():
    from flow_story_studio.analysis_providers.merging import _sanitize_ai_semantic_proposal
    from flow_story_studio.analysis_providers.source_truth import audit_ai_semantic_proposal
    from flow_story_studio.semantic_readiness import evaluate_semantic_readiness

    script = """
NHÂN VẬT
- KHẢI, nam, 35 tuổi.

ĐẠO CỤ
- Chiếc vé tàu giấy màu xanh nhạt.

CẢNH 1 — SẢNH — ĐÊM
Khải cầm chiếc vé tàu giấy màu xanh nhạt.
"""
    project = analyze_story(
        AnalyzeRequest(
            name="ai semantic proposal regression",
            original_text=script,
            settings=VideoSettings(scene_duration=8),
        )
    )
    scene = project.scenes[0]
    ticket = project.props[0]
    proposal = _sanitize_ai_semantic_proposal(
        scene.source_text,
        {
            "facts": [
                {
                    "entity_id": ticket.id,
                    "fact_type": "ownership",
                    "value": "CHAR_999",
                    "evidence": "Khải cầm chiếc vé tàu giấy màu xanh nhạt.",
                    "confidence": 99,
                },
                {
                    "entity_id": ticket.id,
                    "fact_type": "presence",
                    "value": "present",
                    "evidence": "fabricated evidence",
                    "confidence": 99,
                },
            ],
            "negative_facts": [],
            "uncertainties": [],
        },
        character_map={item.id: item.id for item in project.characters},
        prop_map={item.id: item.id for item in project.props},
        location_map={item.id: item.id for item in project.locations},
        characters=project.characters,
        props=project.props,
        locations=project.locations,
    )
    assert len(proposal.facts) == 1
    assert len(proposal.rejected_facts) == 1

    scene.ai_semantic_proposal = proposal
    audit_ai_semantic_proposal(scene)
    assert "deterministic override" in scene.semantic_truth.source_trace["ai_overrides"]
    report = evaluate_semantic_readiness(project)
    assert report.status == "Ready", report.blockers


def test_ai_semantic_confidence_and_fact_type_are_normalized_without_silent_loss():
    from flow_story_studio.analysis_providers.merging import _sanitize_ai_semantic_proposal

    script = """
ĐẠO CỤ
- Chiếc vé tàu giấy màu xanh nhạt.

CẢNH 1 — SẢNH — ĐÊM
Khải cầm chiếc vé tàu giấy màu xanh nhạt.
"""
    project = analyze_story(
        AnalyzeRequest(
            name="ai semantic normalization regression",
            original_text=script,
            settings=VideoSettings(scene_duration=8),
        )
    )
    scene = project.scenes[0]
    ticket = project.props[0]
    maps = {
        "character_map": {item.id: item.id for item in project.characters},
        "prop_map": {item.id: item.id for item in project.props},
        "location_map": {item.id: item.id for item in project.locations},
        "characters": project.characters,
        "props": project.props,
        "locations": project.locations,
    }

    for confidence in (0.95, 95, "95", "95%"):
        proposal = _sanitize_ai_semantic_proposal(
            scene.source_text,
            {
                "facts": [
                    {
                        "entity_id": ticket.id,
                        "fact_type": "present",
                        "value": "present",
                        "evidence": "Khải cầm chiếc vé tàu giấy màu xanh nhạt.",
                        "confidence": confidence,
                    }
                ],
                "negative_facts": [],
                "uncertainties": [],
            },
            **maps,
        )
        assert len(proposal.facts) == 1
        assert proposal.facts[0].fact_type == "presence"
        assert proposal.facts[0].confidence == 95
        assert proposal.normalization_issues == []

    malformed = _sanitize_ai_semantic_proposal(
        scene.source_text,
        {
            "facts": [
                {
                    "entity_id": ticket.id,
                    "fact_type": "made_up_relation",
                    "value": "x",
                    "evidence": "Khải cầm chiếc vé tàu giấy màu xanh nhạt.",
                    "confidence": 0.8,
                }
            ]
        },
        **maps,
    )
    assert malformed.facts == []
    assert malformed.normalization_issues


def test_monitor_reference_does_not_pull_real_world_prop_into_cctv_scope():
    script = """
TARGET RUNTIME: 10 seconds

NHÂN VẬT
- MINH, nam, 34 tuổi.
- BẢO, nam, 58 tuổi.

ĐẠO CỤ
- Máy ghi âm nhỏ màu bạc.
- Chiếc khăn quàng màu đỏ.
- Chìa khóa đồng nhỏ có khắc số 17.

CẢNH 1 — PHÒNG TRỰC NHÀ GA — ĐÊM — HIỆN TẠI
Minh và Bảo đứng trước màn hình camera an ninh. Máy ghi âm nhỏ màu bạc vẫn ở trong tay Minh.
Trong đoạn video cũ trên màn hình, Lan đeo chiếc khăn quàng màu đỏ và cầm chìa khóa đồng số 17.
Ở phòng trực hiện tại không có khăn quàng đỏ và không có chìa khóa đồng.
Bảo tua đoạn video về sáu tháng trước.
"""
    project = analyze_story(
        AnalyzeRequest(
            name="perceptual scope v2 regression",
            original_text=script,
            settings=VideoSettings(scene_duration=8),
        )
    )
    scene = project.scenes[0]
    recorder = next(item for item in project.props if "ghi âm" in item.name.casefold())
    scarf = next(item for item in project.props if "khăn" in item.name.casefold())

    assert recorder.id in scene.semantic_truth.entry_props
    assert scene.semantic_truth.entry_props[recorder.id].scope == "physical_world"
    assert scarf.id not in scene.semantic_truth.entry_props
    assert scene.semantic_truth.perceptual_entities.get(scarf.id) in {"cctv", "screen"}


def test_ticket_alias_does_not_match_ticket_counter_but_matches_physical_ticket():
    script = """
TARGET RUNTIME: 16 seconds

ĐẠO CỤ
- Chiếc vé tàu giấy màu xanh nhạt.

CẢNH 1 — QUẦY VÉ NHÀ GA — ĐÊM
Bảo quay về phía quầy vé. Trên quầy không có vật gì.

CẢNH 2 — SẢNH NHÀ GA — ĐÊM
Minh cầm chiếc vé xanh trong tay.
"""
    project = analyze_story(
        AnalyzeRequest(
            name="ticket context alias regression",
            original_text=script,
            settings=VideoSettings(scene_duration=8),
        )
    )
    ticket = next(item for item in project.props if "vé" in item.name.casefold())
    first = next(scene for scene in project.scenes if "CẢNH 1" in scene.source_text)
    second = next(scene for scene in project.scenes if "CẢNH 2" in scene.source_text)

    assert ticket.id not in first.semantic_truth.entry_props
    assert ticket.id not in first.semantic_truth.exit_props
    assert ticket.id in second.semantic_truth.entry_props


def test_key_alias_and_missing_corner_condition_variants_are_canonicalized():
    script = """
TARGET RUNTIME: 16 seconds

NHÂN VẬT
- MINH, nam, 34 tuổi.
- BẢO, nam, 58 tuổi.

ĐẠO CỤ
- Chiếc vé tàu giấy màu xanh nhạt.
- Chìa khóa đồng nhỏ có khắc số 17.

CẢNH 1 — QUẦY VÉ — ĐÊM
Bảo cầm chìa khóa đồng số 17 rồi đưa chìa khóa cho Minh.

CẢNH 2 — SẢNH — ĐÊM
Minh lấy phần vé xanh bị thiếu góc phải ra khỏi túi.
"""
    project = analyze_story(
        AnalyzeRequest(
            name="key alias condition regression",
            original_text=script,
            settings=VideoSettings(scene_duration=8),
        )
    )
    key_prop = next(item for item in project.props if "khóa" in item.name.casefold())
    ticket = next(item for item in project.props if "vé" in item.name.casefold())
    key_scene = next(scene for scene in project.scenes if "CẢNH 1" in scene.source_text)
    ticket_scene = next(scene for scene in project.scenes if "CẢNH 2" in scene.source_text)

    assert any(
        event.entity_id == key_prop.id
        and event.action == "transfer"
        and event.target_owner_id
        for event in key_scene.semantic_truth.prop_events
    )
    assert ticket_scene.semantic_truth.entry_props[ticket.id].condition == "missing_right_corner"
