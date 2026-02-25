from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.billing import (
    CreditNote,
    CreditNoteStatus,
    Invoice,
    InvoiceStatus,
    Payment,
    PaymentMethod,
    PaymentMethodType,
    PaymentStatus,
)
from app.services.billing.reporting import billing_reporting


def _create_invoice(
    db_session,
    *,
    account_id,
    status: InvoiceStatus,
    total: str,
    balance_due: str,
    created_at: datetime,
):
    invoice = Invoice(
        account_id=account_id,
        status=status,
        subtotal=Decimal(total),
        tax_total=Decimal("0.00"),
        total=Decimal(total),
        balance_due=Decimal(balance_due),
        created_at=created_at,
        paid_at=created_at if status == InvoiceStatus.paid else None,
    )
    db_session.add(invoice)
    db_session.commit()
    db_session.refresh(invoice)
    return invoice


def _create_payment_method(db_session, *, account_id, method_type: PaymentMethodType):
    method = PaymentMethod(
        account_id=account_id,
        method_type=method_type,
        label=method_type.value,
        is_active=True,
    )
    db_session.add(method)
    db_session.commit()
    db_session.refresh(method)
    return method


def _create_payment(
    db_session,
    *,
    account_id,
    status: PaymentStatus,
    amount: str,
    created_at: datetime,
    payment_method_id=None,
):
    payment = Payment(
        account_id=account_id,
        amount=Decimal(amount),
        currency="NGN",
        status=status,
        created_at=created_at,
        paid_at=created_at if status == PaymentStatus.succeeded else None,
        payment_method_id=payment_method_id,
    )
    db_session.add(payment)
    db_session.commit()
    db_session.refresh(payment)
    return payment


def _create_credit_note(
    db_session,
    *,
    account_id,
    status: CreditNoteStatus,
    total: str,
    created_at: datetime,
):
    note = CreditNote(
        account_id=account_id,
        status=status,
        currency="NGN",
        subtotal=Decimal(total),
        tax_total=Decimal("0.00"),
        total=Decimal(total),
        applied_total=Decimal("0.00"),
        created_at=created_at,
    )
    db_session.add(note)
    db_session.commit()
    db_session.refresh(note)
    return note


def test_dashboard_stats_include_new_kpis_and_comparison(db_session, subscriber):
    now = datetime.now(UTC)
    _create_invoice(
        db_session,
        account_id=subscriber.id,
        status=InvoiceStatus.paid,
        total="120.00",
        balance_due="0.00",
        created_at=now,
    )
    _create_invoice(
        db_session,
        account_id=subscriber.id,
        status=InvoiceStatus.issued,
        total="80.00",
        balance_due="80.00",
        created_at=now,
    )
    _create_invoice(
        db_session,
        account_id=subscriber.id,
        status=InvoiceStatus.overdue,
        total="50.00",
        balance_due="50.00",
        created_at=now - timedelta(days=31),
    )

    method = _create_payment_method(
        db_session,
        account_id=subscriber.id,
        method_type=PaymentMethodType.transfer,
    )
    _create_payment(
        db_session,
        account_id=subscriber.id,
        status=PaymentStatus.succeeded,
        amount="100.00",
        created_at=now,
        payment_method_id=method.id,
    )
    _create_payment(
        db_session,
        account_id=subscriber.id,
        status=PaymentStatus.pending,
        amount="60.00",
        created_at=now,
        payment_method_id=method.id,
    )

    _create_credit_note(
        db_session,
        account_id=subscriber.id,
        status=CreditNoteStatus.issued,
        total="25.00",
        created_at=now,
    )
    _create_credit_note(
        db_session,
        account_id=subscriber.id,
        status=CreditNoteStatus.void,
        total="10.00",
        created_at=now,
    )

    result = billing_reporting.get_dashboard_stats(db_session)
    stats = result["stats"]

    assert stats["payments_count"] == 1
    assert stats["payments_amount"] == 100.0
    assert stats["unpaid_invoices_count"] == 2
    assert stats["unpaid_invoices_amount"] == 130.0
    assert stats["credit_notes_count"] == 1
    assert stats["credit_notes_total"] == 25.0
    assert len(result["period_comparison"]) == 3
    assert [item["label"] for item in result["period_comparison"]] == [
        "Last Month",
        "Current Month",
        "Next Month",
    ]


def test_dashboard_stats_include_payment_method_and_daily_payments(db_session, subscriber):
    now = datetime.now(UTC)
    cash_method = _create_payment_method(
        db_session,
        account_id=subscriber.id,
        method_type=PaymentMethodType.cash,
    )
    _create_payment(
        db_session,
        account_id=subscriber.id,
        status=PaymentStatus.succeeded,
        amount="40.00",
        created_at=now,
        payment_method_id=cash_method.id,
    )
    _create_payment(
        db_session,
        account_id=subscriber.id,
        status=PaymentStatus.succeeded,
        amount="30.00",
        created_at=now - timedelta(days=1),
        payment_method_id=cash_method.id,
    )

    result = billing_reporting.get_dashboard_stats(db_session)
    breakdown = result["payment_method_breakdown"]
    daily = result["daily_payments"]

    assert "Cash" in breakdown["labels"]
    assert sum(breakdown["values"]) == 70.0
    assert len(daily["labels"]) >= 28
    assert len(daily["labels"]) == len(daily["values"])
    assert sum(daily["values"]) >= 70.0
