"""Repair of postpaid services wrongly suspended by prepaid balance enforcement.

Clears the wrongful `prepaid` lock; reactivates only subs held by nothing else;
skips accounts with overdue debt; dry-run writes nothing.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.billing import InvoiceStatus
from app.models.catalog import BillingMode, Subscription, SubscriptionStatus
from app.models.enforcement_lock import EnforcementReason
from app.schemas.billing import InvoiceCreate
from app.services import billing as billing_service
from app.services.account_lifecycle import suspend_subscription
from app.services.prepaid_scope_repair import repair


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


def test_dry_run_restores_prepaid_only_postpaid_without_writing(
    db_session, subscriber, catalog_offer
):
    sub = _postpaid_active(db_session, subscriber, catalog_offer)
    suspend_subscription(
        db_session,
        str(sub.id),
        reason=EnforcementReason.prepaid,
        source="prepaid_enforcement",
    )

    result = repair(db_session, apply=False)

    assert result.candidates == 1
    assert result.restored == 1  # projected
    assert result.items[0].action == "would_restore"
    # Dry-run must not change state.
    db_session.refresh(sub)
    assert sub.status == SubscriptionStatus.suspended


def test_apply_restores_prepaid_only_postpaid(db_session, subscriber, catalog_offer):
    sub = _postpaid_active(db_session, subscriber, catalog_offer)
    suspend_subscription(
        db_session,
        str(sub.id),
        reason=EnforcementReason.prepaid,
        source="prepaid_enforcement",
    )

    result = repair(db_session, apply=True)

    assert result.restored == 1
    assert result.items[0].action == "restored"
    db_session.refresh(sub)
    assert sub.status == SubscriptionStatus.active


def test_apply_clears_prepaid_lock_but_keeps_suspended_when_overdue_lock_remains(
    db_session, subscriber, catalog_offer
):
    sub = _postpaid_active(db_session, subscriber, catalog_offer)
    # A lingering overdue lock with NO actual overdue invoice (stale-lock drift).
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

    result = repair(db_session, apply=True)

    assert result.candidates == 1
    assert result.restored == 0
    assert result.lock_cleared_only == 1
    assert result.items[0].action == "lock_cleared_not_restored"
    assert "overdue" in result.items[0].other_active_locks
    db_session.refresh(sub)
    assert sub.status == SubscriptionStatus.suspended  # overdue lock still holds


def test_account_with_overdue_debt_is_skipped(db_session, subscriber, catalog_offer):
    sub = _postpaid_active(db_session, subscriber, catalog_offer)
    suspend_subscription(
        db_session,
        str(sub.id),
        reason=EnforcementReason.prepaid,
        source="prepaid_enforcement",
    )
    billing_service.invoices.create(
        db_session,
        InvoiceCreate(
            account_id=subscriber.id,
            # issued + due_at in the past => counts as overdue debt.
            status=InvoiceStatus.issued,
            total=Decimal("5000.00"),
            balance_due=Decimal("5000.00"),
            issued_at=datetime.now(UTC) - timedelta(days=30),
            due_at=datetime.now(UTC) - timedelta(days=10),
        ),
    )
    db_session.commit()

    result = repair(db_session, apply=True)

    assert result.skipped == 1
    assert result.items[0].action == "skipped"
    db_session.refresh(sub)
    assert sub.status == SubscriptionStatus.suspended
