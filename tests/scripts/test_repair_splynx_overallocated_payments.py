from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.billing import (
    Invoice,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentAllocation,
    PaymentStatus,
)
from scripts.one_off.repair_splynx_overallocated_payments import (
    apply_repair_plan,
    build_repair_plan,
)


def _invoice(db_session, subscriber, amount: str, *, status=InvoiceStatus.paid):
    invoice = Invoice(
        account_id=subscriber.id,
        status=status,
        currency="NGN",
        total=Decimal(amount),
        balance_due=Decimal("0.00")
        if status == InvoiceStatus.paid
        else Decimal(amount),
    )
    db_session.add(invoice)
    db_session.flush()
    return invoice


def _allocation(
    db_session,
    payment,
    invoice,
    amount: str,
    *,
    created_at: datetime,
    ledger: bool = False,
):
    allocation = PaymentAllocation(
        payment_id=payment.id,
        invoice_id=invoice.id,
        amount=Decimal(amount),
        created_at=created_at,
    )
    db_session.add(allocation)
    if ledger:
        db_session.add(
            LedgerEntry(
                account_id=invoice.account_id,
                invoice_id=invoice.id,
                payment_id=payment.id,
                entry_type=LedgerEntryType.credit,
                source=LedgerSource.payment,
                amount=Decimal(amount),
                currency="NGN",
                memo="payment allocation",
            )
        )
    db_session.flush()
    return allocation


def test_build_repair_plan_is_read_only(db_session, subscriber):
    payment = Payment(
        account_id=subscriber.id,
        amount=Decimal("100.00"),
        currency="NGN",
        status=PaymentStatus.succeeded,
        splynx_payment_id=123,
    )
    db_session.add(payment)
    db_session.flush()
    base = datetime(2026, 3, 15, tzinfo=UTC)
    first = _allocation(
        db_session,
        payment,
        _invoice(db_session, subscriber, "60.00"),
        "60.00",
        created_at=base,
    )
    second = _allocation(
        db_session,
        payment,
        _invoice(db_session, subscriber, "80.00"),
        "80.00",
        created_at=base + timedelta(seconds=1),
    )
    db_session.commit()

    plan = build_repair_plan(db_session)

    assert plan.payments == 1
    assert plan.partial_reductions == 1
    assert plan.full_deactivations == 0
    assert plan.total_removed == Decimal("40.00")
    db_session.refresh(first)
    db_session.refresh(second)
    assert first.amount == Decimal("60.00")
    assert second.amount == Decimal("80.00")
    assert second.is_active is True


def test_apply_repair_plan_reduces_and_deactivates_excess(db_session, subscriber):
    payment = Payment(
        account_id=subscriber.id,
        amount=Decimal("100.00"),
        currency="NGN",
        status=PaymentStatus.succeeded,
        splynx_payment_id=456,
    )
    db_session.add(payment)
    db_session.flush()
    base = datetime(2026, 3, 15, tzinfo=UTC)
    keep_invoice = _invoice(db_session, subscriber, "60.00")
    partial_invoice = _invoice(db_session, subscriber, "80.00")
    deactivate_invoice = _invoice(db_session, subscriber, "30.00")
    keep = _allocation(
        db_session,
        payment,
        keep_invoice,
        "60.00",
        created_at=base,
        ledger=True,
    )
    partial = _allocation(
        db_session,
        payment,
        partial_invoice,
        "80.00",
        created_at=base + timedelta(seconds=1),
        ledger=True,
    )
    deactivate = _allocation(
        db_session,
        payment,
        deactivate_invoice,
        "30.00",
        created_at=base + timedelta(seconds=2),
        ledger=True,
    )
    db_session.commit()

    plan = build_repair_plan(db_session)
    apply_repair_plan(db_session, plan)
    db_session.commit()

    db_session.refresh(keep)
    db_session.refresh(partial)
    db_session.refresh(deactivate)
    db_session.refresh(keep_invoice)
    db_session.refresh(partial_invoice)
    db_session.refresh(deactivate_invoice)

    assert keep.amount == Decimal("60.00")
    assert keep.is_active is True
    assert partial.amount == Decimal("40.00")
    assert partial.is_active is True
    assert deactivate.is_active is False
    assert keep_invoice.status == InvoiceStatus.paid
    assert keep_invoice.balance_due == Decimal("0.00")
    assert partial_invoice.status == InvoiceStatus.partially_paid
    assert partial_invoice.balance_due == Decimal("40.00")
    assert deactivate_invoice.status == InvoiceStatus.issued
    assert deactivate_invoice.balance_due == Decimal("30.00")

    active_allocated = sum(
        amount
        for (amount,) in db_session.query(PaymentAllocation.amount)
        .filter(PaymentAllocation.payment_id == payment.id)
        .filter(PaymentAllocation.is_active.is_(True))
        .all()
    )
    assert active_allocated == Decimal("100.00")

    partial_ledger = (
        db_session.query(LedgerEntry)
        .filter(LedgerEntry.payment_id == payment.id)
        .filter(LedgerEntry.invoice_id == partial_invoice.id)
        .one()
    )
    deactivated_ledger = (
        db_session.query(LedgerEntry)
        .filter(LedgerEntry.payment_id == payment.id)
        .filter(LedgerEntry.invoice_id == deactivate_invoice.id)
        .one()
    )
    assert partial_ledger.amount == Decimal("40.00")
    assert partial_ledger.is_active is True
    assert deactivated_ledger.is_active is False

    assert build_repair_plan(db_session).repairs == []
