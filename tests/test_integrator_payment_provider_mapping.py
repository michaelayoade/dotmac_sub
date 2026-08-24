"""Operator-owned mapping between Integrator sources and local providers."""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.models.billing import PaymentProvider, PaymentProviderType
from app.services import payment_gateway_finance


def _provider(db_session, name: str) -> PaymentProvider:
    provider = PaymentProvider(
        name=f"{name} {uuid4().hex}",
        provider_type=PaymentProviderType.custom,
        is_active=True,
    )
    db_session.add(provider)
    db_session.flush()
    return provider


def test_operator_mapping_is_idempotent_and_locally_owned(db_session) -> None:
    provider = _provider(db_session, "Integrator provider")
    installation_id = uuid4()

    first = payment_gateway_finance.bind_integrator_installation(
        db_session,
        provider_id=provider.id,
        installation_id=installation_id,
        actor="pytest:mapping-operator",
    )
    second = payment_gateway_finance.bind_integrator_installation(
        db_session,
        provider_id=provider.id,
        installation_id=installation_id,
        actor="pytest:mapping-operator",
    )

    assert first == second
    assert provider.integrator_installation_ref == installation_id


def test_one_source_cannot_be_bound_to_a_second_provider(db_session) -> None:
    first = _provider(db_session, "First provider")
    second = _provider(db_session, "Second provider")
    installation_id = uuid4()
    payment_gateway_finance.bind_integrator_installation(
        db_session,
        provider_id=first.id,
        installation_id=installation_id,
        actor="pytest:mapping-operator",
    )

    with pytest.raises(
        payment_gateway_finance.PaymentGatewayFinanceError,
        match="already mapped",
    ):
        payment_gateway_finance.bind_integrator_installation(
            db_session,
            provider_id=second.id,
            installation_id=installation_id,
            actor="pytest:mapping-operator",
        )

    assert second.integrator_installation_ref is None
