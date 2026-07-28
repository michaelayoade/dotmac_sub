from __future__ import annotations

from app.models.event_store import EventStore
from app.models.integration_platform import IntegrationBindingState
from app.services.integrations import installations
from app.services.integrations.registry import (
    connector_definition,
    supported_connector_definitions,
)
from app.services.integrations.runtime_execution import build_execution_context
from app.services.owner_commands import CommandContext
from tests.integration_platform_helpers import enable_payment_provider


def _paystack_legacy_pin() -> installations.ManifestPin:
    definition = next(
        definition
        for definition in supported_connector_definitions()
        if definition.key == "paystack"
        and definition.version == "1.0.0"
        and definition.digest
        == "53791d3e2e06fe1ca128a0e3e8ced86549392af7b6131f61bd21044d71aafc6e"
    )
    return installations.ManifestPin(
        connector_version=definition.version,
        manifest_digest=definition.digest,
    )


def _current_paystack_pin() -> installations.ManifestPin:
    definition = connector_definition("paystack")
    assert definition is not None
    return installations.ManifestPin(
        connector_version=definition.version,
        manifest_digest=definition.digest,
    )


def _context(*, idempotency_key: str = "adopt-paystack-1") -> CommandContext:
    return CommandContext.system(
        actor="operator:integration-test",
        scope=installations.MANIFEST_ADOPTION_SCOPE,
        reason="Adopt reviewed Paystack connector manifest",
        idempotency_key=idempotency_key,
    )


def _pin_installation_to_legacy(db_session):
    bindings = enable_payment_provider(db_session, "paystack")
    installation = bindings["payments.intent.v1"].installation
    legacy = _paystack_legacy_pin()
    installation.connector_version = legacy.connector_version
    installation.manifest_digest = legacy.manifest_digest
    installation_id = installation.id
    binding_id = bindings["payments.intent.v1"].id
    db_session.commit()
    return installation_id, binding_id


def test_historical_pin_remains_executable_during_adoption_window(db_session):
    installation_id, binding_id = _pin_installation_to_legacy(db_session)

    context = build_execution_context(
        db_session,
        capability_binding_id=binding_id,
        secret_resolver=lambda _reference: "test-material",
    )

    assert context.binding.installation_id == installation_id
    assert context.manifest.version == "1.0.0"
    assert context.manifest.digest == _paystack_legacy_pin().manifest_digest


def test_manifest_adoption_is_atomic_audited_and_idempotent(db_session):
    installation_id, _binding_id = _pin_installation_to_legacy(db_session)
    preview = installations.preview_manifest_adoption(
        db_session,
        installation_id=installation_id,
    )

    assert preview.pin_state is installations.ManifestPinState.supported_historical
    assert preview.adoption_required is True
    assert preview.ready is True
    assert preview.target_pin == _current_paystack_pin()
    db_session.commit()

    result = installations.adopt_installation_manifest(
        db_session,
        installations.AdoptManifestCommand(
            installation_id=installation_id,
            expected_installed_pin=_paystack_legacy_pin(),
            target_pin=_current_paystack_pin(),
        ),
        context=_context(),
    )

    assert result.previous_pin == _paystack_legacy_pin()
    assert result.adopted_pin == _current_paystack_pin()
    assert result.replayed is False
    installation = installations.get_installation(db_session, installation_id)
    assert installation.connector_version == _current_paystack_pin().connector_version
    assert installation.manifest_digest == _current_paystack_pin().manifest_digest
    assert {binding.state for binding in installation.capability_bindings} == {
        IntegrationBindingState.enabled.value
    }
    events = (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "integration.installation.manifest_adopted")
        .all()
    )
    assert len(events) == 1
    assert events[0].payload["installation_id"] == str(installation_id)
    assert "secret" not in events[0].payload
    db_session.commit()

    replay = installations.adopt_installation_manifest(
        db_session,
        installations.AdoptManifestCommand(
            installation_id=installation_id,
            expected_installed_pin=_paystack_legacy_pin(),
            target_pin=_current_paystack_pin(),
        ),
        context=_context(),
    )

    assert replay.replayed is True
    assert (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "integration.installation.manifest_adopted")
        .count()
        == 1
    )


def test_manifest_adoption_rejects_stale_reviewed_pin(db_session):
    installation_id, _binding_id = _pin_installation_to_legacy(db_session)

    try:
        installations.adopt_installation_manifest(
            db_session,
            installations.AdoptManifestCommand(
                installation_id=installation_id,
                expected_installed_pin=installations.ManifestPin(
                    connector_version="0.9.0",
                    manifest_digest="a" * 64,
                ),
                target_pin=_current_paystack_pin(),
            ),
            context=_context(idempotency_key="stale-adoption"),
        )
    except installations.ManifestAdoptionError as exc:
        assert exc.code == "integration.installations.stale_manifest_pin"
    else:  # pragma: no cover - assertion guard
        raise AssertionError("stale reviewed pin was accepted")

    installation = installations.get_installation(db_session, installation_id)
    assert installation.connector_version == _paystack_legacy_pin().connector_version
    assert installation.manifest_digest == _paystack_legacy_pin().manifest_digest


def test_manifest_adoption_rejects_target_configuration_incompatibility(db_session):
    installation_id, _binding_id = _pin_installation_to_legacy(db_session)
    installation = installations.get_installation(db_session, installation_id)
    revision = installation.current_config_revision
    assert revision is not None
    revision.secret_refs = {
        "gateway_credentials": "env://PAYSTACK_TEST_SECRET",
    }
    db_session.commit()

    preview = installations.preview_manifest_adoption(
        db_session,
        installation_id=installation_id,
    )
    assert preview.ready is False
    assert "secret_required:public_key" in preview.blocking_errors
    db_session.commit()

    try:
        installations.adopt_installation_manifest(
            db_session,
            installations.AdoptManifestCommand(
                installation_id=installation_id,
                expected_installed_pin=_paystack_legacy_pin(),
                target_pin=_current_paystack_pin(),
            ),
            context=_context(idempotency_key="incompatible-adoption"),
        )
    except installations.ManifestAdoptionError as exc:
        assert exc.code == "integration.installations.manifest_adoption_incompatible"
        assert "secret_required:public_key" in exc.details["error_codes"]
    else:  # pragma: no cover - assertion guard
        raise AssertionError("incompatible manifest adoption was accepted")

    installation = installations.get_installation(db_session, installation_id)
    assert installation.connector_version == _paystack_legacy_pin().connector_version
    assert installation.manifest_digest == _paystack_legacy_pin().manifest_digest
