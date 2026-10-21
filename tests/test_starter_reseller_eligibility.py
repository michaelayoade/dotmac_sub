"""Reseller consolidated Starter eligibility gates (pre-write)."""

from __future__ import annotations

import uuid
from decimal import Decimal

from app.models.billing import BillingAccount, Invoice, InvoiceStatus
from app.models.subscriber import Reseller, Subscriber
from app.services.billing import starter_policies, starter_reseller_settlement
from app.services.billing.starter_reseller_settlement import (
    ResellerSettlementSlice,
    reseller_settlement_eligibility_reason,
    touches_starter_invoices,
)


def _account(db) -> BillingAccount:
    reseller = Reseller(name=f"R-{uuid.uuid4().hex[:6]}")
    db.add(reseller)
    db.commit()
    db.refresh(reseller)
    account = BillingAccount(
        reseller_id=reseller.id,
        name=f"BA-{uuid.uuid4().hex[:6]}",
        currency="NGN",
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


def _invoice(db, account, *, total: str = "100.00") -> Invoice:
    sub = Subscriber(
        first_name="M",
        last_name="Ember",
        email=f"m{uuid.uuid4().hex[:8]}@example.com",
        status="active",
        is_active=True,
        reseller_id=account.reseller_id,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    invoice = Invoice(
        account_id=sub.id,
        invoice_number=f"INV-{uuid.uuid4().hex[:6]}",
        status=InvoiceStatus.issued,
        total=Decimal(total),
        balance_due=Decimal(total),
        currency="NGN",
        is_proforma=False,
    )
    db.add(invoice)
    db.commit()
    db.refresh(invoice)
    return invoice


def _enable_ledger(monkeypatch) -> None:
    real_resolve = starter_policies.resolve_value

    def _resolve(db, domain, key):
        if key == "starter_ledger_live_enabled":
            return True
        return real_resolve(db, domain, key)

    monkeypatch.setattr(starter_policies, "resolve_value", _resolve)


def test_switch_off_never_touches_starter(db_session) -> None:
    account = _account(db_session)
    invoice = _invoice(db_session, account)
    slices = (
        ResellerSettlementSlice(
            invoice_id=invoice.id,
            subscriber_id=invoice.account_id,
            amount=Decimal("100.00"),
        ),
    )
    reason = reseller_settlement_eligibility_reason(
        db_session,
        account=account,
        slices=slices,
        total=Decimal("100.00"),
        external_reference="bank-1",
    )
    assert reason is not None and "switch is OFF" in reason
    assert not touches_starter_invoices(db_session, [invoice.id])


def test_unlinked_invoice_is_not_starter_touched(db_session, monkeypatch) -> None:
    _enable_ledger(monkeypatch)
    account = _account(db_session)
    invoice = _invoice(db_session, account)
    assert not starter_reseller_settlement.touches_starter_invoices(
        db_session, [invoice.id]
    )


def test_missing_external_reference_and_partial_slice_refuse(
    db_session, monkeypatch
) -> None:
    _enable_ledger(monkeypatch)
    account = _account(db_session)
    invoice = _invoice(db_session, account)
    slices = (
        ResellerSettlementSlice(
            invoice_id=invoice.id,
            subscriber_id=invoice.account_id,
            amount=Decimal("40.00"),
        ),
    )
    reason = reseller_settlement_eligibility_reason(
        db_session,
        account=account,
        slices=slices,
        total=Decimal("40.00"),
        external_reference=None,
    )
    assert reason is not None and "external settlement reference" in reason

    reason = reseller_settlement_eligibility_reason(
        db_session,
        account=account,
        slices=slices,
        total=Decimal("40.00"),
        external_reference="bank-1",
    )
    assert reason is not None and "canonical party identity" in reason
