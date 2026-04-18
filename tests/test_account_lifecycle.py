"""Scenario tests for subscription/account lifecycle state machine."""

import uuid

import pytest
from sqlalchemy.orm import Session

from app.models.catalog import (
    BillingMode,
    CatalogOffer,
    Subscription,
    SubscriptionStatus,
)
from app.models.enforcement_lock import EnforcementReason
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services.account_lifecycle import (
    ALLOWED_RESTORERS,
    activate_subscription,
    cancel_subscription,
    compute_account_status,
    expire_subscription,
    has_active_lock,
    restore_subscription,
    suspend_subscription,
)
from app.services.events import emit_event
from app.services.events.types import EventType


def _make_subscriber(db: Session, **kwargs) -> Subscriber:
    defaults = {
        "first_name": "Test",
        "last_name": "User",
        "email": f"test-{uuid.uuid4().hex[:8]}@example.com",
        "status": SubscriberStatus.active,
    }
    defaults.update(kwargs)
    sub = Subscriber(**defaults)
    db.add(sub)
    db.flush()
    return sub


def _make_offer(db: Session) -> CatalogOffer:
    from app.models.catalog import AccessType, OfferStatus, PriceBasis, ServiceType

    offer = CatalogOffer(
        name=f"Test Offer {uuid.uuid4().hex[:6]}",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        status=OfferStatus.active,
        is_active=True,
    )
    db.add(offer)
    db.flush()
    return offer


def _make_subscription(
    db: Session, subscriber: Subscriber, offer: CatalogOffer, **kwargs
) -> Subscription:
    defaults = {
        "subscriber_id": subscriber.id,
        "offer_id": offer.id,
        "status": SubscriptionStatus.active,
        "billing_mode": BillingMode.prepaid,
    }
    defaults.update(kwargs)
    sub = Subscription(**defaults)
    db.add(sub)
    db.flush()
    return sub


class TestSuspendSubscription:
    """Tests for suspend_subscription."""

    def test_creates_lock_and_suspends(self, db_session):
        """Single lock: subscription moves to suspended."""
        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        subscription = _make_subscription(db_session, subscriber, offer)

        lock = suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.overdue,
            source="dunning_case:test-123",
            emit=False,
        )

        assert lock.is_active is True
        assert lock.reason == EnforcementReason.overdue
        assert subscription.status == SubscriptionStatus.suspended
        assert subscriber.status == SubscriberStatus.suspended

    def test_idempotent_suspend_creates_second_lock(self, db_session):
        """Suspending twice creates two locks, subscription stays suspended."""
        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        subscription = _make_subscription(db_session, subscriber, offer)

        lock1 = suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.overdue,
            source="dunning_case:1",
            emit=False,
        )
        lock2 = suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.fup,
            source="fup_rule:42",
            emit=False,
        )

        assert lock1.id != lock2.id
        assert lock1.is_active is True
        assert lock2.is_active is True
        assert subscription.status == SubscriptionStatus.suspended

    def test_cannot_suspend_canceled_subscription(self, db_session):
        """Suspension of a terminal subscription raises ValueError."""
        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        subscription = _make_subscription(
            db_session, subscriber, offer, status=SubscriptionStatus.canceled
        )

        with pytest.raises(ValueError, match="Cannot suspend"):
            suspend_subscription(
                db_session,
                str(subscription.id),
                reason=EnforcementReason.admin,
                source="admin:test",
                emit=False,
            )


class TestRestoreSubscription:
    """Tests for restore_subscription."""

    def test_single_lock_restore(self, db_session):
        """Payment clears overdue lock, subscription restores."""
        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        subscription = _make_subscription(db_session, subscriber, offer)

        suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.overdue,
            source="dunning_case:1",
            emit=False,
        )
        assert subscription.status == SubscriptionStatus.suspended

        restored = restore_subscription(
            db_session,
            str(subscription.id),
            trigger="payment",
            resolved_by="payment:pay-123",
            emit=False,
        )

        assert restored is True
        assert subscription.status == SubscriptionStatus.active
        assert subscriber.status == SubscriberStatus.active

    def test_dual_lock_partial_restore(self, db_session):
        """Overdue + FUP locks. Payment clears overdue but FUP remains."""
        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        subscription = _make_subscription(db_session, subscriber, offer)

        suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.overdue,
            source="dunning_case:1",
            emit=False,
        )
        suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.fup,
            source="fup_rule:42",
            emit=False,
        )

        restored = restore_subscription(
            db_session,
            str(subscription.id),
            trigger="payment",
            resolved_by="payment:pay-456",
            emit=False,
        )

        assert restored is False  # FUP lock still active
        assert subscription.status == SubscriptionStatus.suspended
        assert has_active_lock(db_session, str(subscription.id), EnforcementReason.fup)

    def test_dual_lock_full_restore(self, db_session):
        """Overdue + FUP both cleared → subscription restores."""
        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        subscription = _make_subscription(db_session, subscriber, offer)

        suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.overdue,
            source="dunning_case:1",
            emit=False,
        )
        suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.fup,
            source="fup_rule:42",
            emit=False,
        )

        restore_subscription(
            db_session,
            str(subscription.id),
            trigger="payment",
            resolved_by="payment:pay-1",
            emit=False,
        )
        restored = restore_subscription(
            db_session,
            str(subscription.id),
            trigger="cap_reset",
            resolved_by="fup_reset",
            emit=False,
        )

        assert restored is True
        assert subscription.status == SubscriptionStatus.active

    def test_unauthorized_restorer_rejected(self, db_session):
        """Payment cannot clear FUP lock."""
        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        subscription = _make_subscription(db_session, subscriber, offer)

        suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.fup,
            source="fup_rule:1",
            emit=False,
        )

        restored = restore_subscription(
            db_session,
            str(subscription.id),
            trigger="payment",
            resolved_by="payment:pay-1",
            emit=False,
        )

        assert restored is False
        assert subscription.status == SubscriptionStatus.suspended

    def test_admin_lock_only_admin_can_clear(self, db_session):
        """Admin lock is not cleared by payment."""
        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        subscription = _make_subscription(db_session, subscriber, offer)

        suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.admin,
            source="admin:user-1",
            emit=False,
        )

        # Payment fails to restore
        restored = restore_subscription(
            db_session,
            str(subscription.id),
            trigger="payment",
            resolved_by="payment:pay-1",
            emit=False,
        )
        assert restored is False

        # Admin succeeds
        restored = restore_subscription(
            db_session,
            str(subscription.id),
            trigger="admin",
            resolved_by="admin:user-2",
            emit=False,
        )
        assert restored is True
        assert subscription.status == SubscriptionStatus.active

    def test_fraud_lock_only_admin_can_clear(self, db_session):
        """Fraud lock requires admin trigger."""
        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        subscription = _make_subscription(db_session, subscriber, offer)

        suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.fraud,
            source="fraud:investigation-1",
            emit=False,
        )

        restored = restore_subscription(
            db_session,
            str(subscription.id),
            trigger="admin",
            resolved_by="admin:user-1",
            emit=False,
        )
        assert restored is True


class TestTerminalStates:
    """Tests for expire and cancel operations."""

    def test_expire_resolves_locks(self, db_session):
        """Expiring a subscription with active locks resolves them all."""
        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        subscription = _make_subscription(db_session, subscriber, offer)

        suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.overdue,
            source="dunning_case:1",
            emit=False,
        )

        expire_subscription(db_session, str(subscription.id), emit=False)

        assert subscription.status == SubscriptionStatus.expired
        assert not has_active_lock(db_session, str(subscription.id))

    def test_cancel_resolves_locks(self, db_session):
        """Canceling a subscription resolves all locks."""
        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        subscription = _make_subscription(db_session, subscriber, offer)

        suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.admin,
            source="admin:user-1",
            emit=False,
        )

        cancel_subscription(
            db_session,
            str(subscription.id),
            cancel_reason="prepaid_deactivation",
            source="prepaid_enforcement",
            emit=False,
        )

        assert subscription.status == SubscriptionStatus.canceled
        assert subscription.cancel_reason == "prepaid_deactivation"
        assert not has_active_lock(db_session, str(subscription.id))


class TestActivateSubscription:
    """Tests for activate_subscription."""

    def test_activate_pending(self, db_session):
        """Pending subscription activates."""
        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        subscription = _make_subscription(
            db_session, subscriber, offer, status=SubscriptionStatus.pending
        )

        activate_subscription(db_session, str(subscription.id), emit=False)

        assert subscription.status == SubscriptionStatus.active
        assert subscription.start_at is not None
        assert subscriber.status == SubscriberStatus.active

    def test_cannot_activate_active(self, db_session):
        """Already active subscription raises ValueError."""
        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        subscription = _make_subscription(db_session, subscriber, offer)

        with pytest.raises(ValueError, match="Cannot activate"):
            activate_subscription(db_session, str(subscription.id), emit=False)


class TestComputeAccountStatus:
    """Tests for derived account status."""

    def test_mixed_active_and_suspended(self, db_session):
        """Sub A active, Sub B suspended → account active."""
        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        sub_a = _make_subscription(db_session, subscriber, offer)
        sub_b = _make_subscription(db_session, subscriber, offer)

        suspend_subscription(
            db_session,
            str(sub_b.id),
            reason=EnforcementReason.overdue,
            source="test",
            emit=False,
        )

        status = compute_account_status(db_session, str(subscriber.id))
        assert status == SubscriberStatus.active

    def test_all_suspended(self, db_session):
        """Both subs suspended → account suspended."""
        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        sub_a = _make_subscription(db_session, subscriber, offer)
        sub_b = _make_subscription(db_session, subscriber, offer)

        suspend_subscription(
            db_session,
            str(sub_a.id),
            reason=EnforcementReason.overdue,
            source="test",
            emit=False,
        )
        suspend_subscription(
            db_session,
            str(sub_b.id),
            reason=EnforcementReason.overdue,
            source="test",
            emit=False,
        )

        status = compute_account_status(db_session, str(subscriber.id))
        assert status == SubscriberStatus.suspended

    def test_restore_one_of_two_suspended(self, db_session):
        """Restore one of two suspended subs → account active."""
        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        sub_a = _make_subscription(db_session, subscriber, offer)
        sub_b = _make_subscription(db_session, subscriber, offer)

        suspend_subscription(
            db_session,
            str(sub_a.id),
            reason=EnforcementReason.overdue,
            source="test",
            emit=False,
        )
        suspend_subscription(
            db_session,
            str(sub_b.id),
            reason=EnforcementReason.overdue,
            source="test",
            emit=False,
        )

        restore_subscription(
            db_session,
            str(sub_a.id),
            trigger="payment",
            resolved_by="payment:1",
            emit=False,
        )

        status = compute_account_status(db_session, str(subscriber.id))
        assert status == SubscriberStatus.active

    def test_no_subscriptions(self, db_session):
        """Subscriber with no subscriptions → new."""
        subscriber = _make_subscriber(db_session)
        status = compute_account_status(db_session, str(subscriber.id))
        assert status == SubscriberStatus.new

    def test_all_expired(self, db_session):
        """All subscriptions expired → canceled."""
        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        sub = _make_subscription(db_session, subscriber, offer)

        expire_subscription(db_session, str(sub.id), emit=False)

        status = compute_account_status(db_session, str(subscriber.id))
        assert status == SubscriberStatus.canceled

    def test_pending_only(self, db_session):
        """Only pending subscriptions → new."""
        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        _make_subscription(
            db_session, subscriber, offer, status=SubscriptionStatus.pending
        )

        status = compute_account_status(db_session, str(subscriber.id))
        assert status == SubscriberStatus.new

    def test_pending_and_suspended_prefers_suspended(self, db_session):
        """A suspended service must not be masked by an unrelated pending one."""
        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        active_sub = _make_subscription(db_session, subscriber, offer)
        _make_subscription(
            db_session, subscriber, offer, status=SubscriptionStatus.pending
        )

        suspend_subscription(
            db_session,
            str(active_sub.id),
            reason=EnforcementReason.overdue,
            source="dunning_case:1",
            emit=False,
        )

        status = compute_account_status(db_session, str(subscriber.id))
        assert status == SubscriberStatus.suspended


class TestDuplicateLockPrevention:
    """Tests for idempotent lock creation."""

    def test_duplicate_reason_returns_existing(self, db_session):
        """Suspending twice with same reason returns existing lock."""
        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        subscription = _make_subscription(db_session, subscriber, offer)

        lock1 = suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.overdue,
            source="dunning_case:1",
            emit=False,
        )
        lock2 = suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.overdue,
            source="dunning_case:2",
            emit=False,
        )

        assert lock1.id == lock2.id  # Same lock returned


class TestRestoreWithReasonFilter:
    """Tests for restore_subscription with reason parameter."""

    def test_restore_specific_reason(self, db_session):
        """Only resolve locks matching the specified reason."""
        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        subscription = _make_subscription(db_session, subscriber, offer)

        suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.overdue,
            source="test",
            emit=False,
        )
        suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.fup,
            source="test",
            emit=False,
        )

        # Resolve only FUP lock via admin
        restore_subscription(
            db_session,
            str(subscription.id),
            trigger="admin",
            resolved_by="admin:1",
            reason=EnforcementReason.fup,
            emit=False,
        )

        # Overdue lock still active
        assert has_active_lock(
            db_session, str(subscription.id), EnforcementReason.overdue
        )
        assert not has_active_lock(
            db_session, str(subscription.id), EnforcementReason.fup
        )
        assert subscription.status == SubscriptionStatus.suspended

    def test_restore_non_suspended_returns_false(self, db_session):
        """Restoring an active subscription returns False."""
        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        subscription = _make_subscription(db_session, subscriber, offer)

        result = restore_subscription(
            db_session,
            str(subscription.id),
            trigger="payment",
            resolved_by="test",
            emit=False,
        )
        assert result is False


class TestComputeAccountStatusEdgeCases:
    """Edge cases for derived account status."""

    def test_missing_subscriber_raises(self, db_session):
        """Non-existent subscriber raises ValueError."""
        with pytest.raises(ValueError, match="not found"):
            compute_account_status(db_session, str(uuid.uuid4()))


class TestEventEmission:
    """Tests that verify event emission (using emit=True)."""

    def test_suspend_emits_events(self, db_session, monkeypatch):
        """Suspend emits subscription_suspended + enforcement_lock_created."""
        emitted: list[tuple] = []
        original_emit = emit_event

        def mock_emit(db, event_type, payload, **kwargs):
            emitted.append((event_type, payload))
            return original_emit(db, event_type, payload, **kwargs)

        monkeypatch.setattr("app.services.account_lifecycle.emit_event", mock_emit)

        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        subscription = _make_subscription(db_session, subscriber, offer)

        suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.overdue,
            source="test:1",
            emit=True,
        )

        event_types = [e[0] for e in emitted]
        assert EventType.subscription_suspended in event_types
        assert EventType.enforcement_lock_created in event_types

    def test_second_lock_emits_lock_created_only(self, db_session, monkeypatch):
        """Adding a second lock emits lock_created but NOT subscription_suspended."""
        emitted: list[tuple] = []
        original_emit = emit_event

        def mock_emit(db, event_type, payload, **kwargs):
            emitted.append((event_type, payload))
            return original_emit(db, event_type, payload, **kwargs)

        monkeypatch.setattr("app.services.account_lifecycle.emit_event", mock_emit)

        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        subscription = _make_subscription(db_session, subscriber, offer)

        suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.overdue,
            source="test:1",
            emit=True,
        )
        emitted.clear()

        suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.fup,
            source="test:2",
            emit=True,
        )

        event_types = [e[0] for e in emitted]
        assert EventType.enforcement_lock_created in event_types
        assert EventType.subscription_suspended not in event_types

    def test_restore_emits_resumed(self, db_session, monkeypatch):
        """Full restore emits subscription_resumed + enforcement_lock_resolved."""
        emitted: list[tuple] = []
        original_emit = emit_event

        def mock_emit(db, event_type, payload, **kwargs):
            emitted.append((event_type, payload))
            return original_emit(db, event_type, payload, **kwargs)

        monkeypatch.setattr("app.services.account_lifecycle.emit_event", mock_emit)

        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        subscription = _make_subscription(db_session, subscriber, offer)

        suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.overdue,
            source="test:1",
            emit=False,
        )
        restore_subscription(
            db_session,
            str(subscription.id),
            trigger="payment",
            resolved_by="payment:1",
            emit=True,
        )

        event_types = [e[0] for e in emitted]
        assert EventType.enforcement_lock_resolved in event_types
        assert EventType.subscription_resumed in event_types


class TestAllowedRestorers:
    """Verify the restorer map is complete and sensible."""

    def test_all_reasons_have_restorers(self):
        """Every enforcement reason has an entry in ALLOWED_RESTORERS."""
        for reason in EnforcementReason:
            assert reason in ALLOWED_RESTORERS, f"Missing restorer for {reason}"

    def test_admin_can_restore_all(self):
        """Admin trigger can resolve every enforcement reason."""
        for reason, triggers in ALLOWED_RESTORERS.items():
            assert "admin" in triggers, f"Admin cannot restore {reason}"
