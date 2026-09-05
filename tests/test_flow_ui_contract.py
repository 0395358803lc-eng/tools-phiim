import pytest

from flow_story_studio.flow_integration import VIDEO_MODELS, FlowCLIIntegration
from flow_story_studio.flow_ui_contract import (
    DEFAULT_FLOW_VIDEO_MODEL,
    FlowUIContractError,
    assert_safe_video_model,
    choose_model_candidate,
    is_lower_priority_model_label,
    model_matches_contract,
    normalize_flow_label,
)
from flow_story_studio.models import VideoSettings


@pytest.mark.parametrize(
    "label",
    [
        "Veo 3.1 - Lite [Lower Priority]",
        "Veo 3.1 Lite [Lower Priority]",
        "Veo 3.1 - Lite · Lower Priority",
        "arrow_drop_down Veo 3.1 Lite Lower Priority",
    ],
)
def test_lower_priority_model_label_survives_ui_punctuation_drift(label: str) -> None:
    assert is_lower_priority_model_label(label)
    assert model_matches_contract(DEFAULT_FLOW_VIDEO_MODEL, label)


@pytest.mark.parametrize(
    "label",
    [
        "Veo 3.1 - Lite",
        "Veo 3.1 - Fast",
        "Veo 3.1 - Quality",
        "Omni 1.1 Flash",
        "Veo 3.1 Lite [Lower Priority] Fast",
    ],
)
def test_lower_priority_contract_rejects_credited_or_wrong_models(label: str) -> None:
    assert not is_lower_priority_model_label(label)


def test_model_candidate_selection_is_unique_and_collision_safe() -> None:
    labels = [
        "Veo 3.1 - Lite",
        "Veo 3.1 - Fast",
        "Veo 3.1 - Lite [Lower Priority]",
        "Veo 3.1 - Quality",
    ]
    selected = choose_model_candidate(DEFAULT_FLOW_VIDEO_MODEL, labels)
    assert selected.index == 2
    assert "Lower Priority" in selected.text


def test_model_candidate_selection_fails_closed_when_missing_or_ambiguous() -> None:
    with pytest.raises(FlowUIContractError, match="not present"):
        choose_model_candidate(
            DEFAULT_FLOW_VIDEO_MODEL,
            ["Veo 3.1 - Lite", "Veo 3.1 - Fast"],
        )
    with pytest.raises(FlowUIContractError, match="ambiguous"):
        choose_model_candidate(
            DEFAULT_FLOW_VIDEO_MODEL,
            [
                "Veo 3.1 - Lite [Lower Priority]",
                "Veo 3.1 Lite · Lower Priority",
            ],
        )


def test_default_video_settings_and_catalog_pin_lower_priority() -> None:
    settings = VideoSettings()
    assert settings.video_model == DEFAULT_FLOW_VIDEO_MODEL
    assert VIDEO_MODELS[0].id == DEFAULT_FLOW_VIDEO_MODEL
    assert "Lower Priority" in VIDEO_MODELS[0].display_name


def test_paid_models_are_blocked_without_explicit_escape_hatch() -> None:
    assert assert_safe_video_model(DEFAULT_FLOW_VIDEO_MODEL) == DEFAULT_FLOW_VIDEO_MODEL
    with pytest.raises(FlowUIContractError, match="Paid/credited"):
        assert_safe_video_model("veo-3.1-fast")
    assert assert_safe_video_model("veo-3.1-fast", allow_paid=True) == "veo-3.1-fast"


def test_flow_compatibility_injects_lower_priority_aliases(tmp_path) -> None:
    import flow_cli._flow_ui as flow_ui

    FlowCLIIntegration(tmp_path)._apply_flow_ui_compatibility()
    labels = flow_ui.VIDEO_MODEL_UI_LABELS[DEFAULT_FLOW_VIDEO_MODEL]
    assert any(model_matches_contract(DEFAULT_FLOW_VIDEO_MODEL, item) for item in labels)


def test_normalization_removes_material_symbols_without_loosening_identity() -> None:
    normalized = normalize_flow_label(
        "check arrow_drop_down Veo 3.1 - Lite [Lower Priority]"
    )
    assert normalized == "veo 3.1 lite lower priority"
