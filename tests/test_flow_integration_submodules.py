"""Tests for flow_integration submodules."""

from pathlib import Path

from flow_story_studio.flow_integration import FlowCLIIntegration
from flow_story_studio.flow_integration.browser import (
    can_attach_existing_chrome,
    is_flow_cookie_domain,
)
from flow_story_studio.flow_integration.catalog import VIDEO_MODELS
from flow_story_studio.flow_integration.errors import FlowIntegrationError, RenderCheckpoint


class TestCatalog:
    def test_video_models_are_populated(self) -> None:
        assert len(VIDEO_MODELS) > 0
        assert all(m.id for m in VIDEO_MODELS)
        assert all(m.display_name for m in VIDEO_MODELS)

    def test_default_model_is_veo_3_1_lite_lower_priority(self) -> None:
        assert VIDEO_MODELS[0].id == "veo-3.1-lite-lower-priority"

    def test_all_models_have_notes(self) -> None:
        for model in VIDEO_MODELS:
            assert hasattr(model, "note")


class TestErrors:
    def test_flow_integration_error_is_runtime_error(self) -> None:
        error = FlowIntegrationError("test message")
        assert isinstance(error, RuntimeError)
        assert str(error) == "test message"

    def test_render_checkpoint_is_callable_type(self) -> None:
        assert callable(RenderCheckpoint)


class TestBrowser:
    def test_flow_cookie_domain_includes_labs_google(self) -> None:
        assert is_flow_cookie_domain("labs.google")
        assert is_flow_cookie_domain(".labs.google")
        assert is_flow_cookie_domain("flow.google.com")
        assert is_flow_cookie_domain(".google.com")
        assert not is_flow_cookie_domain("googleusercontent.com")

    def test_can_attach_existing_chrome_returns_false_for_missing_file(
        self, tmp_path: Path
    ) -> None:
        result = can_attach_existing_chrome(tmp_path / "nonexistent")
        assert result is False

    def test_can_attach_existing_chrome_returns_false_for_empty_file(
        self, tmp_path: Path
    ) -> None:
        port_file = tmp_path / "DevToolsActivePort"
        port_file.write_text("", encoding="utf-8")
        result = can_attach_existing_chrome(port_file)
        assert result is False

    def test_can_attach_existing_chrome_returns_false_for_invalid_port(
        self, tmp_path: Path
    ) -> None:
        port_file = tmp_path / "DevToolsActivePort"
        port_file.write_text("not_a_number\n", encoding="utf-8")
        result = can_attach_existing_chrome(port_file)
        assert result is False

    def test_can_attach_existing_chrome_returns_false_for_unreachable_port(
        self, tmp_path: Path
    ) -> None:
        port_file = tmp_path / "DevToolsActivePort"
        port_file.write_text("99999\n\n", encoding="utf-8")
        result = can_attach_existing_chrome(port_file)
        assert result is False

    def test_can_attach_existing_chrome_parses_valid_port_file(
        self, tmp_path: Path
    ) -> None:
        port_file = tmp_path / "DevToolsActivePort"
        port_file.write_text("99998\n/devtools/browser/\n", encoding="utf-8")
        result = can_attach_existing_chrome(port_file)
        assert result is False


class TestDiscovery:
    def test_discovery_module_imports(self) -> None:
        from flow_story_studio.flow_integration.discovery import _flow_cli_available

        assert callable(_flow_cli_available)


class TestSession:
    def test_flow_session_initialization(self, tmp_path: Path) -> None:
        from flow_story_studio.flow_integration.session import FlowSession

        session = FlowSession(tmp_path)
        assert session.data_root == tmp_path.resolve()
        assert session.vault is not None
        assert session.configured is False

    def test_flow_session_configured_property(self, tmp_path: Path) -> None:
        from flow_story_studio.flow_integration.session import FlowSession

        session = FlowSession(tmp_path)
        assert session.configured is False

    def test_session_disconnect(self, tmp_path: Path) -> None:
        from flow_story_studio.flow_integration.session import FlowSession

        session = FlowSession(tmp_path)
        session.disconnect()
        assert session.configured is False


class TestIntegrationFacade:
    def test_integration_exports_errors(self) -> None:
        from flow_story_studio.flow_integration import FlowIntegrationError

        assert FlowIntegrationError is not None

    def test_integration_exports_video_models(self) -> None:
        from flow_story_studio.flow_integration import VIDEO_MODELS

        assert len(VIDEO_MODELS) > 0

    def test_flow_cli_integration_has_required_attributes(self, tmp_path: Path) -> None:
        integration = FlowCLIIntegration(tmp_path)
        assert hasattr(integration, "data_root")
        assert hasattr(integration, "session")
        assert hasattr(integration, "timeout")
        assert hasattr(integration, "configured")
        assert hasattr(integration, "vault")


def test_integration_falls_back_to_consent_enabled_user_chrome(
    tmp_path: Path, monkeypatch
) -> None:
    from flow_story_studio.flow_integration import integration as integration_module

    local_app_data = tmp_path / "Local"
    user_port = (
        local_app_data
        / "Google"
        / "Chrome"
        / "User Data"
        / "DevToolsActivePort"
    )
    user_port.parent.mkdir(parents=True, exist_ok=True)
    user_port.write_text("9222\n/devtools/browser/test\n", encoding="utf-8")

    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.setenv("FLOW_VIDEO_TRANSPORT", "legacy")
    monkeypatch.delenv("FLOW_CHROME_DEVTOOLS_ACTIVE_PORT", raising=False)

    expected = user_port.resolve()

    def fake_can_attach(path: Path) -> bool:
        return Path(path).resolve() == expected

    monkeypatch.setattr(
        integration_module,
        "can_attach_existing_chrome",
        fake_can_attach,
    )

    integration = FlowCLIIntegration(tmp_path / "data")

    assert integration._chrome_port_file == expected
