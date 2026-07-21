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
