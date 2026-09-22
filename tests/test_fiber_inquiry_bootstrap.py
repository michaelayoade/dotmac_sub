"""Fiber Integration Platform bootstrap contract."""

from __future__ import annotations

from uuid import UUID

from app.models.integration_platform import (
    IntegrationCapabilityBinding,
    IntegrationConfigRevision,
    IntegrationInstallation,
)
from scripts.one_off.bootstrap_fiber_inquiry_integration import (
    CAPABILITY_ID,
    BootstrapMode,
    FiberInquiryBootstrapCommand,
    InstallationEnvironment,
    configure_fiber_inquiry_installation,
    fiber_inquiry_callback_path,
)


def _command(mode: BootstrapMode) -> FiberInquiryBootstrapCommand:
    return FiberInquiryBootstrapCommand(
        mode=mode,
        environment=InstallationEnvironment.test,
        name="Fiber Website Inquiry - Test",
        secret_ref="env://FIBER_TEST_SIGNING_SECRET",
    )


def test_apply_is_idempotent_and_enables_one_binding(db_session, monkeypatch) -> None:
    monkeypatch.setenv("FIBER_TEST_SIGNING_SECRET", "test-signing-secret")

    first = configure_fiber_inquiry_installation(
        db_session,
        command=_command(BootstrapMode.apply),
    )
    second = configure_fiber_inquiry_installation(
        db_session,
        command=_command(BootstrapMode.apply),
    )

    assert second.installation_id == first.installation_id
    assert second.binding_id == first.binding_id
    assert second.installation_state == "enabled"
    assert second.binding_state == "enabled"
    assert db_session.query(IntegrationInstallation).count() == 1
    assert db_session.query(IntegrationConfigRevision).count() == 1
    assert db_session.query(IntegrationCapabilityBinding).count() == 1


def test_prepare_leaves_receiver_disabled(db_session) -> None:
    result = configure_fiber_inquiry_installation(
        db_session,
        command=_command(BootstrapMode.prepare),
    )

    assert result.installation_state == "disabled"
    assert result.binding_state == "disabled"


def test_callback_path_matches_mounted_route() -> None:
    from app.main import app

    binding_id = UUID("00000000-0000-0000-0000-000000000123")
    mounted_paths = {getattr(route, "path", "") for route in app.routes}

    assert "/api/v1/webhooks/fiber-inquiry/{capability_binding_id}" in mounted_paths
    assert fiber_inquiry_callback_path(binding_id) == (
        "/api/v1/webhooks/fiber-inquiry/00000000-0000-0000-0000-000000000123"
    )
    assert CAPABILITY_ID == "communications.fiber_inquiry.receive.v1"
