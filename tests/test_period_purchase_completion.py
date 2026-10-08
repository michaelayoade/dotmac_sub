"""Acceptance regressions for manual approval, recovery and original clocks."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.models.billing import ServiceEntitlement, TopupIntent
from app.models.network_monitoring import CustomerOutageInterval
from app.models.service_period_purchase import (
    OutageCompensationDecision,
    OutageCompensationDecisionStatus,
    PrepaidPeriodPurchase,
    PrepaidPeriodPurchaseStatus,
)
from app.services import outage_compensation as outages
from app.services import prepaid_period_purchases as purchases
from app.services.compensated_service_time import (
    StageTimeCreditCommand,
    TimeCreditQuery,
    TimeCreditSource,
    resolve_compensated_service_time,
    stage_compensated_service_time,
)
from app.services.domain_errors import DomainError
from app.services.outage_interval_algebra import TimeInterval
from app.services.owner_commands import CommandContext
from app.services.payment_gateway_adapter import (
    PaymentGatewayVerificationObservation,
    PaymentGatewayVerificationOutcome,
    PaymentGatewayVerificationReason,
)
from app.services.purchased_service_coverage import (
    PurchasedCoverageQuery,
    resolve_purchased_coverage,
)
from app.services.topup_intents import (
    GatewayTopupObservationSource,
    RecordGatewayTopupObservationCommand,
    stage_gateway_topup_observation,
)
from tests.period_purchase_review_helpers import create_review_staff
from tests.test_outage_compensation_safety import _interval
from tests.test_outage_compensation_safety import outage_setup as _outage_fixture
from tests.test_prepaid_period_purchase_safety import _context
from tests.test_prepaid_period_purchase_safety import (
    purchase_setup as _purchase_fixture,
)


@pytest.fixture(name="outage_setup")
def _completion_outage_setup(db_session, active_subscription, monkeypatch):
    return _outage_fixture.__wrapped__(db_session, active_subscription, monkeypatch)


@pytest.fixture(name="purchase_setup")
def _completion_purchase_setup(db_session, active_subscription, monkeypatch, request):
    return _purchase_fixture.__wrapped__(
        db_session, active_subscription, monkeypatch, request
    )


def _propose(db, subscription):
    now = datetime.now(UTC)
    preview = outages.preview_outage_compensation(
        db, subscription_id=subscription.id, effective_at=now
    )
    command = outages.ApplyOutageCompensationCommand(
        subscription_id=subscription.id,
        expected_fingerprint=preview.fingerprint,
        idempotency_key=str(uuid4()),
        effective_at=now,
        context=CommandContext.system(
            actor="system:outage",
            scope="outage-compensation:apply",
            reason="Finalized outage",
        ),
    )
    db.rollback()
    return outages.apply_outage_compensation(db, command)


def _approval(db, proposal, principal):
    preview = outages.preview_outage_compensation(
        db,
        subscription_id=proposal.subscription_id,
        effective_at=datetime.now(UTC),
        review_decision_id=proposal.id,
    )
    command = outages.ApproveOutageCompensationCommand(
        decision_id=proposal.id,
        expected_fingerprint=preview.fingerprint,
        actor_system_user_id=principal.id,
        effective_at=datetime.now(UTC),
    )
    context = CommandContext.system(
        actor=f"user:{principal.id}",
        scope=outages.OUTAGE_APPROVAL_SCOPE,
        reason="Reviewed exact funded downtime",
        idempotency_key=str(uuid4()),
    )
    db.rollback()
    return command, context


def test_outage_event_records_proposal_without_grant(db_session, outage_setup):
    subscription, now, _ = outage_setup
    previous = subscription.next_billing_at
    _interval(
        db_session, subscription, now - timedelta(hours=9), now - timedelta(hours=1)
    )
    result = _propose(db_session, subscription)
    assert result.status is OutageCompensationDecisionStatus.awaiting_approval
    assert result.entitlement_id is None and result.compensated_seconds == 0
    db_session.refresh(subscription)
    assert outages._utc(subscription.next_billing_at) == outages._utc(previous)
    assert db_session.scalar(select(func.count(ServiceEntitlement.id))) == 1


def test_approval_is_permission_bound_and_exactly_once(db_session, outage_setup):
    subscription, now, _ = outage_setup
    _interval(
        db_session, subscription, now - timedelta(hours=9), now - timedelta(hours=1)
    )
    proposed = _propose(db_session, subscription)
    principal = create_review_staff(db_session)
    db_session.commit()
    proposal = db_session.get(OutageCompensationDecision, proposed.decision_id)
    command, context = _approval(db_session, proposal, principal)
    result = outages.approve_outage_compensation(db_session, command, context=context)
    assert result.compensated_seconds == 8 * 3600
    db_session.rollback()
    replay = outages.approve_outage_compensation(db_session, command, context=context)
    assert replay.replayed and replay.entitlement_id == result.entitlement_id
    decision = db_session.get(OutageCompensationDecision, result.decision_id)
    assert (
        decision.approved_by == principal.id
        and decision.approval_reason == context.reason
    )
    assert db_session.scalar(select(func.count(ServiceEntitlement.id))) == 2


def test_inactive_approver_and_self_approval_are_rejected(db_session, outage_setup):
    subscription, now, _ = outage_setup
    _interval(
        db_session, subscription, now - timedelta(hours=9), now - timedelta(hours=1)
    )
    result = _propose(db_session, subscription)
    principal = create_review_staff(db_session)
    proposal = db_session.get(OutageCompensationDecision, result.decision_id)
    proposal.created_by = f"user:{principal.id}"
    db_session.commit()
    command, context = _approval(db_session, proposal, principal)
    with pytest.raises(outages.OutageCompensationError, match="different staff"):
        outages.approve_outage_compensation(db_session, command, context=context)
    db_session.rollback()
    principal.is_active = False
    db_session.commit()
    with pytest.raises(outages.OutageCompensationError, match="active staff"):
        outages.approve_outage_compensation(db_session, command, context=context)


def test_changed_credit_history_invalidates_approval(db_session, outage_setup):
    subscription, now, _ = outage_setup
    start = now - timedelta(hours=9)
    _interval(db_session, subscription, start, now - timedelta(hours=1))
    result = _propose(db_session, subscription)
    principal = create_review_staff(db_session)
    db_session.commit()
    proposal = db_session.get(OutageCompensationDecision, result.decision_id)
    command, context = _approval(db_session, proposal, principal)
    stage_compensated_service_time(
        db_session,
        StageTimeCreditCommand(
            subscription.id,
            TimeCreditSource.pause,
            uuid4(),
            (TimeInterval(start, start + timedelta(hours=4)),),
            "reviewed-pause",
        ),
    )
    db_session.commit()
    with pytest.raises(outages.OutageCompensationError, match="fresh proposal"):
        outages.approve_outage_compensation(db_session, command, context=context)


def test_existing_clock_credit_is_subtracted(db_session, outage_setup):
    subscription, now, _ = outage_setup
    start = now - timedelta(hours=9)
    stage_compensated_service_time(
        db_session,
        StageTimeCreditCommand(
            subscription.id,
            TimeCreditSource.pause,
            uuid4(),
            (TimeInterval(start, start + timedelta(hours=4)),),
            "reviewed-pause",
        ),
    )
    db_session.commit()
    _interval(db_session, subscription, start, now - timedelta(hours=1))
    preview = outages.preview_outage_compensation(
        db_session, subscription_id=subscription.id, effective_at=now
    )
    assert preview.funded_overlap_seconds == 4 * 3600
    assert resolve_compensated_service_time(
        db_session, TimeCreditQuery(subscription.id)
    ).credited


@pytest.mark.parametrize(
    "outcome,reason",
    [
        ("failed", "provider_reported_failed"),
        ("abandoned", "provider_reported_abandoned"),
    ],
)
def test_confirmed_unpaid_checkout_releases_hold(
    db_session, purchase_setup, outcome, reason
):
    purchase_id, create, _ = purchase_setup
    purchase = db_session.get(PrepaidPeriodPurchase, purchase_id)
    stage_gateway_topup_observation(
        db_session,
        RecordGatewayTopupObservationCommand(
            intent_id=purchase.topup_intent_id,
            observation=PaymentGatewayVerificationObservation(
                outcome=PaymentGatewayVerificationOutcome(outcome),
                reason_code=PaymentGatewayVerificationReason(reason),
            ),
            observed_at=datetime.now(UTC),
            source=GatewayTopupObservationSource.customer_gateway_verify,
        ),
        context=_context(),
    )
    db_session.commit()
    assert purchase.status is PrepaidPeriodPurchaseStatus.failed
    assert not resolve_purchased_coverage(
        db_session, PurchasedCoverageQuery(purchase.subscription_id)
    ).has_unsettled_purchase
    db_session.rollback()
    next_purchase = purchases.create_prepaid_period_purchase(
        db_session,
        replace(create, idempotency_key=str(uuid4()), effective_at=datetime.now(UTC)),
        context=_context(),
    )
    assert next_purchase.id != purchase_id


def test_unknown_provider_outcome_does_not_release_hold(db_session, purchase_setup):
    purchase_id, _, _ = purchase_setup
    purchase = db_session.get(PrepaidPeriodPurchase, purchase_id)
    intent = db_session.get(TopupIntent, purchase.topup_intent_id)
    intent.status = "expired"
    db_session.commit()
    assert resolve_purchased_coverage(
        db_session, PurchasedCoverageQuery(purchase.subscription_id)
    ).has_unsettled_purchase


def test_provisional_recovery_blocks_quote(db_session, purchase_setup):
    purchase_id, _, _ = purchase_setup
    purchase = db_session.get(PrepaidPeriodPurchase, purchase_id)
    now = datetime.now(UTC)
    interval = CustomerOutageInterval(
        incident_id=uuid4(),
        subscription_id=purchase.subscription_id,
        state="confirmed_unavailable",
        quality="exact",
        started_at=now - timedelta(hours=8),
        ended_at=now - timedelta(minutes=1),
        finalized_at=None,
        idempotency_key=str(uuid4()),
    )
    db_session.add(interval)
    db_session.commit()
    with pytest.raises(purchases.PrepaidPeriodPurchaseError, match="finalized"):
        purchases.preview_prepaid_period_purchase(
            db_session,
            account_id=purchase.account_id,
            subscription_id=purchase.subscription_id,
            period_count=2,
            effective_at=now,
        )
    interval.finalized_at = now
    db_session.commit()
    assert purchases.preview_prepaid_period_purchase(
        db_session,
        account_id=purchase.account_id,
        subscription_id=purchase.subscription_id,
        period_count=2,
        effective_at=now,
    )


def test_clock_claim_replay_conflicts_on_changed_evidence(db_session, outage_setup):
    subscription, now, _ = outage_setup
    command = StageTimeCreditCommand(
        subscription.id,
        TimeCreditSource.pause,
        uuid4(),
        (TimeInterval(now - timedelta(hours=8), now),),
        "pause-proof",
    )
    stage_compensated_service_time(db_session, command)
    stage_compensated_service_time(db_session, command)
    with pytest.raises(DomainError, match="different"):
        stage_compensated_service_time(
            db_session,
            replace(command, ranges=(TimeInterval(now - timedelta(hours=7), now),)),
        )


def test_customer_script_sends_csrf_and_displays_dates():
    script = Path("templates/customer/billing/service_periods.html").read_text(
        encoding="utf-8"
    )
    assert "'X-CSRF-Token': getCsrfToken()" in script
    assert "period.starts_at" in script and "period.ends_at" in script
    assert "data.coverage_starts_at" in script and "data.coverage_ends_at" in script
    assert "sequence !== previewSequence" in script


def test_late_capture_on_closed_checkout_is_held(db_session, purchase_setup):
    purchase_id, create, settlement = purchase_setup
    purchase = db_session.get(PrepaidPeriodPurchase, purchase_id)
    stage_gateway_topup_observation(
        db_session,
        RecordGatewayTopupObservationCommand(
            intent_id=purchase.topup_intent_id,
            observation=PaymentGatewayVerificationObservation(
                outcome=PaymentGatewayVerificationOutcome.failed,
                reason_code=PaymentGatewayVerificationReason.provider_reported_failed,
            ),
            observed_at=datetime.now(UTC),
            source=GatewayTopupObservationSource.customer_gateway_verify,
        ),
        context=_context(),
    )
    db_session.commit()
    replacement = purchases.create_prepaid_period_purchase(
        db_session,
        replace(create, idempotency_key=str(uuid4()), effective_at=datetime.now(UTC)),
        context=_context(),
    )
    replacement_id = replacement.id
    db_session.rollback()
    held = purchases.settle_verified_prepaid_period_purchase(
        db_session, settlement, context=_context()
    )
    assert held.status is PrepaidPeriodPurchaseStatus.review_required
    assert held.payment_id is not None and not held.invoice_ids
    assert (
        db_session.get(PrepaidPeriodPurchase, replacement_id).status
        is PrepaidPeriodPurchaseStatus.quoted
    )
    from app.services.billing._common import get_spendable_account_credit_balance

    assert (
        get_spendable_account_credit_balance(db_session, str(purchase.account_id)) == 0
    )


def test_approval_failure_rolls_back_grant_claim_and_tail(
    db_session, outage_setup, monkeypatch
):
    subscription, now, _ = outage_setup
    tail = outages._utc(subscription.next_billing_at)
    _interval(
        db_session, subscription, now - timedelta(hours=9), now - timedelta(hours=1)
    )
    result = _propose(db_session, subscription)
    principal = create_review_staff(db_session)
    db_session.commit()
    proposal = db_session.get(OutageCompensationDecision, result.decision_id)
    command, context = _approval(db_session, proposal, principal)

    def reject(*args, **kwargs):
        raise DomainError(code="test.anchor_failure", message="Forced anchor rejection")

    monkeypatch.setattr(outages, "stage_subscription_billing_anchor", reject)
    with pytest.raises(DomainError, match="Forced anchor"):
        outages.approve_outage_compensation(db_session, command, context=context)
    db_session.refresh(subscription)
    assert outages._utc(subscription.next_billing_at) == tail
    assert db_session.scalar(select(func.count(ServiceEntitlement.id))) == 1
    from app.models.service_period_purchase import CompensatedServiceTime

    assert db_session.scalar(select(func.count(CompensatedServiceTime.id))) == 0
    assert (
        db_session.get(
            OutageCompensationDecision, result.decision_id
        ).resolved_by_decision_id
        is None
    )


def test_legacy_extension_is_reviewed_before_another_award(db_session, outage_setup):
    from app.models.service_extension import (
        ServiceExtension,
        ServiceExtensionEntry,
        ServiceExtensionScope,
        ServiceExtensionStatus,
    )

    subscription, now, funded = outage_setup
    start, end = now - timedelta(hours=9), now - timedelta(hours=1)
    tail = outages._utc(funded.ends_at)
    extension = ServiceExtension(
        reason="Previously compensated outage",
        window_start=start,
        window_end=end,
        days=1,
        scope_type=ServiceExtensionScope.subscribers,
        scope_subscriber_ids=[str(subscription.subscriber_id)],
        status=ServiceExtensionStatus.applied,
    )
    db_session.add(extension)
    db_session.flush()
    entry = ServiceExtensionEntry(
        extension_id=extension.id,
        subscription_id=subscription.id,
        subscriber_id=subscription.subscriber_id,
        previous_next_billing_at=tail,
        grant_starts_at=tail,
        grant_ends_at=tail + timedelta(days=1),
        new_next_billing_at=tail + timedelta(days=1),
    )
    db_session.add(entry)
    # A later paid period must not make the older compensation invisible.
    db_session.add(
        ServiceEntitlement(
            account_id=subscription.subscriber_id,
            subscription_id=subscription.id,
            starts_at=tail + timedelta(days=1),
            ends_at=tail + timedelta(days=31),
            amount_funded=100,
            currency="NGN",
            status=funded.status,
        )
    )
    subscription.next_billing_at = tail + timedelta(days=31)
    principal = create_review_staff(db_session)
    db_session.commit()
    _interval(db_session, subscription, start, end)
    preview = outages.preview_outage_compensation(
        db_session, subscription_id=subscription.id, effective_at=now
    )
    assert preview.status is OutageCompensationDecisionStatus.review_required
    assert str(entry.id) in preview.policy_snapshot["unresolved_time_credit_ids"]
    reviewed = outages.preview_legacy_time_credit(db_session, entry.id)
    command = outages.AttestLegacyTimeCreditCommand(
        entry_id=entry.id,
        ranges=reviewed.ranges,
        expected_fingerprint=reviewed.fingerprint,
        actor_system_user_id=principal.id,
    )
    context = CommandContext.system(
        actor=f"user:{principal.id}",
        scope=outages.OUTAGE_REPAIR_SCOPE,
        reason="Verified original compensated clock",
        idempotency_key=str(uuid4()),
    )
    db_session.rollback()
    outages.attest_legacy_time_credit(db_session, command, context=context)
    current = outages.preview_outage_compensation(
        db_session, subscription_id=subscription.id, effective_at=now
    )
    assert current.funded_overlap_seconds == 0


def test_finance_projection_exposes_receipt_and_approval_facts(
    db_session, purchase_setup, monkeypatch
):
    purchase_id, _, settlement = purchase_setup

    def reject(*args, **kwargs):
        raise purchases.PrepaidPeriodPurchaseError(
            code="test.hold", message="Held for review"
        )

    monkeypatch.setattr(purchases, "settle_prepaid_period_purchase", reject)
    held = purchases.settle_verified_prepaid_period_purchase(
        db_session, settlement, context=_context()
    )
    from app.services.web_billing_period_reviews import (
        PeriodReviewQuery,
        resolve_period_reviews,
    )

    rows = resolve_period_reviews(db_session, PeriodReviewQuery())
    row = next(row for row in rows if row.entity_id == purchase_id)
    assert row.receipts[0].payment_id == held.payment_id
    assert row.receipts[0].held_amount == settlement.amount
    assert row.fingerprint and row.reference
