"""Starter manual target: only single full allocations switch lanes."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.billing import Invoice, PaymentStatus, StarterInvoiceLink
from app.services import manual_payment_recording as manual


class _Result:
    def __init__(self, links):
        self._links = links

    def scalars(self):
        return list(self._links)


class _Db:
    def __init__(self, links, invoice=None):
        self._links = links
        self._invoice = invoice

    def execute(self, _statement):  # noqa: ANN001, ANN202
        return _Result(self._links)


def _invoice(account_id=None) -> Invoice:
    invoice = Invoice(currency="NGN")
    invoice.id = uuid4()
    invoice.account_id = account_id or uuid4()
    return invoice


def _link(invoice: Invoice) -> StarterInvoiceLink:
    return StarterInvoiceLink(
        invoice_id=invoice.id,
        billing_account_id=uuid4(),
        document_id=uuid4(),
        source_fact_id="test",
    )


def _preview(invoice: Invoice, **overrides):  # noqa: ANN202
    effect = SimpleNamespace(invoice_id=invoice.id, allocation_amount=Decimal("107.50"))
    payment_preview = SimpleNamespace(
        allocation_effects=(effect,),
        unallocated_amount=Decimal("0"),
        prepaid_funding_before=Decimal("0"),
        prepaid_funding_after=Decimal("0"),
        account_credit_before=Decimal("0"),
        account_credit_after=Decimal("0"),
    )
    for key, value in overrides.items():
        setattr(payment_preview, key, value)
    return SimpleNamespace(payment_preview=payment_preview)


def _request(invoice: Invoice, **overrides):  # noqa: ANN202
    request = SimpleNamespace(
        status=PaymentStatus.succeeded,
        billing_account_id=None,
        account_id=invoice.account_id,
        amount=Decimal("107.50"),
        currency="NGN",
    )
    for key, value in overrides.items():
        setattr(request, key, value)
    return request


def test_no_starter_link_stays_legacy(monkeypatch) -> None:
    invoice = _invoice()
    result = manual._starter_manual_target(
        _Db([]),
        _preview(invoice),
        _request(invoice),  # type: ignore[arg-type]
    )
    assert result is None


def test_single_full_allocation_is_eligible(monkeypatch) -> None:
    invoice = _invoice()
    link = _link(invoice)
    monkeypatch.setattr(manual, "lock_for_update", lambda db, model, pk: invoice)
    result = manual._starter_manual_target(
        _Db([link]),
        _preview(invoice),
        _request(invoice),  # type: ignore[arg-type]
    )
    assert result == (invoice, link)


def test_partial_allocation_is_refused() -> None:
    invoice = _invoice()
    with pytest.raises(manual.ManualPaymentRecordingError):
        manual._starter_manual_target(
            _Db([_link(invoice)]),
            _preview(invoice, unallocated_amount=Decimal("7.50")),
            _request(invoice),  # type: ignore[arg-type]
        )


def test_pending_status_is_refused() -> None:
    invoice = _invoice()
    with pytest.raises(manual.ManualPaymentRecordingError):
        manual._starter_manual_target(
            _Db([_link(invoice)]),
            _preview(invoice),
            _request(invoice, status=PaymentStatus.pending),  # type: ignore[arg-type]
        )


def test_credit_movement_is_refused() -> None:
    invoice = _invoice()
    with pytest.raises(manual.ManualPaymentRecordingError):
        manual._starter_manual_target(
            _Db([_link(invoice)]),
            _preview(invoice, account_credit_after=Decimal("10.00")),
            _request(invoice),  # type: ignore[arg-type]
        )
