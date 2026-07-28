"""Behavior coverage for lifecycle-owned account billing approval."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.subscribers import change_subscriber_billing_approval
from app.models.catalog import SubscriptionStatus
from app.models.enforcement_lock import EnforcementReason
from app.models.event_store import EventStore
from app.models.subscriber import SubscriberStatus
from app.schemas.subscriber import (
    SubscriberBillingApprovalUpdate,
    SubscriberUpdate,
)
from app.services import account_billing_approval as approval_service
from app.services import subscriber as subscriber_service
from app.services import subscription_billing_treatments as treatment_service
from app.services.account_lifecycle import (
    activate_subscription,
    get_active_locks,
    suspend_subscription,
)
from app.services.owner_commands import CommandContext


def _context(reason: str = "pytest billing approval") -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor="user:pytest",
        scope=approval_service.BILLING_APPROVAL_WRITE_SCOPE,
        reason=reason,
        idempotency_key=f"pytest:{command_id}",
    )


def _change(db, account_id: UUID, approved: bool):
    return approval_service.change_account_billing_approval(
        db,
        approval_service.ChangeAccountBillingApprovalCommand(
            context=_context(),
            account_id=account_id,
            approved=approved,
        ),
    )


def test_revocation_disables_billing_account_and_service_atomically(
    db_session, subscriber, subscription
):
    account_id = subscriber.id
    subscription_id = subscription.id
    subscriber.billing_enabled = True
    subscriber.status = SubscriberStatus.active
    subscription.status = SubscriptionStatus.active
    db_session.commit()

    outcome = _change(db_session, account_id, False)

    refreshed_account = db_session.get(type(subscriber), account_id)
    refreshed_subscription = db_session.get(type(subscription), subscription_id)
    assert outcome.action is approval_service.BillingApprovalAction.disabled
    assert outcome.affected_subscription_ids == (subscription_id,)
    assert refreshed_account.billing_enabled is False
    assert refreshed_account.status == SubscriberStatus.disabled
    assert refreshed_account.lifecycle_override_status == SubscriberStatus.disabled
    assert refreshed_account.lifecycle_override_source.startswith("billing_approval:")
    assert refreshed_subscription.status == SubscriptionStatus.disabled
    event = db_session.scalar(
        select(EventStore).where(
            EventStore.event_type == "subscriber.billing_approval_changed",
            EventStore.account_id == account_id,
        )
    )
    assert event is not None
    assert event.payload["approved"] is False
    assert event.payload["status"] == SubscriberStatus.disabled.value


def test_reapproval_restores_only_the_disable_owned_by_billing_approval(
    db_session, subscriber, subscription
):
    account_id = subscriber.id
    subscription_id = subscription.id
    subscriber.billing_enabled = True
    subscription.status = SubscriptionStatus.active
    db_session.commit()

    _change(db_session, account_id, False)
    restored = _change(db_session, account_id, True)

    refreshed_account = db_session.get(type(subscriber), account_id)
    refreshed_subscription = db_session.get(type(subscription), subscription_id)
    assert restored.action is approval_service.BillingApprovalAction.restored
    assert refreshed_account.billing_enabled is True
    assert refreshed_account.status == SubscriberStatus.active
    assert refreshed_account.lifecycle_override_status is None
    assert refreshed_subscription.status == SubscriptionStatus.active


def test_reapproval_does_not_lift_unrelated_administrative_disable(
    db_session, subscriber, subscription
):
    account_id = subscriber.id
    subscription_id = subscription.id
    subscriber.billing_enabled = False
    subscriber.status = SubscriberStatus.disabled
    subscriber.lifecycle_override_status = SubscriberStatus.disabled
    subscriber.lifecycle_override_source = "admin:security-review"
    subscriber.lifecycle_override_reason = "Security review"
    subscription.status = SubscriptionStatus.disabled
    db_session.commit()

    outcome = _change(db_session, account_id, True)

    refreshed_account = db_session.get(type(subscriber), account_id)
    refreshed_subscription = db_session.get(type(subscription), subscription_id)
    assert outcome.action is approval_service.BillingApprovalAction.approved
    assert refreshed_account.billing_enabled is True
    assert refreshed_account.status == SubscriberStatus.disabled
    assert refreshed_account.lifecycle_override_source == "admin:security-review"
    assert refreshed_subscription.status == SubscriptionStatus.disabled


def test_reapproval_preserves_an_unrelated_enforcement_lock(
    db_session, subscriber, subscription
):
    account_id = subscriber.id
    subscription_id = subscription.id
    subscriber.billing_enabled = True
    subscriber.status = SubscriberStatus.active
    subscription.status = SubscriptionStatus.active
    db_session.commit()

    suspend_subscription(
        db_session,
        str(subscription_id),
        reason=EnforcementReason.fraud,
        source="fraud_case:pytest",
        emit=False,
    )
    db_session.commit()
    _change(db_session, account_id, False)
    restored = _change(db_session, account_id, True)

    refreshed_account = db_session.get(type(subscriber), account_id)
    refreshed_subscription = db_session.get(type(subscription), subscription_id)
    active_locks = get_active_locks(
        db_session,
        subscription_id=str(subscription_id),
    )
    assert restored.action is approval_service.BillingApprovalAction.restored
    assert refreshed_account.billing_enabled is True
    assert refreshed_account.status == SubscriberStatus.suspended
    assert refreshed_account.lifecycle_override_status is None
    assert refreshed_subscription.status == SubscriptionStatus.suspended
    assert [lock.reason for lock in active_locks] == [EnforcementReason.fraud]


def test_unapproved_account_cannot_activate_pending_service(
    db_session, subscriber, subscription
):
    subscriber.billing_enabled = False
    subscriber.status = SubscriberStatus.new
    subscription.status = SubscriptionStatus.pending
    subscription_id = subscription.id
    db_session.commit()

    with pytest.raises(
        ValueError,
        match="billing approval is required",
    ):
        activate_subscription(db_session, str(subscription_id), emit=False)

    db_session.refresh(subscription)
    assert subscription.status == SubscriptionStatus.pending


def test_generic_subscriber_update_rejects_billing_approval_change(
    db_session, subscriber
):
    account_id = subscriber.id
    subscriber.billing_enabled = True
    db_session.commit()

    with pytest.raises(HTTPException) as exc_info:
        subscriber_service.subscribers.update(
            db_session,
            str(account_id),
            SubscriberUpdate(billing_enabled=False),
        )

    assert exc_info.value.status_code == 409
    assert subscriber.billing_enabled is True


def test_api_adapter_submits_the_explicit_owner_command(
    db_session, subscriber, subscription
):
    account_id = subscriber.id
    subscriber.billing_enabled = True
    subscriber.status = SubscriberStatus.active
    subscription.status = SubscriptionStatus.active
    db_session.commit()
    request = SimpleNamespace(
        state=SimpleNamespace(
            auth={"principal_type": "user", "principal_id": "pytest-operator"}
        )
    )

    outcome = change_subscriber_billing_approval(
        str(account_id),
        SubscriberBillingApprovalUpdate(
            approved=False,
            reason="API adapter behavior test",
            idempotency_key=f"pytest-api:{uuid4()}",
        ),
        request,
        db_session,
    )

    assert outcome.action is approval_service.BillingApprovalAction.disabled
    assert db_session.get(type(subscriber), account_id).billing_enabled is False


def test_reconciler_disables_active_service_without_treatment(
    db_session, subscriber, subscription
):
    account_id = subscriber.id
    subscription_id = subscription.id
    subscriber.billing_enabled = False
    subscriber.status = SubscriberStatus.active
    subscription.status = SubscriptionStatus.active
    db_session.commit()

    outcome = approval_service.reconcile_account_billing_approval(
        db_session,
        approval_service.ReconcileAccountBillingApprovalCommand(
            context=_context("Repair active unapproved service"),
            account_id=account_id,
        ),
    )

    assert outcome.action is approval_service.BillingApprovalAction.disabled
    assert db_session.get(type(subscriber), account_id).billing_enabled is False
    assert (
        db_session.get(type(subscription), subscription_id).status
        == SubscriptionStatus.disabled
    )


def test_reconciler_repairs_redundant_false_when_all_active_service_is_treated(
    db_session, subscriber, subscription, monkeypatch
):
    account_id = subscriber.id
    subscription_id = subscription.id
    subscriber.billing_enabled = False
    subscriber.status = SubscriberStatus.active
    subscription.status = SubscriptionStatus.active
    db_session.commit()

    monkeypatch.setattr(
        treatment_service,
        "resolve_subscription_billing_treatments",
        lambda _db, subscriptions: {
            item.id: SimpleNamespace(
                status=treatment_service.BillingTreatmentDecisionStatus.effective
            )
            for item in subscriptions
        },
    )
    outcome = approval_service.reconcile_account_billing_approval(
        db_session,
        approval_service.ReconcileAccountBillingApprovalCommand(
            context=_context("Repair treatment-aligned approval"),
            account_id=account_id,
        ),
    )

    assert outcome.action is approval_service.BillingApprovalAction.treatment_aligned
    assert db_session.get(type(subscriber), account_id).billing_enabled is True
    assert (
        db_session.get(type(subscription), subscription_id).status
        == SubscriptionStatus.active
    )


def test_drift_query_returns_only_active_unapproved_accounts(
    db_session, subscriber, subscription
):
    account_id = subscriber.id
    subscriber.billing_enabled = False
    subscription.status = SubscriptionStatus.active
    db_session.commit()

    assert account_id in approval_service.find_billing_approval_drift_account_ids(
        db_session
    )
