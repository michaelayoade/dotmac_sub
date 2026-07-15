"""Tests for admin change-plan effective-timing (instant vs next-cycle).

Covers Catalog C-4: the admin change-plan flow can either apply immediately
(with proration, unchanged) or schedule the swap for the next billing cycle.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.models.billing import LedgerEntry, LedgerEntryType, LedgerSource
from app.models.catalog import SubscriptionStatus
from app.models.subscription_change import (
    SubscriptionChangeRequest,
    SubscriptionChangeStatus,
)
from app.services import catalog as catalog_service
from app.services import web_catalog_subscriptions as core
from app.services.subscription_changes import subscription_change_requests

# Reuse the offer/subscription builders from the prepaid plan-change suite.
from tests.test_customer_plan_change_prepaid import (
    _confirmation_kwargs,
    _make_offer,
    _make_subscription,
    _stub_plan_change_side_effects,
)


def _same_family_offers(db_session):
    current = _make_offer(
        db_session,
        name="Unlimited Basic",
        amount=Decimal("100.00"),
        plan_family="unlimited",
    )
    target = _make_offer(
        db_session,
        name="Unlimited Plus",
        amount=Decimal("150.00"),
        plan_family="unlimited",
    )
    return current, target


def test_instant_change_swaps_offer_now(db_session, subscriber, monkeypatch):
    """One owner-previewed instant change swaps now with exact evidence."""
    _stub_plan_change_side_effects(monkeypatch)
    current, target = _same_family_offers(db_session)
    subscription = _make_subscription(
        db_session,
        subscriber,
        current,
        next_billing_at=datetime.now(UTC) + timedelta(days=15),
        start_at=datetime.now(UTC) - timedelta(days=15),
    )
    db_session.add(
        LedgerEntry(
            account_id=subscriber.id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.payment,
            amount=Decimal("25.00"),
            currency="NGN",
            memo="Wallet top-up for prorated admin plan change",
        )
    )
    db_session.commit()

    result = core.bulk_change_plan(
        db_session,
        str(subscription.id),
        str(target.id),
        request=None,
        actor_id=None,
        preview_fingerprint=_confirmation_kwargs(db_session, subscription, target)[
            "preview_fingerprint"
        ],
        idempotency_key="admin-instant-test",
    )

    assert result["changed"] == 1
    db_session.refresh(subscription)
    assert str(subscription.offer_id) == str(target.id)
    request = (
        db_session.query(SubscriptionChangeRequest)
        .filter(SubscriptionChangeRequest.subscription_id == subscription.id)
        .one()
    )
    assert request.status == SubscriptionChangeStatus.applied
    assert request.confirmation_preview_fingerprint
    assert request.account_adjustment_id is not None
    assert request.ledger_entry_id is not None


def test_next_cycle_records_scheduled_change_without_swapping(
    db_session, subscriber, monkeypatch
):
    """next_cycle records an approved future-dated change and does NOT swap now."""
    _stub_plan_change_side_effects(monkeypatch)
    current, target = _same_family_offers(db_session)
    next_billing = datetime.now(UTC) + timedelta(days=15)
    subscription = _make_subscription(
        db_session,
        subscriber,
        current,
        next_billing_at=next_billing,
        start_at=datetime.now(UTC) - timedelta(days=15),
    )

    result = core.bulk_change_plan(
        db_session,
        str(subscription.id),
        str(target.id),
        request=None,
        actor_id=None,
        effective_timing="next_cycle",
    )

    assert result["changed"] == 1
    # Offer is unchanged now.
    db_session.refresh(subscription)
    assert str(subscription.offer_id) == str(current.id)
    # A single approved, unapplied scheduled change exists, effective next cycle.
    scheduled = subscription_change_requests.get_scheduled_for_subscription(
        db_session, str(subscription.id)
    )
    assert scheduled is not None
    assert scheduled.status == SubscriptionChangeStatus.approved
    assert scheduled.applied_at is None
    assert str(scheduled.requested_offer_id) == str(target.id)
    assert scheduled.effective_date == next_billing.date()


def test_next_cycle_rejects_duplicate_outstanding_change(
    db_session, subscriber, monkeypatch
):
    """A second next-cycle schedule is rejected while one is still outstanding."""
    _stub_plan_change_side_effects(monkeypatch)
    current, target = _same_family_offers(db_session)
    subscription = _make_subscription(
        db_session,
        subscriber,
        current,
        next_billing_at=datetime.now(UTC) + timedelta(days=10),
        start_at=datetime.now(UTC) - timedelta(days=20),
    )

    first = core.bulk_change_plan(
        db_session,
        str(subscription.id),
        str(target.id),
        request=None,
        actor_id=None,
        effective_timing="next_cycle",
    )
    assert first["changed"] == 1

    second = core.bulk_change_plan(
        db_session,
        str(subscription.id),
        str(target.id),
        request=None,
        actor_id=None,
        effective_timing="next_cycle",
    )
    # The lifecycle owner reports the duplicate as ineligible, not an execution
    # failure, and leaves the original scheduled change untouched.
    assert second["changed"] == 0
    assert second["skipped_ids"] == [str(subscription.id)]
    assert second["failed_ids"] == []


def test_applier_swaps_offer_when_due(db_session, subscriber, monkeypatch):
    """The applier swaps the offer for a scheduled change whose date has arrived."""
    _stub_plan_change_side_effects(monkeypatch)
    current, target = _same_family_offers(db_session)
    subscription = _make_subscription(
        db_session,
        subscriber,
        current,
        next_billing_at=datetime.now(UTC) + timedelta(days=5),
        start_at=datetime.now(UTC) - timedelta(days=25),
    )

    scheduled = subscription_change_requests.schedule(
        db_session,
        subscription_id=str(subscription.id),
        new_offer_id=str(target.id),
        effective_date=(datetime.now(UTC) - timedelta(days=1)).date(),
    )

    result = subscription_change_requests.apply_due_changes(db_session)

    assert result["applied"] == 1
    assert result["failed_ids"] == []
    db_session.refresh(subscription)
    assert str(subscription.offer_id) == str(target.id)
    db_session.refresh(scheduled)
    assert scheduled.status == SubscriptionChangeStatus.applied
    assert scheduled.applied_at is not None


def test_applier_auto_cancels_due_change_when_target_subscription_canceled(
    db_session, subscriber, monkeypatch
):
    """Stale scheduled changes on terminal subscriptions stop retrying."""
    _stub_plan_change_side_effects(monkeypatch)
    current, target = _same_family_offers(db_session)
    subscription = _make_subscription(
        db_session,
        subscriber,
        current,
        next_billing_at=datetime.now(UTC) + timedelta(days=5),
        start_at=datetime.now(UTC) - timedelta(days=25),
    )
    scheduled = subscription_change_requests.schedule(
        db_session,
        subscription_id=str(subscription.id),
        new_offer_id=str(target.id),
        effective_date=(datetime.now(UTC) - timedelta(days=1)).date(),
    )
    subscription.status = SubscriptionStatus.canceled
    subscription.canceled_at = datetime.now(UTC)
    db_session.commit()

    result = subscription_change_requests.apply_due_changes(db_session)

    assert result["applied"] == 0
    assert result["canceled_ids"] == [str(scheduled.id)]
    assert result["failed_ids"] == []
    db_session.refresh(subscription)
    assert str(subscription.offer_id) == str(current.id)
    db_session.refresh(scheduled)
    assert scheduled.status == SubscriptionChangeStatus.canceled
    assert scheduled.applied_at is None
    assert "target subscription is canceled" in (scheduled.notes or "")


def test_applier_marks_due_change_applied_when_subscription_already_on_target_offer(
    db_session, subscriber, monkeypatch
):
    """A retry after a prior partial apply is treated as idempotent."""
    _stub_plan_change_side_effects(monkeypatch)
    current, target = _same_family_offers(db_session)
    subscription = _make_subscription(
        db_session,
        subscriber,
        current,
        next_billing_at=datetime.now(UTC) + timedelta(days=5),
        start_at=datetime.now(UTC) - timedelta(days=25),
    )
    scheduled = subscription_change_requests.schedule(
        db_session,
        subscription_id=str(subscription.id),
        new_offer_id=str(target.id),
        effective_date=(datetime.now(UTC) - timedelta(days=1)).date(),
    )
    subscription.offer_id = target.id
    db_session.commit()

    def _fail_update(*args, **kwargs):
        raise AssertionError("already-applied retry should not update subscription")

    monkeypatch.setattr(catalog_service.subscriptions, "update", _fail_update)

    result = subscription_change_requests.apply_due_changes(db_session)

    assert result["applied"] == 1
    assert result["canceled_ids"] == []
    assert result["failed_ids"] == []
    db_session.refresh(scheduled)
    assert scheduled.status == SubscriptionChangeStatus.applied
    assert scheduled.applied_at is not None
    assert "already on the requested offer" in (scheduled.notes or "")


def test_applier_ignores_future_dated_change(db_session, subscriber, monkeypatch):
    """A change whose effective date is still in the future is not yet applied."""
    _stub_plan_change_side_effects(monkeypatch)
    current, target = _same_family_offers(db_session)
    subscription = _make_subscription(
        db_session,
        subscriber,
        current,
        next_billing_at=datetime.now(UTC) + timedelta(days=15),
        start_at=datetime.now(UTC) - timedelta(days=15),
    )

    subscription_change_requests.schedule(
        db_session,
        subscription_id=str(subscription.id),
        new_offer_id=str(target.id),
        effective_date=(datetime.now(UTC) + timedelta(days=15)).date(),
    )

    result = subscription_change_requests.apply_due_changes(db_session)

    assert result["applied"] == 0
    db_session.refresh(subscription)
    assert str(subscription.offer_id) == str(current.id)


def test_cancel_scheduled_change(db_session, subscriber, monkeypatch):
    """A scheduled change can be canceled before it is applied."""
    _stub_plan_change_side_effects(monkeypatch)
    current, target = _same_family_offers(db_session)
    subscription = _make_subscription(
        db_session,
        subscriber,
        current,
        next_billing_at=datetime.now(UTC) + timedelta(days=15),
        start_at=datetime.now(UTC) - timedelta(days=15),
    )
    scheduled = subscription_change_requests.schedule(
        db_session,
        subscription_id=str(subscription.id),
        new_offer_id=str(target.id),
        effective_date=(datetime.now(UTC) + timedelta(days=15)).date(),
    )

    subscription_change_requests.cancel_scheduled(db_session, str(scheduled.id))

    db_session.refresh(scheduled)
    assert scheduled.status == SubscriptionChangeStatus.canceled
    assert (
        subscription_change_requests.get_scheduled_for_subscription(
            db_session, str(subscription.id)
        )
        is None
    )
    # Canceled changes are skipped by the applier even once due.
    scheduled.effective_date = (datetime.now(UTC) - timedelta(days=1)).date()
    db_session.commit()
    result = subscription_change_requests.apply_due_changes(db_session)
    assert result["applied"] == 0


def test_invalid_effective_timing_raises(db_session, subscriber, monkeypatch):
    _stub_plan_change_side_effects(monkeypatch)
    current, target = _same_family_offers(db_session)
    subscription = _make_subscription(
        db_session,
        subscriber,
        current,
        next_billing_at=datetime.now(UTC) + timedelta(days=15),
        start_at=datetime.now(UTC) - timedelta(days=15),
    )
    with pytest.raises(ValueError):
        core.bulk_change_plan(
            db_session,
            str(subscription.id),
            str(target.id),
            request=None,
            actor_id=None,
            effective_timing="bogus",
        )
