"""Reconcile of stale `overdue` enforcement locks (lock active, no overdue debt).

Clears the stale overdue lock; reactivates subs held by nothing else; leaves
subs held by another lock suspended; ignores accounts that owe overdue debt.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.billing import InvoiceStatus
from app.models.catalog import BillingMode, Subscription, SubscriptionStatus
from app.models.enforcement_lock import EnforcementReason
from app.schemas.billing import InvoiceCreate
from app.services import billing as billing_service
from app.services.account_lifecycle import suspend_subscription
from app.services.stale_overdue_lock_reconcile import reconcile


def _postpaid_active(db, subscriber, offer):
    sub = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=SubscriptionStatus.active,
        billing_mode=BillingMode.postpaid,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def test_apply_restores_stale_overdue_only(db_session, subscriber, catalog_offer):
    sub = _postpaid_active(db_session, subscriber, catalog_offer)
    # Overdue lock but NO overdue invoice => stale.
    suspend_subscription(
        db_session,
        str(sub.id),
        reason=EnforcementReason.overdue,
        source="invoice:stale",
    )

    result = reconcile(db_session, apply=True)

    assert result.candidates == 1
    assert result.restored == 1
    assert result.items[0].action == "restored"
    db_session.refresh(sub)
    assert sub.status == SubscriptionStatus.active


def test_other_lock_keeps_suspended_after_clearing_overdue(
    db_session, subscriber, catalog_offer
):
    sub = _postpaid_active(db_session, subscriber, catalog_offer)
    suspend_subscription(
        db_session,
        str(sub.id),
        reason=EnforcementReason.overdue,
        source="invoice:stale",
    )
    suspend_subscription(
        db_session,
        str(sub.id),
        reason=EnforcementReason.prepaid,
        source="prepaid_enforcement",
    )

    result = reconcile(db_session, apply=True)

    assert result.candidates == 1
    assert result.restored == 0
    assert result.lock_cleared_only == 1
    assert "prepaid" in result.items[0].other_active_locks
    db_session.refresh(sub)
    assert sub.status == SubscriptionStatus.suspended


def test_account_with_real_overdue_debt_is_not_a_candidate(
    db_session, subscriber, catalog_offer
):
    sub = _postpaid_active(db_session, subscriber, catalog_offer)
    suspend_subscription(
        db_session,
        str(sub.id),
        reason=EnforcementReason.overdue,
        source="invoice:real",
    )
    billing_service.invoices.create(
        db_session,
        InvoiceCreate(
            account_id=subscriber.id,
            status=InvoiceStatus.issued,  # past due_at => real overdue debt
            total=Decimal("5000.00"),
            balance_due=Decimal("5000.00"),
            issued_at=datetime.now(UTC) - timedelta(days=30),
            due_at=datetime.now(UTC) - timedelta(days=10),
        ),
    )
    db_session.commit()

    result = reconcile(db_session, apply=True)

    assert result.candidates == 0
    db_session.refresh(sub)
    assert sub.status == SubscriptionStatus.suspended


def test_dry_run_writes_nothing(db_session, subscriber, catalog_offer):
    sub = _postpaid_active(db_session, subscriber, catalog_offer)
    suspend_subscription(
        db_session,
        str(sub.id),
        reason=EnforcementReason.overdue,
        source="invoice:stale",
    )

    result = reconcile(db_session, apply=False)

    assert result.candidates == 1
    assert result.restored == 1  # projected
    db_session.refresh(sub)
    assert sub.status == SubscriptionStatus.suspended
