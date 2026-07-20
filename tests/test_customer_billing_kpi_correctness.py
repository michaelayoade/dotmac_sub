"""Regression tests for the customer billing/state correctness slice.

The billing headline KPIs were previously summed in Jinja over the paginated
invoice page, so they were wrong past page one. These lock in that the KPIs are
computed by the read service over the COMPLETE invoice set and match the
canonical financial-position owner, that an unresolvable figure renders as
unknown rather than zero, and that service access is sourced from the access
resolver rather than the subscription's lifecycle status.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.billing import Invoice, InvoiceStatus
from app.models.catalog import BillingMode
from app.models.subscriber import Subscriber
from app.services import customer_portal_context, customer_portal_flow_billing
from app.services.customer_financial_position import get_customer_billing_summary


def _subscriber(db_session) -> Subscriber:
    sub = Subscriber(
        first_name="Bill",
        last_name="Payer",
        email=f"kpi-{uuid.uuid4().hex}@example.com",
        billing_mode=BillingMode.postpaid,
    )
    db_session.add(sub)
    db_session.commit()
    return sub


def _invoice(
    db_session,
    sub,
    *,
    status=InvoiceStatus.issued,
    balance="100.00",
    due_at=None,
    currency="NGN",
    is_proforma=False,
) -> Invoice:
    inv = Invoice(
        account_id=sub.id,
        invoice_number=f"INV-{uuid.uuid4().hex[:8]}",
        status=status,
        total=Decimal(balance),
        balance_due=Decimal(balance),
        due_at=due_at,
        currency=currency,
        is_proforma=is_proforma,
        is_active=True,
    )
    db_session.add(inv)
    db_session.commit()
    return inv


def test_billing_kpis_computed_over_full_set_not_paginated_page(db_session):
    sub = _subscriber(db_session)
    future = datetime.now(UTC) + timedelta(days=10)
    for _ in range(3):
        _invoice(db_session, sub, status=InvoiceStatus.issued, due_at=future)
    for _ in range(2):
        _invoice(db_session, sub, status=InvoiceStatus.overdue)

    page = customer_portal_flow_billing.get_billing_page(
        db_session, {"account_id": str(sub.id)}, per_page=2, page=1
    )
    stats = page["billing_stats"]
    summary = get_customer_billing_summary(db_session, str(sub.id))

    # The page is capped at two rows, but the KPIs reflect the whole account —
    # the exact defect the old Jinja-over-the-page sum introduced.
    assert len(page["invoices"]) == 2
    assert page["total"] == 5
    assert stats["available"] is True
    # Total billed is the lifetime sum of all five invoices (500), never the
    # page-of-two sum (200).
    assert stats["total_billed"] == Decimal("500.00")
    # Outstanding/overdue are exactly the canonical financial-position figures.
    assert stats["outstanding"] == summary.outstanding
    assert stats["overdue"] == summary.overdue
    assert stats["overdue_count"] == summary.overdue_count


def test_billing_kpis_exclude_non_billed_and_other_currency_rows(db_session):
    sub = _subscriber(db_session)
    past = datetime.now(UTC) - timedelta(days=1)
    _invoice(db_session, sub, status=InvoiceStatus.issued, balance="100.00")
    _invoice(db_session, sub, status=InvoiceStatus.paid, balance="50.00")
    _invoice(db_session, sub, status=InvoiceStatus.draft, balance="900.00")
    _invoice(db_session, sub, status=InvoiceStatus.void, balance="800.00")
    _invoice(
        db_session,
        sub,
        status=InvoiceStatus.issued,
        balance="700.00",
        is_proforma=True,
    )
    _invoice(
        db_session,
        sub,
        status=InvoiceStatus.overdue,
        balance="600.00",
        due_at=past,
        currency="USD",
    )

    page = customer_portal_flow_billing.get_billing_page(
        db_session, {"account_id": str(sub.id)}
    )

    assert page["billing_stats"] == {
        "available": True,
        "currency": "NGN",
        "total_billed": Decimal("150.00"),
        "outstanding": Decimal("100.00"),
        "overdue": Decimal("0.00"),
        "overdue_count": 0,
    }


def test_billing_kpis_marked_unavailable_when_owner_fails(db_session, monkeypatch):
    sub = _subscriber(db_session)
    _invoice(db_session, sub)

    def _boom(*_a, **_k):
        raise RuntimeError("financial owner unavailable")

    monkeypatch.setattr(
        customer_portal_flow_billing, "get_customer_billing_summary", _boom
    )
    page = customer_portal_flow_billing.get_billing_page(
        db_session, {"account_id": str(sub.id)}
    )
    # Unavailable, never a misleading zero.
    assert page["billing_stats"] == {"available": False}


def test_dashboard_unknown_balance_is_not_reported_as_zero(db_session, monkeypatch):
    sub = _subscriber(db_session)

    def _boom(*_a, **_k):
        raise RuntimeError("balance source unavailable")

    monkeypatch.setattr(customer_portal_context, "get_available_balance", _boom)
    ctx = customer_portal_context.get_dashboard_context(
        db_session,
        {"account_id": str(sub.id), "subscriber_id": str(sub.id), "username": "Bill"},
    )
    assert ctx["account"].balance_available is False
    assert ctx["stats_error"] is True


def test_dashboard_service_access_comes_from_resolver(db_session, monkeypatch):
    sub = _subscriber(db_session)
    monkeypatch.setattr(
        customer_portal_context, "customer_is_restricted", lambda _db, _sid: True
    )
    ctx = customer_portal_context.get_dashboard_context(
        db_session,
        {"account_id": str(sub.id), "subscriber_id": str(sub.id), "username": "Bill"},
    )
    access = ctx["service_access"]
    assert access.known is True
    assert access.restricted is True
