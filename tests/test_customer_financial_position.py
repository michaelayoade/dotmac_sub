"""Shared invoice collectibility and customer financial position tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.billing import Invoice, InvoiceStatus
from app.models.subscriber import Subscriber
from app.services.customer_financial_position import get_customer_financial_position
from app.services.invoice_collectibility import (
    due_invoice_balance,
    list_open_invoices,
    open_invoice_balance,
    overdue_status_count,
)
from app.services.notification_template_conditions import conditions_match
from app.services.vas_wallet import _open_invoice_balance
from app.services.web_customer_actions import _billing_template_variables


def _subscriber(db_session) -> Subscriber:
    subscriber = Subscriber(
        first_name="Financial",
        last_name="Position",
        email=f"financial-{uuid.uuid4().hex}@example.com",
    )
    db_session.add(subscriber)
    db_session.commit()
    return subscriber


def _invoice(
    db_session,
    subscriber,
    *,
    status=InvoiceStatus.issued,
    balance="100.00",
    due_at=None,
    is_active=True,
):
    invoice = Invoice(
        account_id=subscriber.id,
        invoice_number=f"INV-{uuid.uuid4().hex[:8]}",
        status=status,
        total=Decimal(balance),
        balance_due=Decimal(balance),
        due_at=due_at,
        is_active=is_active,
    )
    db_session.add(invoice)
    db_session.commit()
    return invoice


def test_invoice_collectibility_splits_open_due_and_overdue_status(db_session):
    subscriber = _subscriber(db_session)
    now = datetime.now(UTC)
    past = now - timedelta(days=3)
    future = now + timedelta(days=3)
    _invoice(
        db_session,
        subscriber,
        status=InvoiceStatus.issued,
        balance="100.00",
        due_at=past,
    )
    _invoice(
        db_session,
        subscriber,
        status=InvoiceStatus.partially_paid,
        balance="50.00",
        due_at=future,
    )
    _invoice(db_session, subscriber, status=InvoiceStatus.overdue, balance="25.00")
    _invoice(
        db_session, subscriber, status=InvoiceStatus.paid, balance="10.00", due_at=past
    )
    _invoice(
        db_session, subscriber, status=InvoiceStatus.issued, balance="0.00", due_at=past
    )
    _invoice(
        db_session,
        subscriber,
        status=InvoiceStatus.issued,
        balance="99.00",
        due_at=past,
        is_active=False,
    )

    assert open_invoice_balance(db_session, subscriber.id) == Decimal("175.00")
    assert due_invoice_balance(db_session, subscriber.id, now=now) == Decimal("125.00")
    assert overdue_status_count(db_session, subscriber.id) == 1
    assert [
        invoice.balance_due for invoice in list_open_invoices(db_session, subscriber.id)
    ] == [
        Decimal("100.00"),
        Decimal("50.00"),
        Decimal("25.00"),
    ]


def test_customer_financial_position_summarizes_customer_debt(db_session):
    subscriber = _subscriber(db_session)
    now = datetime.now(UTC)
    oldest = _invoice(
        db_session,
        subscriber,
        status=InvoiceStatus.issued,
        balance="120.00",
        due_at=now - timedelta(days=5),
    )
    _invoice(
        db_session,
        subscriber,
        status=InvoiceStatus.overdue,
        balance="80.00",
        due_at=now - timedelta(days=1),
    )

    position = get_customer_financial_position(
        db_session,
        subscriber.id,
        now=now,
        include_prepaid_balance=False,
    )

    assert position.open_invoice_balance == Decimal("200.00")
    assert position.due_invoice_balance == Decimal("200.00")
    assert position.overdue_debt_balance == Decimal("200.00")
    assert position.overdue_invoice_count == 1
    assert position.oldest_due_invoice == oldest
    assert position.days_overdue == 5
    assert position.has_open_debt is True
    assert position.has_due_debt is True
    assert position.has_overdue_debt is True


def test_wired_consumers_use_shared_due_and_overdue_rules(db_session):
    subscriber = _subscriber(db_session)
    _invoice(db_session, subscriber, status=InvoiceStatus.issued, balance="20.00")
    _invoice(db_session, subscriber, status=InvoiceStatus.overdue, balance="30.00")

    assert _open_invoice_balance(db_session, subscriber.id) == Decimal("30.00")
    assert conditions_match(
        db_session,
        subscriber_id=subscriber.id,
        conditions={
            "field": "has_overdue_invoice",
            "operator": "=",
            "value": True,
        },
    )


def test_billing_template_variables_use_financial_position(db_session):
    subscriber = _subscriber(db_session)
    due_at = datetime.now(UTC) - timedelta(days=4)
    invoice = _invoice(
        db_session,
        subscriber,
        status=InvoiceStatus.issued,
        balance="45.50",
        due_at=due_at,
    )

    variables = _billing_template_variables(db_session, subscriber)

    assert variables["balance_due"] == "₦45.50"
    assert variables["days_overdue"] == "4"
    assert variables["invoice_number"] == invoice.invoice_number
