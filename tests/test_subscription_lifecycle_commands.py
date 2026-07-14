"""Execution tests for canonical single-subscription lifecycle commands."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.models.audit import AuditEvent
from app.models.catalog import (
    AccessType,
    BillingMode,
    CatalogOffer,
    OfferPrice,
    PriceBasis,
    PriceType,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.enforcement_lock import EnforcementLock
from app.models.idempotency import IdempotencyKey
from app.models.subscription_change import (
    SubscriptionChangeRequest,
    SubscriptionChangeStatus,
)
from app.services.subscription_lifecycle import (
    SubscriptionCommandKind,
    SubscriptionCommandOutcomeStatus,
    SubscriptionEffectiveTiming,
    SubscriptionLifecycleCommand,
    SubscriptionLifecycleError,
    resolve_subscription_lifecycle,
)
from app.services.subscription_lifecycle_commands import (
    execute_subscription_command,
)
from app.services.web_catalog_subscription_workflows import (
    execute_lifecycle_command_response,
)


def _subscription(
    db_session,
    subscriber,
    offer,
    *,
    status: SubscriptionStatus = SubscriptionStatus.active,
) -> Subscription:
    item = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=status,
        billing_mode=BillingMode.postpaid,
        start_at=datetime(2026, 7, 1, tzinfo=UTC),
        next_billing_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    db_session.add(item)
    db_session.flush()
    return item


def _target_offer(db_session, current: CatalogOffer) -> CatalogOffer:
    current.plan_family = "unlimited"
    current.billing_mode = BillingMode.postpaid
    target = CatalogOffer(
        name="Faster Internet",
        code="FAST-EXEC",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_mode=BillingMode.postpaid,
        plan_family="unlimited",
    )
    db_session.add(target)
    db_session.flush()
    db_session.add_all(
        [
            OfferPrice(
                offer_id=current.id,
                price_type=PriceType.recurring,
                amount=Decimal("10000.00"),
                currency="NGN",
            ),
            OfferPrice(
                offer_id=target.id,
                price_type=PriceType.recurring,
                amount=Decimal("15000.00"),
                currency="NGN",
            ),
        ]
    )
    db_session.flush()
    return target


def test_execute_activation_applies_and_records_outcome(
    db_session, subscriber, catalog_offer
):
    subscription = _subscription(
        db_session,
        subscriber,
        catalog_offer,
        status=SubscriptionStatus.pending,
    )
    reviewed = resolve_subscription_lifecycle(db_session, str(subscription.id))
    command = SubscriptionLifecycleCommand(
        subscription_id=str(subscription.id),
        kind=SubscriptionCommandKind.activate,
        source="admin:test",
        expected_head=reviewed.head,
        idempotency_key="activate-1",
    )

    outcome = execute_subscription_command(
        db_session,
        command,
        actor_id="operator-1",
        now=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
    )

    db_session.refresh(subscription)
    assert outcome.status == SubscriptionCommandOutcomeStatus.applied
    assert outcome.previous_head == reviewed.head
    assert outcome.current_head != reviewed.head
    assert subscription.status == SubscriptionStatus.active
    assert db_session.query(IdempotencyKey).count() == 1
    audit = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.entity_type == "subscription_lifecycle_command")
        .one()
    )
    assert audit.actor_id == "operator-1"
    assert audit.metadata_["status"] == "applied"


def test_admin_workflow_serializes_canonical_command_outcome(
    db_session, subscriber, catalog_offer
):
    subscription = _subscription(
        db_session,
        subscriber,
        catalog_offer,
        status=SubscriptionStatus.pending,
    )
    reviewed = resolve_subscription_lifecycle(db_session, str(subscription.id))

    payload, status_code = execute_lifecycle_command_response(
        db_session,
        subscription_id=str(subscription.id),
        kind=SubscriptionCommandKind.activate,
        actor_id="operator-2",
        expected_head=reviewed.head,
        idempotency_key="admin-workflow-activate",
        reason="installation accepted",
    )

    assert status_code == 200
    assert payload["status"] == "applied"
    assert payload["previous_head"] == reviewed.head
    assert payload["current_head"] != reviewed.head
    assert payload["replayed"] is False


def test_admin_workflow_returns_structured_validation_error(
    db_session, subscriber, catalog_offer
):
    subscription = _subscription(db_session, subscriber, catalog_offer)

    payload, status_code = execute_lifecycle_command_response(
        db_session,
        subscription_id=str(subscription.id),
        kind=SubscriptionCommandKind.change_plan,
        actor_id="operator-2",
    )

    assert status_code == 422
    assert payload["status"] == "rejected"
    assert payload["error_code"] == "invalid_lifecycle_command"
    assert "target_offer_id" in str(payload["message"])


def test_idempotent_replay_precedes_stale_head_validation(
    db_session, subscriber, catalog_offer
):
    subscription = _subscription(
        db_session,
        subscriber,
        catalog_offer,
        status=SubscriptionStatus.pending,
    )
    reviewed = resolve_subscription_lifecycle(db_session, str(subscription.id))
    command = SubscriptionLifecycleCommand(
        subscription_id=str(subscription.id),
        kind=SubscriptionCommandKind.activate,
        source="admin:test",
        expected_head=reviewed.head,
        idempotency_key="activate-retry",
    )

    first = execute_subscription_command(db_session, command)
    replay = execute_subscription_command(db_session, command)

    assert first.status == SubscriptionCommandOutcomeStatus.applied
    assert replay.status == SubscriptionCommandOutcomeStatus.skipped
    assert replay.replayed is True
    assert replay.error_code == "idempotent_replay"
    assert db_session.query(IdempotencyKey).count() == 1


def test_reused_idempotency_key_with_changed_payload_is_rejected(
    db_session, subscriber, catalog_offer
):
    subscription = _subscription(
        db_session,
        subscriber,
        catalog_offer,
        status=SubscriptionStatus.pending,
    )
    reviewed = resolve_subscription_lifecycle(db_session, str(subscription.id))
    original = SubscriptionLifecycleCommand(
        subscription_id=str(subscription.id),
        kind=SubscriptionCommandKind.activate,
        source="admin:test",
        expected_head=reviewed.head,
        idempotency_key="shared-key",
    )
    execute_subscription_command(db_session, original)
    changed = SubscriptionLifecycleCommand(
        subscription_id=str(subscription.id),
        kind=SubscriptionCommandKind.suspend,
        source="admin:test",
        reason="different command",
        idempotency_key="shared-key",
    )

    outcome = execute_subscription_command(db_session, changed)

    assert outcome.status == SubscriptionCommandOutcomeStatus.rejected
    assert outcome.error_code == "idempotency_key_conflict"
    db_session.refresh(subscription)
    assert subscription.status == SubscriptionStatus.active


def test_stale_review_is_superseded_without_mutation(
    db_session, subscriber, catalog_offer
):
    subscription = _subscription(db_session, subscriber, catalog_offer)
    command = SubscriptionLifecycleCommand(
        subscription_id=str(subscription.id),
        kind=SubscriptionCommandKind.cancel,
        source="admin:test",
        expected_head="stale-head",
        idempotency_key="cancel-stale",
    )

    outcome = execute_subscription_command(db_session, command)

    assert outcome.status == SubscriptionCommandOutcomeStatus.superseded
    assert outcome.error_code == "subscription_head_changed"
    db_session.refresh(subscription)
    assert subscription.status == SubscriptionStatus.active
    assert db_session.query(IdempotencyKey).count() == 0


def test_suspend_and_restore_delegate_to_account_lifecycle(
    db_session, subscriber, catalog_offer
):
    subscription = _subscription(db_session, subscriber, catalog_offer)
    suspend = SubscriptionLifecycleCommand(
        subscription_id=str(subscription.id),
        kind=SubscriptionCommandKind.suspend,
        source="admin:test",
        reason="operator hold",
    )

    suspended = execute_subscription_command(db_session, suspend)
    restored = execute_subscription_command(
        db_session,
        SubscriptionLifecycleCommand(
            subscription_id=str(subscription.id),
            kind=SubscriptionCommandKind.restore,
            source="admin:test",
            reason="operator cleared hold",
        ),
    )

    db_session.refresh(subscription)
    assert suspended.status == SubscriptionCommandOutcomeStatus.applied
    assert len(suspended.artifact_ids) == 1
    lock = db_session.get(EnforcementLock, suspended.artifact_ids[0])
    assert lock is not None
    assert lock.is_active is False
    assert restored.status == SubscriptionCommandOutcomeStatus.applied
    assert subscription.status == SubscriptionStatus.active


def test_immediate_plan_change_delegates_to_catalog_owner(
    db_session, subscriber, catalog_offer
):
    target = _target_offer(db_session, catalog_offer)
    subscription = _subscription(db_session, subscriber, catalog_offer)
    command = SubscriptionLifecycleCommand(
        subscription_id=str(subscription.id),
        kind=SubscriptionCommandKind.change_plan,
        source="admin:test",
        target_offer_id=str(target.id),
        idempotency_key="plan-now-1",
    )

    outcome = execute_subscription_command(db_session, command)

    db_session.refresh(subscription)
    assert outcome.status == SubscriptionCommandOutcomeStatus.applied
    assert subscription.offer_id == target.id
    assert db_session.query(IdempotencyKey).count() == 1


def test_deferred_plan_change_returns_scheduled_artifact_and_replays_it(
    db_session, subscriber, catalog_offer
):
    target = _target_offer(db_session, catalog_offer)
    subscription = _subscription(db_session, subscriber, catalog_offer)
    command = SubscriptionLifecycleCommand(
        subscription_id=str(subscription.id),
        kind=SubscriptionCommandKind.change_plan,
        source="admin:test",
        target_offer_id=str(target.id),
        effective_timing=SubscriptionEffectiveTiming.next_cycle,
        idempotency_key="plan-next-1",
    )

    outcome = execute_subscription_command(db_session, command)
    replay = execute_subscription_command(db_session, command)

    assert outcome.status == SubscriptionCommandOutcomeStatus.scheduled
    assert len(outcome.artifact_ids) == 1
    request = db_session.get(SubscriptionChangeRequest, outcome.artifact_ids[0])
    assert request is not None
    assert request.status == SubscriptionChangeStatus.approved
    assert request.effective_date.isoformat() == "2026-08-01"
    assert replay.replayed is True
    assert replay.artifact_ids == outcome.artifact_ids
    db_session.refresh(subscription)
    assert subscription.offer_id == catalog_offer.id


def test_renewal_and_deferred_status_commands_fail_closed(
    db_session, subscriber, catalog_offer
):
    subscription = _subscription(db_session, subscriber, catalog_offer)

    renewal = execute_subscription_command(
        db_session,
        SubscriptionLifecycleCommand(
            subscription_id=str(subscription.id),
            kind=SubscriptionCommandKind.renew,
            source="admin:test",
        ),
    )
    deferred_cancel = execute_subscription_command(
        db_session,
        SubscriptionLifecycleCommand(
            subscription_id=str(subscription.id),
            kind=SubscriptionCommandKind.cancel,
            source="admin:test",
            effective_timing=SubscriptionEffectiveTiming.scheduled,
            effective_at=datetime(2026, 8, 15, tzinfo=UTC),
        ),
        now=datetime(2026, 7, 14, tzinfo=UTC),
    )

    assert renewal.status == SubscriptionCommandOutcomeStatus.rejected
    assert renewal.error_code == "renewal_execution_is_billing_owned"
    assert deferred_cancel.status == SubscriptionCommandOutcomeStatus.rejected
    assert deferred_cancel.error_code == "deferred_status_execution_not_supported"


def test_future_time_is_not_executed_under_immediate_timing(
    db_session, subscriber, catalog_offer
):
    subscription = _subscription(db_session, subscriber, catalog_offer)

    outcome = execute_subscription_command(
        db_session,
        SubscriptionLifecycleCommand(
            subscription_id=str(subscription.id),
            kind=SubscriptionCommandKind.cancel,
            source="admin:test",
            effective_at=datetime(2026, 7, 15, tzinfo=UTC),
        ),
        now=datetime(2026, 7, 14, tzinfo=UTC),
    )

    assert outcome.status == SubscriptionCommandOutcomeStatus.rejected
    assert outcome.error_code == "future_effective_at_requires_scheduling"
    db_session.refresh(subscription)
    assert subscription.status == SubscriptionStatus.active


def test_next_cycle_rejects_an_explicit_custom_date() -> None:
    with pytest.raises(SubscriptionLifecycleError, match="use scheduled"):
        SubscriptionLifecycleCommand(
            subscription_id="subscription-1",
            kind=SubscriptionCommandKind.change_plan,
            source="admin:test",
            target_offer_id="offer-1",
            effective_timing=SubscriptionEffectiveTiming.next_cycle,
            effective_at=datetime(2026, 8, 1, tzinfo=UTC),
        )


def test_owner_failure_rolls_back_idempotency_reservation(
    db_session, subscriber, catalog_offer, monkeypatch
):
    subscription = _subscription(
        db_session,
        subscriber,
        catalog_offer,
        status=SubscriptionStatus.pending,
    )
    db_session.commit()

    def _fail(*args, **kwargs):
        raise RuntimeError("owner unavailable")

    monkeypatch.setattr(
        "app.services.account_lifecycle.transition_subscription_status",
        _fail,
    )
    command = SubscriptionLifecycleCommand(
        subscription_id=str(subscription.id),
        kind=SubscriptionCommandKind.activate,
        source="admin:test",
        idempotency_key="activation-owner-failure",
    )

    outcome = execute_subscription_command(db_session, command)

    assert outcome.status == SubscriptionCommandOutcomeStatus.failed
    assert outcome.error_code == "command_execution_failed"
    assert "owner unavailable" in outcome.message
    assert db_session.query(IdempotencyKey).count() == 0


def test_expired_subscription_is_rejected_before_reservation(
    db_session, subscriber, catalog_offer
):
    subscription = _subscription(
        db_session,
        subscriber,
        catalog_offer,
        status=SubscriptionStatus.expired,
    )
    command = SubscriptionLifecycleCommand(
        subscription_id=str(subscription.id),
        kind=SubscriptionCommandKind.cancel,
        source="admin:test",
        idempotency_key="terminal-cancel",
    )

    outcome = execute_subscription_command(
        db_session,
        command,
        now=datetime.now(UTC) + timedelta(seconds=1),
    )

    assert outcome.status == SubscriptionCommandOutcomeStatus.rejected
    assert outcome.error_code == "status_expired_not_eligible_for_cancel"
    assert db_session.query(IdempotencyKey).count() == 0
