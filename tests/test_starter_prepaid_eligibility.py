"""Starter prepaid funding: link discovery and pre-write refusals."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from app.models.billing import Invoice, InvoiceLine, StarterPrepaidFundingLink
from app.services.billing import starter_prepaid as prepaid


class _Result:
    def __init__(self, link):
        self._link = link

    def scalar_one_or_none(self):
        return self._link


class _Db:
    def __init__(self, link=None):
        self._link = link

    def execute(self, _statement):  # noqa: ANN001, ANN202
        return _Result(self._link)


def _invoice(*line_keys: str | None) -> Invoice:
    invoice = Invoice(currency="NGN")
    invoice.id = uuid4()
    invoice.lines = [
        InvoiceLine(
            description="Period",
            quantity=Decimal("1"),
            unit_price=Decimal("10.00"),
            amount=Decimal("10.00"),
            is_active=True,
            billing_line_key=key,
        )
        for key in line_keys
    ]
    return invoice


def _link(key: str) -> StarterPrepaidFundingLink:
    return StarterPrepaidFundingLink(
        tenant_id=uuid4(),
        subscription_id=uuid4(),
        billing_line_key=key,
        source_fact_id=f"prepaid-period:{key}",
        billing_account_id=uuid4(),
        settlement_id=uuid4(),
        confirmation_id=uuid4(),
        purchase_ref="purchase-1",
    )


def test_no_line_key_has_no_funding_link() -> None:
    assert prepaid.funding_link_for_invoice(_Db(), _invoice(None)) is None


def test_multiple_line_keys_have_no_funding_link() -> None:
    assert prepaid.funding_link_for_invoice(_Db(), _invoice("a", "b")) is None


def test_single_line_key_resolves_link() -> None:
    link = _link("prepaid-renewal:abc")
    assert (
        prepaid.funding_link_for_invoice(_Db(link), _invoice("prepaid-renewal:abc"))
        is link
    )


def test_funding_without_targets_refuses_before_writes() -> None:
    with pytest.raises(prepaid.StarterPrepaidError):
        prepaid.fund_prepaid_purchase(
            _Db(),  # type: ignore[arg-type]
            subscriber_id=uuid4(),
            purchase_ref="purchase-1",
            intent_reference="intent-1",
            external_reference="ext-1",
            confirmed_amount=Decimal("10.00"),
            currency_code="NGN",
            targets=(),
            occurred_at=datetime.now(UTC),
        )


def test_funding_targets_must_cover_purchase() -> None:
    with pytest.raises(prepaid.StarterPrepaidError):
        prepaid.fund_prepaid_purchase(
            _Db(),  # type: ignore[arg-type]
            subscriber_id=uuid4(),
            purchase_ref="purchase-1",
            intent_reference="intent-1",
            external_reference="ext-1",
            confirmed_amount=Decimal("20.00"),
            currency_code="NGN",
            targets=(
                prepaid.PrepaidPeriodTarget(
                    subscription_id=uuid4(),
                    billing_line_key="prepaid-renewal:abc",
                    amount=Decimal("10.00"),
                ),
            ),
            occurred_at=datetime.now(UTC),
        )
