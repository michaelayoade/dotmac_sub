"""Canonical TopupIntent completion and expiry projection behavior."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.models.billing import (
    Payment,
    PaymentProvider,
    PaymentProviderType,
    PaymentStatus,
    TopupIntent,
)
from app.models.event_store import EventStore
from app.models.subscriber import Subscriber
from app.services import topup_intents
from app.services.owner_commands import CommandContext
from app.services.payment_gateway_adapter import (
    PaymentGatewayProviderStatus,
    PaymentGatewayVerificationObservation,
    PaymentGatewayVerificationOutcome,
    PaymentGatewayVerificationReason,
)


def _context(scope: str) -> CommandContext:
    return CommandContext.system(
        actor="pytest:topup-projection",
        scope=scope,
        reason="Top-up intent projection behavior test",
    )


def _provider(db_session) -> PaymentProvider:
    provider = PaymentProvider(
        name="Projection Paystack",
        provider_type=PaymentProviderType.paystack,
    )
    db_session.add(provider)
    db_session.commit()
    return provider


def _intent_and_payment(db_session, subscriber) -> tuple[TopupIntent, Payment]:
    provider = _provider(db_session)
    intent = TopupIntent(
        account_id=subscriber.id,
        provider_id=provider.id,
        reference="projection-ref-1",
        provider_type="paystack",
        currency="NGN",
        requested_amount=Decimal("5000.00"),
        status="pending",
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    payment = Payment(
        account_id=subscriber.id,
        provider_id=provider.id,
        amount=Decimal("5000.00"),
        currency="NGN",
        status=PaymentStatus.succeeded,
        external_id="projection-payment-1",
        paid_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    db_session.add_all([intent, payment])
    db_session.commit()
    return intent, payment


def test_completion_derives_locked_payment_evidence_and_emits_once(
    db_session, subscriber
):
    intent, payment = _intent_and_payment(db_session, subscriber)
    command = topup_intents.CompleteTopupIntentCommand(
        intent_id=intent.id,
        payment_id=payment.id,
        source=topup_intents.TopupIntentCompletionSource.gateway_reconciliation,
    )

    first = topup_intents.stage_topup_intent_completion(
        db_session,
        command,
        context=_context(topup_intents.COMPLETION_SCOPE),
    )
    db_session.commit()
    second = topup_intents.stage_topup_intent_completion(
        db_session,
        command,
        context=_context(topup_intents.COMPLETION_SCOPE),
    )
    db_session.commit()

    db_session.refresh(intent)
    assert first.changed is True
    assert second.changed is False
    assert intent.status == "completed"
    assert intent.completed_payment_id == payment.id
    assert intent.external_id == payment.external_id
    assert intent.actual_amount == payment.amount
    assert intent.completed_at == payment.paid_at
    events = (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "topup_intent.completed")
        .all()
    )
    assert len(events) == 1
    assert events[0].payload["payment_id"] == str(payment.id)
    assert events[0].payload["source"] == "gateway_reconciliation"


def test_completion_rejects_payment_from_another_account(db_session, subscriber):
    intent, payment = _intent_and_payment(db_session, subscriber)
    other = Subscriber(
        first_name="Projection",
        last_name="Other",
        email=f"projection-{uuid.uuid4().hex}@example.com",
        reseller_id=subscriber.reseller_id,
    )
    db_session.add(other)
    db_session.flush()
    payment.account_id = other.id
    db_session.commit()

    with pytest.raises(topup_intents.TopupIntentError) as exc:
        topup_intents.stage_topup_intent_completion(
            db_session,
            topup_intents.CompleteTopupIntentCommand(
                intent_id=intent.id,
                payment_id=payment.id,
                source=(
                    topup_intents.TopupIntentCompletionSource.gateway_reconciliation
                ),
            ),
            context=_context(topup_intents.COMPLETION_SCOPE),
        )

    assert exc.value.code == "financial.topup_intents.payment_scope_mismatch"
    db_session.expire_all()
    persisted = db_session.get(TopupIntent, intent.id)
    assert persisted is not None
    assert persisted.status == "pending"
    assert persisted.completed_payment_id is None


def test_completion_event_failure_rolls_back_projection(
    db_session, subscriber, monkeypatch
):
    intent, payment = _intent_and_payment(db_session, subscriber)

    def fail_event(*_args, **_kwargs):
        raise RuntimeError("completion event unavailable")

    monkeypatch.setattr(topup_intents, "emit_event", fail_event)
    intent_id = intent.id
    nested = db_session.begin_nested()

    with pytest.raises(RuntimeError, match="completion event unavailable"):
        topup_intents.stage_topup_intent_completion(
            db_session,
            topup_intents.CompleteTopupIntentCommand(
                intent_id=intent.id,
                payment_id=payment.id,
                source=topup_intents.TopupIntentCompletionSource.provider_webhook,
            ),
            context=_context(topup_intents.COMPLETION_SCOPE),
        )
    nested.rollback()
    db_session.expire_all()

    persisted = db_session.get(TopupIntent, intent_id)
    assert persisted is not None
    assert persisted.status == "pending"
    assert persisted.completed_payment_id is None


def test_expiry_uses_due_time_and_emits_idempotently(db_session, subscriber):
    intent = TopupIntent(
        account_id=subscriber.id,
        reference="projection-expiry-1",
        provider_type="paystack",
        currency="NGN",
        requested_amount=Decimal("5000.00"),
        status="pending",
        expires_at=datetime.now(UTC) - timedelta(days=2),
    )
    db_session.add(intent)
    db_session.commit()
    command = topup_intents.ExpireTopupIntentCommand(
        intent_id=intent.id,
        observed_at=datetime.now(UTC),
        grace=timedelta(hours=24),
        source=topup_intents.TopupIntentExpirySource.gateway_reconciliation,
    )

    first = topup_intents.stage_topup_intent_expiry(
        db_session,
        command,
        context=_context(topup_intents.EXPIRY_SCOPE),
    )
    db_session.commit()
    second = topup_intents.stage_topup_intent_expiry(
        db_session,
        command,
        context=_context(topup_intents.EXPIRY_SCOPE),
    )
    db_session.commit()

    assert first.changed is True
    assert second.changed is False
    assert db_session.get(TopupIntent, intent.id).status == "expired"
    assert (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "topup_intent.expired")
        .count()
        == 1
    )


def test_expiry_keeps_intent_pending_before_grace_elapses(db_session, subscriber):
    intent = TopupIntent(
        account_id=subscriber.id,
        reference="projection-expiry-not-due",
        provider_type="paystack",
        currency="NGN",
        requested_amount=Decimal("5000.00"),
        status="pending",
        expires_at=datetime.now(UTC) - timedelta(hours=2),
    )
    db_session.add(intent)
    db_session.commit()

    result = topup_intents.stage_topup_intent_expiry(
        db_session,
        topup_intents.ExpireTopupIntentCommand(
            intent_id=intent.id,
            observed_at=datetime.now(UTC),
            grace=timedelta(hours=24),
            source=topup_intents.TopupIntentExpirySource.gateway_reconciliation,
        ),
        context=_context(topup_intents.EXPIRY_SCOPE),
    )

    assert result.changed is False
    assert db_session.get(TopupIntent, intent.id).status == "pending"
    assert db_session.query(EventStore).count() == 0


def _observation(outcome: PaymentGatewayVerificationOutcome):
    status = {
        PaymentGatewayVerificationOutcome.failed: PaymentGatewayProviderStatus.failed,
        PaymentGatewayVerificationOutcome.abandoned: (
            PaymentGatewayProviderStatus.abandoned
        ),
        PaymentGatewayVerificationOutcome.processing: (
            PaymentGatewayProviderStatus.processing
        ),
    }.get(outcome)
    reason = {
        PaymentGatewayVerificationOutcome.failed: (
            PaymentGatewayVerificationReason.provider_reported_failed
        ),
        PaymentGatewayVerificationOutcome.abandoned: (
            PaymentGatewayVerificationReason.provider_reported_abandoned
        ),
        PaymentGatewayVerificationOutcome.processing: (
            PaymentGatewayVerificationReason.provider_reported_processing
        ),
        PaymentGatewayVerificationOutcome.unavailable: (
            PaymentGatewayVerificationReason.provider_unavailable
        ),
    }[outcome]
    return PaymentGatewayVerificationObservation(
        outcome=outcome,
        provider_status=status,
        reason_code=reason,
    )


def _same_utc_instant(left: datetime | None, right: datetime) -> bool:
    if left is None:
        return False
    if left.tzinfo is None:
        left = left.replace(tzinfo=UTC)
    return left == right


@pytest.mark.parametrize(
    ("outcome", "status", "normalized"),
    (
        (PaymentGatewayVerificationOutcome.failed, "failed", "failed"),
        (PaymentGatewayVerificationOutcome.abandoned, "abandoned", "abandoned"),
    ),
)
def test_terminal_gateway_observation_releases_blocker_without_money(
    db_session, subscriber, outcome, status, normalized
):
    intent = TopupIntent(
        account_id=subscriber.id,
        reference=f"terminal-{status}",
        provider_type="paystack",
        currency="NGN",
        requested_amount=Decimal("5000.00"),
        status="pending",
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
        metadata_={"payment_flow": "account_topup"},
    )
    db_session.add(intent)
    db_session.commit()
    observed_at = datetime.now(UTC)
    next_reconcile_at = observed_at + timedelta(hours=24)

    result = topup_intents.stage_gateway_topup_observation(
        db_session,
        topup_intents.RecordGatewayTopupObservationCommand(
            intent_id=intent.id,
            observation=_observation(outcome),
            observed_at=observed_at,
            source=topup_intents.GatewayTopupObservationSource.gateway_reconciliation,
            next_reconcile_at=next_reconcile_at,
        ),
        context=_context(topup_intents.GATEWAY_OBSERVATION_SCOPE),
    )
    db_session.commit()

    projection = topup_intents.project_topup_intent_lifecycle(intent)
    assert result.status.value == status
    assert projection.normalized_status.value == normalized
    assert projection.blocks_another_attempt is False
    assert projection.customer_retry_allowed is True
    assert projection.reason_code == _observation(outcome).reason_code.value
    assert projection.last_verification_at is not None
    assert _same_utc_instant(intent.gateway_last_observed_at, observed_at)
    assert intent.gateway_last_outcome == outcome.value
    assert intent.gateway_last_reason_code == _observation(outcome).reason_code.value
    assert _same_utc_instant(intent.gateway_next_reconcile_at, next_reconcile_at)
    assert intent.gateway_observation_count == 1
    assert db_session.query(Payment).count() == 0


@pytest.mark.parametrize(
    ("outcome", "normalized"),
    (
        (PaymentGatewayVerificationOutcome.processing, "processing"),
        (
            PaymentGatewayVerificationOutcome.unavailable,
            "confirmation_unavailable",
        ),
    ),
)
def test_nonterminal_or_ambiguous_observation_remains_blocking(
    db_session, subscriber, outcome, normalized
):
    intent = TopupIntent(
        account_id=subscriber.id,
        reference=f"nonterminal-{outcome.value}",
        provider_type="paystack",
        currency="NGN",
        requested_amount=Decimal("5000.00"),
        status="pending",
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    db_session.add(intent)
    db_session.commit()
    observed_at = datetime.now(UTC)
    next_reconcile_at = observed_at + timedelta(minutes=30)

    topup_intents.stage_gateway_topup_observation(
        db_session,
        topup_intents.RecordGatewayTopupObservationCommand(
            intent_id=intent.id,
            observation=_observation(outcome),
            observed_at=observed_at,
            source=topup_intents.GatewayTopupObservationSource.gateway_reconciliation,
            next_reconcile_at=next_reconcile_at,
        ),
        context=_context(topup_intents.GATEWAY_OBSERVATION_SCOPE),
    )
    db_session.commit()

    projection = topup_intents.project_topup_intent_lifecycle(intent)
    assert intent.status == "pending"
    assert projection.normalized_status.value == normalized
    assert projection.blocks_another_attempt is True
    assert projection.customer_retry_allowed is False
    assert projection.last_verification_at == observed_at
    assert _same_utc_instant(intent.gateway_next_reconcile_at, next_reconcile_at)
    assert intent.gateway_observation_count == 1


def test_ambiguous_observation_persists_expiry_and_releases_retry(
    db_session, subscriber
):
    intent = TopupIntent(
        account_id=subscriber.id,
        reference="ambiguous-expired",
        provider_type="paystack",
        currency="NGN",
        requested_amount=Decimal("5000.00"),
        status="pending",
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    db_session.add(intent)
    db_session.commit()

    topup_intents.stage_gateway_topup_observation(
        db_session,
        topup_intents.RecordGatewayTopupObservationCommand(
            intent_id=intent.id,
            observation=_observation(PaymentGatewayVerificationOutcome.unavailable),
            observed_at=datetime.now(UTC),
            source=topup_intents.GatewayTopupObservationSource.gateway_reconciliation,
        ),
        context=_context(topup_intents.GATEWAY_OBSERVATION_SCOPE),
    )
    db_session.commit()

    projection = topup_intents.project_topup_intent_lifecycle(intent)
    assert intent.status == "expired"
    assert projection.label == "Expired"
    assert projection.blocks_another_attempt is False
    assert projection.customer_retry_allowed is True
    assert projection.reason_code == topup_intents.INTENT_EXPIRED_REASON_CODE


def test_terminal_reason_survives_a_later_unavailable_check(db_session, subscriber):
    intent = TopupIntent(
        account_id=subscriber.id,
        reference="terminal-reason-preserved",
        provider_type="paystack",
        currency="NGN",
        requested_amount=Decimal("5000.00"),
        status="pending",
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    db_session.add(intent)
    db_session.commit()
    first_observed_at = datetime.now(UTC)
    later_observed_at = first_observed_at + timedelta(minutes=2)

    for observation, observed_at in (
        (_observation(PaymentGatewayVerificationOutcome.failed), first_observed_at),
        (
            _observation(PaymentGatewayVerificationOutcome.unavailable),
            later_observed_at,
        ),
    ):
        topup_intents.stage_gateway_topup_observation(
            db_session,
            topup_intents.RecordGatewayTopupObservationCommand(
                intent_id=intent.id,
                observation=observation,
                observed_at=observed_at,
                source=(
                    topup_intents.GatewayTopupObservationSource.gateway_reconciliation
                ),
            ),
            context=_context(topup_intents.GATEWAY_OBSERVATION_SCOPE),
        )
        db_session.commit()

    projection = topup_intents.project_topup_intent_lifecycle(intent)

    assert projection.normalized_status.value == "failed"
    assert projection.reason_code == "provider_reported_failed"
    assert projection.last_verification_at == later_observed_at


@pytest.mark.parametrize("prior_status", ("failed", "abandoned", "expired"))
def test_authoritative_late_success_reopens_terminal_intent_once(
    db_session, subscriber, prior_status
):
    intent, payment = _intent_and_payment(db_session, subscriber)
    intent.status = prior_status
    db_session.commit()
    command = topup_intents.CompleteTopupIntentCommand(
        intent_id=intent.id,
        payment_id=payment.id,
        source=topup_intents.TopupIntentCompletionSource.gateway_reconciliation,
    )

    first = topup_intents.stage_topup_intent_completion(
        db_session, command, context=_context(topup_intents.COMPLETION_SCOPE)
    )
    db_session.commit()
    second = topup_intents.stage_topup_intent_completion(
        db_session, command, context=_context(topup_intents.COMPLETION_SCOPE)
    )
    db_session.commit()

    assert first.changed is True
    assert second.changed is False
    assert intent.status == "completed"
    assert intent.completed_payment_id == payment.id
