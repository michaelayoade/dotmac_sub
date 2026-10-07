"""Owner tests for delayed outage evidence, coverage expiry, and reviewed recovery."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.models.billing import ServiceEntitlement, ServiceEntitlementStatus
from app.models.catalog import BillingMode, SubscriptionStatus
from app.models.network_monitoring import CustomerOutageInterval
from app.models.service_period_purchase import (
    OutageCompensationDecision,
    OutageCompensationDecisionStatus,
)
from app.services import outage_compensation as outages
from app.services.owner_commands import CommandContext
from app.services.service_period_policy import OutageCompensationPolicy


@pytest.fixture
def outage_setup(db_session, active_subscription, monkeypatch):
    subscription = active_subscription
    monkeypatch.setattr(
        outages, "_policy", lambda db: OutageCompensationPolicy(True, 6 * 3600)
    )
    now = datetime.now(UTC)
    subscription.billing_mode = BillingMode.prepaid
    subscription.next_billing_at = now + timedelta(days=10)
    funded = ServiceEntitlement(
        account_id=subscription.subscriber_id,
        subscription_id=subscription.id,
        starts_at=now - timedelta(days=20),
        ends_at=subscription.next_billing_at,
        amount_funded=Decimal("100.00"),
        currency="NGN",
        status=ServiceEntitlementStatus.active,
    )
    db_session.add(funded)
    db_session.commit()
    return subscription, now, funded


def _interval(db, subscription, start, end):
    interval = CustomerOutageInterval(
        incident_id=uuid4(),
        subscription_id=subscription.id,
        state="confirmed_unavailable",
        quality="exact",
        started_at=start,
        ended_at=end,
        finalized_at=end,
        idempotency_key=str(uuid4()),
    )
    db.add(interval)
    db.commit()
    return interval


def _apply(db, subscription, now):
    preview = outages.preview_outage_compensation(
        db, subscription_id=subscription.id, effective_at=now
    )
    command = outages.ApplyOutageCompensationCommand(
        subscription_id=subscription.id,
        expected_fingerprint=preview.fingerprint,
        idempotency_key=str(uuid4()),
        effective_at=now,
        context=CommandContext.system(
            actor="pytest:outage",
            scope="outage-compensation:apply",
            reason="Outage regression",
        ),
    )
    db.rollback()
    result = outages.apply_outage_compensation(db, command)
    if result.status is not OutageCompensationDecisionStatus.awaiting_approval:
        return result
    from tests.period_purchase_review_helpers import create_review_staff

    principal = create_review_staff(db)
    db.commit()
    approved = outages.preview_outage_compensation(
        db,
        subscription_id=subscription.id,
        effective_at=datetime.now(UTC),
        review_decision_id=result.decision_id,
    )
    approval = outages.ApproveOutageCompensationCommand(
        decision_id=result.decision_id,
        expected_fingerprint=approved.fingerprint,
        actor_system_user_id=principal.id,
        effective_at=datetime.now(UTC),
    )
    context = CommandContext.system(
        actor=f"user:{principal.id}",
        scope=outages.OUTAGE_APPROVAL_SCOPE,
        reason="Reviewed exact outage credit",
        idempotency_key=str(uuid4()),
    )
    db.rollback()
    return outages.approve_outage_compensation(db, approval, context=context)


def test_sequential_finalization_does_not_compensate_overlap_twice(
    db_session, outage_setup
):
    subscription, now, _ = outage_setup
    start = now - timedelta(days=2)
    _interval(db_session, subscription, start, start + timedelta(hours=8))
    first = _apply(db_session, subscription, now)
    _interval(
        db_session,
        subscription,
        start + timedelta(hours=4),
        start + timedelta(hours=12),
    )
    second = _apply(db_session, subscription, now)
    assert first.compensated_seconds == 8 * 3600
    assert second.compensated_seconds == 4 * 3600


def test_subthreshold_history_can_qualify_after_connected_finalization(
    db_session, outage_setup
):
    subscription, now, _ = outage_setup
    start = now - timedelta(days=2)
    _interval(db_session, subscription, start, start + timedelta(hours=4))
    first = _apply(db_session, subscription, now)
    assert first.status is OutageCompensationDecisionStatus.below_threshold
    assert first.compensated_seconds == 0
    _interval(
        db_session, subscription, start + timedelta(hours=3), start + timedelta(hours=7)
    )
    second = _apply(db_session, subscription, now)
    assert second.compensated_seconds == 7 * 3600


@pytest.mark.parametrize(
    "status,mode,expired",
    [
        (SubscriptionStatus.canceled, BillingMode.prepaid, False),
        (SubscriptionStatus.active, BillingMode.postpaid, False),
        (SubscriptionStatus.active, BillingMode.prepaid, True),
    ],
)
def test_ineligible_service_is_held_in_durable_review(
    db_session, outage_setup, status, mode, expired
):
    subscription, now, funded = outage_setup
    subscription.status, subscription.billing_mode = status, mode
    if expired:
        funded.ends_at = now - timedelta(hours=1)
        subscription.next_billing_at = funded.ends_at
    db_session.commit()
    _interval(
        db_session, subscription, now - timedelta(days=2), now - timedelta(days=1)
    )
    result = _apply(db_session, subscription, now)
    assert result.status is OutageCompensationDecisionStatus.review_required
    assert result.entitlement_id is None and result.compensated_seconds == 0
    assert db_session.scalar(select(func.count(OutageCompensationDecision.id))) == 1


def test_refunded_funding_retracts_dependent_outage_grant(db_session, outage_setup):
    subscription, now, funded = outage_setup
    _interval(
        db_session, subscription, now - timedelta(days=2), now - timedelta(days=1)
    )
    result = _apply(db_session, subscription, now)
    funded.status = ServiceEntitlementStatus.reversed
    db_session.flush()
    outages.stage_revoke_outage_compensation_funding(
        db_session,
        outages.RevokeOutageCompensationFundingCommand(
            source_entitlement_ids=(funded.id,),
            evidence_ref="pytest:confirmed-refund",
        ),
    )
    grant = db_session.get(ServiceEntitlement, result.entitlement_id)
    assert grant.status is ServiceEntitlementStatus.reversed
    assert (
        db_session.get(OutageCompensationDecision, result.decision_id).status
        is OutageCompensationDecisionStatus.compensated
    )
