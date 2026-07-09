from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.billing import Invoice, InvoiceStatus
from app.models.catalog import (
    AccessType,
    BillingCycle,
    BillingMode,
    CatalogOffer,
    PriceBasis,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.enforcement_lock import EnforcementLock, EnforcementReason
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services.billing_cleanup_remediation import (
    apply_cleanup_remediation,
    plan_account_mode_row,
    plan_anchor_row,
    plan_cleanup_remediation,
    plan_stale_overdue_lock_row,
)


def _account(db, *, mode=BillingMode.prepaid):
    account = Subscriber(
        first_name="Cleanup",
        last_name="Target",
        email=f"{uuid.uuid4().hex}@example.com",
        status=SubscriberStatus.active,
        billing_mode=mode,
        is_active=True,
    )
    db.add(account)
    db.flush()
    return account


def _offer(db, *, mode=BillingMode.prepaid):
    offer = CatalogOffer(
        name=f"Cleanup {mode.value}",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_cycle=BillingCycle.monthly,
        billing_mode=mode,
        is_active=True,
    )
    db.add(offer)
    db.flush()
    return offer


def _subscription(
    db,
    account,
    *,
    mode=BillingMode.prepaid,
    status=SubscriptionStatus.active,
    next_billing_at=None,
):
    subscription = Subscription(
        subscriber_id=account.id,
        offer_id=_offer(db, mode=mode).id,
        status=status,
        billing_mode=mode,
        next_billing_at=next_billing_at,
    )
    db.add(subscription)
    db.flush()
    return subscription


def test_resolves_stale_overdue_lock_and_restores_subscription(db_session):
    account = _account(db_session, mode=BillingMode.postpaid)
    subscription = _subscription(
        db_session,
        account,
        mode=BillingMode.postpaid,
        status=SubscriptionStatus.suspended,
    )
    lock = EnforcementLock(
        subscription_id=subscription.id,
        subscriber_id=account.id,
        reason=EnforcementReason.overdue,
        source="invoice:test",
        is_active=True,
    )
    db_session.add(lock)
    db_session.commit()

    row = {
        "lock_id": str(lock.id),
        "account_id": str(account.id),
        "subscription_id": str(subscription.id),
        "source": "invoice:test",
    }
    item = plan_stale_overdue_lock_row(db_session, row)
    assert item["decision"] == "apply"
    assert item["would_restore"] is True

    result = apply_cleanup_remediation(db_session, {"items": [item]}, dry_run=False)

    db_session.refresh(lock)
    db_session.refresh(subscription)
    assert result["applied_count"] == 1
    assert lock.is_active is False
    assert subscription.status == SubscriptionStatus.active


def test_stale_lock_plan_refuses_when_account_still_has_overdue_ar(db_session):
    subscriber = _account(db_session, mode=BillingMode.postpaid)
    invoice = Invoice(
        account_id=subscriber.id,
        invoice_number="INV-COLLECTIBLE-AR",
        status=InvoiceStatus.overdue,
        currency="NGN",
        subtotal=Decimal("100.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("100.00"),
        balance_due=Decimal("100.00"),
        due_at=datetime(2026, 7, 1, tzinfo=UTC),
        is_active=True,
    )
    db_session.add(invoice)
    db_session.flush()
    subscription = _subscription(
        db_session,
        subscriber,
        mode=BillingMode.postpaid,
        status=SubscriptionStatus.suspended,
    )
    lock = EnforcementLock(
        subscription_id=subscription.id,
        subscriber_id=subscriber.id,
        reason=EnforcementReason.overdue,
        source=f"invoice:{invoice.id}",
        is_active=True,
    )
    db_session.add(lock)
    db_session.commit()

    item = plan_stale_overdue_lock_row(
        db_session,
        {
            "lock_id": str(lock.id),
            "account_id": str(subscriber.id),
            "subscription_id": str(subscription.id),
            "source": lock.source,
        },
    )

    assert item["decision"] == "refuse"
    assert item["reason"] == "account_has_collectible_overdue_ar"


def test_advances_prepaid_next_billing_anchor(db_session):
    account = _account(db_session, mode=BillingMode.prepaid)
    current = datetime(2026, 7, 1, tzinfo=UTC)
    target = current + timedelta(days=10)
    subscription = _subscription(
        db_session,
        account,
        mode=BillingMode.prepaid,
        next_billing_at=current,
    )
    db_session.commit()

    item = plan_anchor_row(
        db_session,
        {
            "account_id": str(account.id),
            "subscription_id": str(subscription.id),
            "current_next_billing_at": current.isoformat(),
            "paid_through": target.isoformat(),
        },
    )
    assert item["decision"] == "apply"

    apply_cleanup_remediation(db_session, {"items": [item]}, dry_run=False)

    db_session.refresh(subscription)
    assert subscription.next_billing_at.replace(tzinfo=UTC) == target


def test_anchor_plan_refuses_if_anchor_changed_since_audit(db_session):
    account = _account(db_session, mode=BillingMode.prepaid)
    current = datetime(2026, 7, 5, tzinfo=UTC)
    subscription = _subscription(
        db_session,
        account,
        mode=BillingMode.prepaid,
        next_billing_at=current,
    )
    db_session.commit()

    item = plan_anchor_row(
        db_session,
        {
            "account_id": str(account.id),
            "subscription_id": str(subscription.id),
            "current_next_billing_at": datetime(2026, 7, 1, tzinfo=UTC).isoformat(),
            "paid_through": datetime(2026, 7, 10, tzinfo=UTC).isoformat(),
        },
    )

    assert item["decision"] == "refuse"
    assert item["reason"] == "next_billing_at_changed_since_audit"


def test_aligns_account_mode_when_single_live_subscription_mode(db_session):
    account = _account(db_session, mode=BillingMode.prepaid)
    subscription = _subscription(db_session, account, mode=BillingMode.postpaid)
    db_session.commit()

    item = plan_account_mode_row(
        db_session,
        {
            "issue": "subscription_vs_account",
            "subscriber_id": str(account.id),
            "subscription_id": str(subscription.id),
            "subscription_mode": "postpaid",
            "account_mode": "prepaid",
        },
    )
    assert item["decision"] == "apply"

    apply_cleanup_remediation(db_session, {"items": [item]}, dry_run=False)

    db_session.refresh(account)
    assert account.billing_mode == BillingMode.postpaid


def test_account_mode_plan_refuses_mixed_live_modes(db_session):
    account = _account(db_session, mode=BillingMode.prepaid)
    _subscription(db_session, account, mode=BillingMode.prepaid)
    _subscription(db_session, account, mode=BillingMode.postpaid)
    db_session.commit()

    item = plan_account_mode_row(
        db_session,
        {
            "issue": "subscription_vs_account",
            "subscriber_id": str(account.id),
            "subscription_mode": "postpaid",
            "account_mode": "prepaid",
        },
    )

    assert item["decision"] == "refuse"
    assert item["reason"] == "mixed_or_changed_live_subscription_modes"


def test_plan_cleanup_remediation_combines_counts(db_session):
    account = _account(db_session, mode=BillingMode.prepaid)
    current = datetime(2026, 7, 1, tzinfo=UTC)
    subscription = _subscription(
        db_session,
        account,
        mode=BillingMode.prepaid,
        next_billing_at=current,
    )
    db_session.commit()

    plan = plan_cleanup_remediation(
        db_session,
        anchor_rows=[
            {
                "account_id": str(account.id),
                "subscription_id": str(subscription.id),
                "current_next_billing_at": current.isoformat(),
                "paid_through": datetime(2026, 7, 10, tzinfo=UTC).isoformat(),
            }
        ],
        mode_rows=[{"issue": "subscription_vs_offer"}],
    )

    assert plan["counts"]["apply"] == 1
    assert plan["counts"]["refuse"] == 1
