from __future__ import annotations

import pytest
from fastapi.templating import Jinja2Templates

from app.models.billing import (
    PaymentChannel,
    PaymentChannelType,
    PaymentProvider,
    PaymentProviderType,
)
from app.models.integration_platform import IntegrationInstallation
from app.services import payment_gateway_finance
from app.services import web_integrations_payment_gateways as service
from app.services.integrations import installations
from app.services.integrations.runtime import ValidationResult

SECRET_KEY_REF = "env://PAYSTACK_TEST_SECRET_KEY"
PUBLIC_KEY_REF = "env://PAYSTACK_TEST_PUBLIC_KEY"
FLW_SECRET_KEY_REF = "env://FLW_TEST_SECRET_KEY"
FLW_PUBLIC_KEY_REF = "env://FLW_TEST_PUBLIC_KEY"
FLW_WEBHOOK_HASH_REF = "env://FLW_TEST_WEBHOOK_HASH"


@pytest.fixture(autouse=True)
def _resolvable_gateway_secrets(monkeypatch):
    """Back every test reference with a real value.

    `save_config` resolves each reference before creating a revision, so test
    references must actually resolve. Environment references keep that check
    exercised without needing OpenBao.
    """
    monkeypatch.setenv("PAYSTACK_TEST_SECRET_KEY", "sk_test_value")
    monkeypatch.setenv("PAYSTACK_TEST_PUBLIC_KEY", "pk_test_value")
    monkeypatch.setenv("FLW_TEST_SECRET_KEY", "FLWSECK_TEST-value")
    monkeypatch.setenv("FLW_TEST_PUBLIC_KEY", "FLWPUBK_TEST-value")
    monkeypatch.setenv("FLW_TEST_WEBHOOK_HASH", "webhook-hash-value")


def _save_paystack(db_session):
    return service.save_config(
        db_session,
        provider_type_value="paystack",
        presentment_priority=40,
        gateway_credentials=SECRET_KEY_REF,
        public_key=PUBLIC_KEY_REF,
        webhook_signing_secret="",
    )


def test_save_gateway_config_creates_complete_bundle_and_finance_identity(db_session):
    installation = _save_paystack(db_session)

    assert db_session.query(IntegrationInstallation).one().id == installation.id
    assert installation.state == "disabled"
    assert {binding.capability_id for binding in installation.capability_bindings} == {
        "payments.intent.v1",
        "payments.webhook.v1",
        "payments.reconcile.v1",
        "payments.refund.v1",
    }
    intent_binding = next(
        binding
        for binding in installation.capability_bindings
        if binding.capability_id == "payments.intent.v1"
    )
    assert intent_binding.policy_json["presentment_priority"] == 40
    assert db_session.query(PaymentProvider).one().provider_type.value == "paystack"
    assert db_session.query(PaymentChannel).one().provider_id is not None

    state = service.build_config_state(db_session, "paystack")
    assert state["form"]["gateway_credentials"] == ""
    assert state["form"]["gateway_credentials_masked"].startswith("env://")
    assert "secret_key" not in str(state)


def test_save_gateway_config_rejects_plaintext_secret(db_session):
    try:
        service.save_config(
            db_session,
            provider_type_value="paystack",
            presentment_priority=0,
            gateway_credentials="plaintext-secret",
            public_key=PUBLIC_KEY_REF,
            webhook_signing_secret="",
        )
    except ValueError as exc:
        assert "reference" in str(exc)
    else:
        raise AssertionError("plaintext secret was accepted")


def test_save_gateway_config_rejects_a_reference_that_does_not_resolve(db_session):
    """A well-formed reference pointing nowhere must not reach a revision.

    Static validation only checks reference *form*. Creating a revision
    disables every existing binding, so accepting an unresolvable reference
    would take a working gateway offline at live validation instead of here.
    """
    with pytest.raises(ValueError) as excinfo:
        service.save_config(
            db_session,
            provider_type_value="paystack",
            presentment_priority=0,
            gateway_credentials=SECRET_KEY_REF,
            public_key="env://PAYSTACK_TYPO_NOT_SET",
            webhook_signing_secret="",
        )
    message = str(excinfo.value)
    assert "do not resolve" in message
    assert "public_key" in message
    assert db_session.query(IntegrationInstallation).count() == 0


def test_paystack_checkout_requires_a_public_key_reference(db_session):
    """`public_key` is manifest-optional but checkout cannot work without it."""
    with pytest.raises(ValueError) as excinfo:
        service.save_config(
            db_session,
            provider_type_value="paystack",
            presentment_priority=0,
            gateway_credentials=SECRET_KEY_REF,
            public_key="",
            webhook_signing_secret="",
        )
    assert "Public key" in str(excinfo.value)


def test_flutterwave_requires_a_webhook_signing_secret(db_session):
    """Flutterwave compares `verif-hash` against a stored literal.

    The manifest marks the hash optional, so without this the installation
    enables, reports healthy, and silently rejects every inbound webhook.
    """
    with pytest.raises(ValueError) as excinfo:
        service.save_config(
            db_session,
            provider_type_value="flutterwave",
            presentment_priority=0,
            gateway_credentials=FLW_SECRET_KEY_REF,
            public_key=FLW_PUBLIC_KEY_REF,
            webhook_signing_secret="",
        )
    assert "Webhook signing secret" in str(excinfo.value)


def test_flutterwave_saves_when_every_required_reference_is_present(db_session):
    installation = service.save_config(
        db_session,
        provider_type_value="flutterwave",
        presentment_priority=10,
        gateway_credentials=FLW_SECRET_KEY_REF,
        public_key=FLW_PUBLIC_KEY_REF,
        webhook_signing_secret=FLW_WEBHOOK_HASH_REF,
    )
    refs = installation.current_config_revision.secret_refs
    assert refs["webhook_signing_secret"] == FLW_WEBHOOK_HASH_REF
    assert refs["public_key"] == FLW_PUBLIC_KEY_REF


def test_validate_and_enable_uses_connector_connection_result(db_session, monkeypatch):
    installation = _save_paystack(db_session)
    monkeypatch.setattr(service, "build_execution_context", lambda *a, **k: object())
    monkeypatch.setattr(
        service,
        "validate_connection",
        lambda _context: ValidationResult(valid=True),
    )

    enabled = installations.execute_command(
        db_session,
        lambda: service.validate_and_enable(db_session, provider_type_value="paystack"),
    )

    assert enabled.id == installation.id
    assert enabled.state == "enabled"
    assert all(binding.state == "enabled" for binding in enabled.capability_bindings)


def test_disable_stops_new_checkout_but_keeps_lifecycle_capabilities(db_session):
    installation = _save_paystack(db_session)
    installations.enable_after_connection_validation(
        db_session,
        installation_id=installation.id,
        connection_result=ValidationResult(valid=True),
    )

    service.disable(db_session, provider_type_value="paystack")

    states = {
        binding.capability_id: binding.state
        for binding in installation.capability_bindings
    }
    assert states["payments.intent.v1"] == "disabled"
    assert states["payments.webhook.v1"] == "enabled"
    assert states["payments.reconcile.v1"] == "enabled"
    assert states["payments.refund.v1"] == "enabled"
    state = service.build_config_state(db_session, "paystack")
    assert state["health"]["health"] == "checkout_disabled"
    assert state["health"]["lifecycle_ready"] is True


def test_finance_identity_fails_closed_on_multiple_provider_channels(db_session):
    _save_paystack(db_session)
    provider = db_session.query(PaymentProvider).one()
    db_session.add(
        PaymentChannel(
            name="Unexpected Paystack channel",
            channel_type=PaymentChannelType.card,
            provider_id=provider.id,
        )
    )
    db_session.flush()

    with pytest.raises(
        payment_gateway_finance.PaymentGatewayFinanceError,
        match="multiple settlement channels",
    ):
        payment_gateway_finance.ensure_gateway_identity(
            db_session,
            provider_type=PaymentProviderType.paystack,
        )


def test_payment_gateway_and_customer_payment_templates_compile():
    env = Jinja2Templates(directory="templates").env

    for template_name in (
        "admin/integrations/payment_gateways/config.html",
        "customer/billing/pay.html",
        "customer/billing/topup.html",
    ):
        env.get_template(template_name)
