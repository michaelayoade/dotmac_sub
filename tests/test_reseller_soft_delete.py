"""Soft-deleted (canceled) accounts and inactive invoices must not leak into the
reseller portal's lists, counts, balances, or revenue."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.models.billing import Invoice, InvoiceStatus
from app.models.subscriber import Reseller, Subscriber, SubscriberStatus
from app.services import reseller_portal


@pytest.fixture()
def reseller(db_session):
    r = Reseller(name="SoftDelete Co", code="SOFTDEL")
    db_session.add(r)
    db_session.commit()
    db_session.refresh(r)
    return r


def _account(db_session, reseller, status):
    sub = Subscriber(
        first_name="Acc",
        last_name=status.value,
        email=f"{uuid.uuid4()}@example.com",
        reseller_id=reseller.id,
        status=status,
        is_active=(status != SubscriberStatus.canceled),
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)
    return sub


def _invoice(db_session, account_id, balance, *, is_active=True):
    inv = Invoice(
        account_id=account_id,
        invoice_number=f"INV-{uuid.uuid4()}",
        status=InvoiceStatus.issued,
        subtotal=Decimal(balance),
        tax_total=Decimal("0.00"),
        total=Decimal(balance),
        balance_due=Decimal(balance),
        issued_at=datetime.now(UTC) - timedelta(days=14),
        due_at=datetime.now(UTC) + timedelta(days=1),
        is_active=is_active,
    )
    db_session.add(inv)
    db_session.commit()
    return inv


def test_canceled_accounts_and_inactive_invoices_excluded(db_session, reseller):
    active = _account(db_session, reseller, SubscriberStatus.active)
    canceled = _account(db_session, reseller, SubscriberStatus.canceled)

    _invoice(db_session, active.id, "100.00")  # counts
    _invoice(db_session, active.id, "999.00", is_active=False)  # soft-deleted invoice
    _invoice(db_session, canceled.id, "500.00")  # canceled account → excluded

    rid = str(reseller.id)

    # List + count exclude the canceled account.
    listed = reseller_portal.list_accounts(db_session, rid, 50, 0)
    listed_ids = {row["id"] for row in listed}
    assert str(active.id) in listed_ids
    assert str(canceled.id) not in listed_ids
    assert reseller_portal.count_accounts(db_session, rid) == 1

    # Dashboard KPIs: only the active account, and only its live invoice (100) —
    # not the canceled account's 500, nor the soft-deleted 999.
    summary = reseller_portal.get_dashboard_summary(db_session, rid, 50, 0)
    assert summary["totals"]["accounts"] == 1
    assert Decimal(str(summary["totals"]["open_balance"])) == Decimal("100.00")

    # Revenue outstanding excludes both the canceled account and the inactive invoice.
    revenue = reseller_portal.get_revenue_summary(db_session, rid)
    assert Decimal(str(revenue["total_outstanding"])) == Decimal("100.00")


def test_disabled_accounts_hidden_by_default_but_filterable(db_session, reseller):
    # Disabled accounts are deactivated-but-real records. The normal reseller
    # list hides them, but the status filter can still find them.
    active = _account(db_session, reseller, SubscriberStatus.active)
    disabled = _account(db_session, reseller, SubscriberStatus.disabled)
    rid = str(reseller.id)

    assert reseller_portal.count_accounts(db_session, rid) == 1
    assert [
        row["id"] for row in reseller_portal.list_accounts(db_session, rid, 50, 0)
    ] == [str(active.id)]

    disabled_rows = reseller_portal.list_accounts(
        db_session, rid, 50, 0, status_filter="disabled"
    )
    assert (
        reseller_portal.count_accounts(db_session, rid, status_filter="disabled") == 1
    )
    assert [row["id"] for row in disabled_rows] == [str(disabled.id)]
