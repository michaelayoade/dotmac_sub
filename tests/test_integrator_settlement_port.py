"""Product-owned acceptance for `payments.settlement.observation.v1`.

These fast tests prove adapter and owner behaviour.  The migration/constraint
acceptance lives in the PostgreSQL integration lane; SQLite is not described as
deployed-schema evidence here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import integrator_observations
from app.db import get_db
from app.models.auth import ApiKey
from app.models.billing import (
    Payment,
    PaymentProvider,
    PaymentProviderEvent,
    PaymentProviderType,
    TopupIntent,
)
from app.models.integration_platform import IntegrationInbox
from app.schemas.integrator_settlement_observation import (
    PRODUCT_OBSERVATION_SCHEMA_VERSION,
    SETTLEMENT_CAPABILITY,
)
from app.services.auth import hash_api_key
from app.services.integrations.connectors.integrator_http import (
    INTEGRATOR_CONNECTOR_KEY,
)
from tests.integration_platform_helpers import enable_capability

WRITE_TOKEN = "integrator-settlement-write"
MIRROR_TOKEN = "integrator-settlement-mirror"


@pytest.fixture()
def source_installation_id() -> UUID:
    return uuid4()


@pytest.fixture()
def binding(db_session):
    return enable_capability(
        db_session,
        connector_key=INTEGRATOR_CONNECTOR_KEY,
        capability_id=SETTLEMENT_CAPABILITY,
        config={},
        secret_refs={},
    )


@pytest.fixture()
def keys(db_session):
    now = datetime.now(UTC)
    db_session.add_all(
        [
            ApiKey(
                label="integrator-settlement-write",
                key_hash=hash_api_key(WRITE_TOKEN),
                scopes=[integrator_observations.INTEGRATOR_OBSERVATION_SCOPE],
                is_active=True,
                expires_at=now + timedelta(days=1),
            ),
            ApiKey(
                label="integrator-settlement-mirror",
                key_hash=hash_api_key(MIRROR_TOKEN),
                scopes=[integrator_observations.INTEGRATOR_MIRROR_SCOPE],
                is_active=True,
                expires_at=now + timedelta(days=1),
            ),
        ]
    )
    db_session.commit()


@pytest.fixture()
def client(db_session, keys):
    app = FastAPI()
    app.include_router(integrator_observations.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def provider(db_session, source_installation_id):
    row = PaymentProvider(
        name=f"Integrator Paystack {source_installation_id}",
        provider_type=PaymentProviderType.paystack,
        integrator_installation_ref=source_installation_id,
        is_active=True,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def _intent(db_session, subscriber, provider, *, reference: str) -> TopupIntent:
    row = TopupIntent(
        account_id=subscriber.id,
        provider_id=provider.id,
        reference=reference,
        provider_type=provider.provider_type.value,
        currency="NGN",
        requested_amount=Decimal("1000.00"),
        status="pending",
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def _envelope(
    source_installation_id: UUID,
    *,
    provider_event_id: str = "charge.success:990001",
    provider_transaction_id: str = "990001",
    reference: str = "DMAC-INTG-1",
    fee: dict[str, str] | None = None,
    connector_key: str = "transport-key-not-used-for-routing",
) -> dict[str, object]:
    return {
        "schema_version": PRODUCT_OBSERVATION_SCHEMA_VERSION,
        "capability_id": SETTLEMENT_CAPABILITY,
        "contract_version": 1,
        "source": {
            "installation_id": str(source_installation_id),
            "connector_key": connector_key,
        },
        "provider_event_id": provider_event_id,
        "event_type": SETTLEMENT_CAPABILITY,
        "scope": {"kind": "payment_provider_events", "ref": "verified"},
        "observation": {
            "capability_id": SETTLEMENT_CAPABILITY,
            "observation_kind": "capture",
            "provider_status": "success",
            "amount": {"amount": "1020.00", "currency": "NGN"},
            "provider_fee": fee,
            "occurred_at": "2026-08-23T12:00:00+00:00",
            "arrival_mode": "ingress",
            "confirmation_evidence": "connector_verified",
            "merchant_reference": reference,
            "transport_evidence": {
                "provider_event_type": "charge.success",
                "identity_source": "derived_from_provider_fields",
                "provider_transaction_id": provider_transaction_id,
            },
        },
    }


def _post(client, binding, envelope, *, token=WRITE_TOKEN, suffix=""):
    return client.post(
        "/api/v1/integration/observations/payment-settlements/"
        f"{binding.id}{suffix}",
        json=envelope,
        headers={"X-Api-Key": token} if token else {},
    )


def _counts(db_session) -> tuple[int, int, int]:
    return (
        db_session.query(IntegrationInbox).count(),
        db_session.query(PaymentProviderEvent).count(),
        db_session.query(Payment).count(),
    )


def test_rejected_credentials_change_no_financial_row(
    client, db_session, binding, source_installation_id
):
    before = _counts(db_session)
    response = _post(
        client,
        binding,
        _envelope(source_installation_id),
        token=None,
    )
    assert response.status_code == 401
    assert _counts(db_session) == before


def test_source_installation_mapping_not_connector_name_selects_provider(
    client,
    db_session,
    binding,
    source_installation_id,
    provider,
    subscriber,
):
    intent = _intent(
        db_session,
        subscriber,
        provider,
        reference="DMAC-INTG-1",
    )
    envelope = _envelope(
        source_installation_id,
        fee={"amount": "20.00", "currency": "NGN"},
        connector_key="a-name-sub-does-not-branch-on",
    )

    first = _post(client, binding, envelope)
    second = _post(client, binding, envelope)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert second.json()["replayed"] is True
    event = db_session.query(PaymentProviderEvent).one()
    payment = db_session.query(Payment).one()
    assert event.provider_id == provider.id
    assert event.external_id == "990001"
    assert event.provider_fee == Decimal("20.00")
    assert payment.provider_id == provider.id
    assert payment.amount == Decimal("1020.00")
    db_session.refresh(intent)
    assert intent.completed_payment_id == payment.id


def test_unmapped_source_is_durable_but_moves_no_money(
    client, db_session, binding, source_installation_id
):
    response = _post(client, binding, _envelope(source_installation_id))

    assert response.status_code == 503
    receipt = db_session.query(IntegrationInbox).one()
    assert receipt.state == "retryable"
    assert receipt.error_code.endswith("integrator_provider_not_configured")
    assert db_session.query(PaymentProviderEvent).count() == 0
    assert db_session.query(Payment).count() == 0


def test_unobserved_fee_never_becomes_zero(
    client,
    db_session,
    binding,
    source_installation_id,
    provider,
    subscriber,
):
    _intent(db_session, subscriber, provider, reference="DMAC-INTG-1")

    response = _post(client, binding, _envelope(source_installation_id, fee=None))

    assert response.status_code == 503
    receipt = db_session.query(IntegrationInbox).one()
    assert receipt.state == "retryable"
    assert receipt.error_code.endswith("provider_fee_unobserved")
    assert db_session.query(PaymentProviderEvent).count() == 0
    assert db_session.query(Payment).count() == 0


def test_payload_cannot_name_a_local_provider(
    client, db_session, binding, source_installation_id, provider
):
    envelope = _envelope(source_installation_id)
    observation = envelope["observation"]
    assert isinstance(observation, dict)
    observation["provider_id"] = str(provider.id)
    before = _counts(db_session)

    response = _post(client, binding, envelope)

    assert response.status_code == 422
    assert _counts(db_session) == before


def test_mirror_compares_without_writing(
    client,
    db_session,
    binding,
    source_installation_id,
    provider,
    subscriber,
):
    _intent(db_session, subscriber, provider, reference="DMAC-INTG-1")
    envelope = _envelope(
        source_installation_id,
        fee={"amount": "20.00", "currency": "NGN"},
    )
    assert _post(client, binding, envelope).status_code == 200
    before = _counts(db_session)

    mirrored = _post(
        client,
        binding,
        envelope,
        token=MIRROR_TOKEN,
        suffix="/mirror",
    )

    assert mirrored.status_code == 200, mirrored.text
    assert mirrored.json()["agrees"] is True
    assert _counts(db_session) == before


def test_descriptor_declares_the_generic_v2_wire(
    client, binding
):
    response = client.get(
        "/api/v1/integration/observations/payment-settlements/"
        f"{binding.id}/descriptor",
        headers={"X-Api-Key": MIRROR_TOKEN},
    )

    assert response.status_code == 200, response.text
    descriptor = response.json()
    assert descriptor["schema_version"] == "dotmac.io/product-port-descriptor/v2"
    assert descriptor["capability_id"] == SETTLEMENT_CAPABILITY
    assert descriptor["delivery_path"].endswith(str(binding.id))
    assert descriptor["destination_scope"] == {
        "kind": "payment_provider_events",
        "ref": "verified",
    }
