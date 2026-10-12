"""Canceled-account visibility in the reseller portal."""

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


def _account(db_session, reseller, status, *, metadata=None):
    sub = Subscriber(
        first_name="Acc",
        last_name=status.value,
        email=f"{uuid.uuid4()}@example.com",
        reseller_id=reseller.id,
        status=status,
        is_active=(status != SubscriberStatus.canceled),
        metadata_=metadata or {},
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


def test_canceled_accounts_are_exact_filter_only_and_metrics_stay_excluded(
    db_session, reseller
):
    active = _account(db_session, reseller, SubscriberStatus.active)
    canceled = _account(db_session, reseller, SubscriberStatus.canceled)

    _invoice(db_session, active.id, "100.00")  # counts
    _invoice(db_session, active.id, "999.00", is_active=False)  # soft-deleted invoice
    _invoice(db_session, canceled.id, "500.00")  # canceled account → excluded

    rid = str(reseller.id)

    # The default list + count exclude the canceled account.
    listed = reseller_portal.list_accounts(db_session, rid, 50, 0)
    listed_ids = {row["id"] for row in listed}
    assert str(active.id) in listed_ids
    assert str(canceled.id) not in listed_ids
    assert reseller_portal.count_accounts(db_session, rid) == 1

    # The exact canceled filter exposes the retained row as read-only.
    canceled_filter = reseller_portal.ResellerAccountListStatusFilter.canceled
    canceled_result = reseller_portal.query_reseller_account_list(
        db_session,
        reseller_portal.ResellerAccountListQuery(
            reseller_id=reseller.id,
            limit=50,
            offset=0,
            status_filter=canceled_filter,
        ),
    )
    assert canceled_result.total == 1
    assert [row.id for row in canceled_result.items] == [canceled.id]
    assert canceled_result.items[0].is_read_only is True
    assert reseller_portal.owned_account(db_session, rid, str(canceled.id)) is None
    assert reseller_portal.get_account_detail(db_session, rid, str(canceled.id)) is None

    # Dashboard KPIs: only the active account, and only its live invoice (100) —
    # not the canceled account's 500, nor the soft-deleted 999.
    summary = reseller_portal.get_dashboard_summary(db_session, rid, 50, 0)
    assert summary["totals"]["accounts"] == 1
    assert Decimal(str(summary["totals"]["open_balance"])) == Decimal("100.00")

    # Revenue outstanding excludes both the canceled account and the inactive invoice.
    revenue = reseller_portal.get_revenue_summary(db_session, rid)
    assert Decimal(str(revenue["total_outstanding"])) == Decimal("100.00")


def test_canceled_filter_keeps_reseller_scope_and_legacy_deletions_excluded(
    db_session, reseller
):
    retained_one = _account(db_session, reseller, SubscriberStatus.canceled)
    retained_two = _account(db_session, reseller, SubscriberStatus.canceled)
    legacy_deleted = _account(
        db_session,
        reseller,
        SubscriberStatus.canceled,
        metadata={"splynx_deleted": True},
    )
    other_reseller = Reseller(name="Other Reseller", code="OTHER-CANCELED")
    db_session.add(other_reseller)
    db_session.commit()
    foreign = _account(db_session, other_reseller, SubscriberStatus.canceled)

    status_filter = reseller_portal.ResellerAccountListStatusFilter.canceled
    first_result = reseller_portal.query_reseller_account_list(
        db_session,
        reseller_portal.ResellerAccountListQuery(
            reseller_id=reseller.id,
            limit=1,
            offset=0,
            status_filter=status_filter,
        ),
    )
    second_result = reseller_portal.query_reseller_account_list(
        db_session,
        reseller_portal.ResellerAccountListQuery(
            reseller_id=reseller.id,
            limit=1,
            offset=1,
            status_filter=status_filter,
        ),
    )
    assert first_result.total == second_result.total == 2
    visible_ids = {first_result.items[0].id, second_result.items[0].id}
    assert visible_ids == {retained_one.id, retained_two.id}
    assert legacy_deleted.id not in visible_ids
    assert foreign.id not in visible_ids


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
        db_session,
        rid,
        50,
        0,
        status_filter=reseller_portal.ResellerAccountListStatusFilter.disabled,
    )
    assert (
        reseller_portal.count_accounts(
            db_session,
            rid,
            status_filter=reseller_portal.ResellerAccountListStatusFilter.disabled,
        )
        == 1
    )
    assert [row["id"] for row in disabled_rows] == [str(disabled.id)]
