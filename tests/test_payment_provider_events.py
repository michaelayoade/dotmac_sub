from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

import pytest

from app.models.audit import AuditEvent
from app.models.billing import (
    PaymentProvider,
    PaymentProviderEvent,
    PaymentProviderEventSource,
    PaymentProviderEventStatus,
    PaymentProviderType,
)
from app.models.event_store import EventStore
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext
from app.services.payment_provider_events import (
    ADMINISTRATIVE_INGEST_SCOPE,
    RECONCILIATION_PARTICIPANT_SCOPE,
    WEBHOOK_PARTICIPANT_SCOPE,
    PaymentProviderEventCommand,
    PaymentProviderEventError,
    PaymentProviderEventQuery,
    PaymentProviderEvents,
)


def _provider(db_session) -> PaymentProvider:
    provider = PaymentProvider(
        name=f"Provider Event {uuid4().hex}",
        provider_type=PaymentProviderType.custom,
        is_active=True,
    )
    db_session.add(provider)
    db_session.commit()
    return provider


def _context(key: str) -> CommandContext:
    return CommandContext.system(
        actor="pytest:payment-provider-events",
        scope=ADMINISTRATIVE_INGEST_SCOPE,
        reason="Verify administrative provider observation ownership",
        idempotency_key=key,
    )


def test_administrative_observation_is_typed_audited_and_exactly_replayed(db_session):
    provider = _provider(db_session)
    command = PaymentProviderEventCommand(
        provider_id=provider.id,
        event_type="provider.informational_notice",
        external_id=f"notice-{uuid4().hex}",
        amount=Decimal("25.00"),
        provider_fee=Decimal("0.50"),
        net_amount=Decimal("24.50"),
        currency="ngn",
        payload={"kind": "informational"},
    )
    context = _context(command.external_id or "missing")
    db_session_adapter.release_read_transaction(db_session)

    first = PaymentProviderEvents.ingest(db_session, command, context=context)
    second = PaymentProviderEvents.ingest(db_session, command, context=context)

    assert first.replayed is False
    assert second.replayed is True
    assert first.id == second.id
    assert first.source is PaymentProviderEventSource.administrative_ingest
    assert first.currency == "NGN"
    assert first.observation_digest is not None
    assert (
        db_session.query(PaymentProviderEvent)
        .filter_by(provider_id=provider.id)
        .count()
        == 1
    )
    assert (
        db_session.query(AuditEvent)
        .filter_by(entity_type="payment_provider_event", entity_id=str(first.id))
        .count()
        == 1
    )
    stored_events = (
        db_session.query(EventStore)
        .filter_by(event_type="payment_provider_event.processed")
        .all()
    )
    assert (
        sum(item.payload.get("aggregate_id") == str(first.id) for item in stored_events)
        == 1
    )

    listed = PaymentProviderEvents.list(
        db_session,
        PaymentProviderEventQuery(provider_id=provider.id),
    )
    assert listed == (PaymentProviderEvents.get(db_session, first.id),)


def test_administrative_observation_cannot_claim_payment_success(db_session):
    provider = _provider(db_session)
    command = PaymentProviderEventCommand(
        provider_id=provider.id,
        event_type="charge.success",
        external_id=f"untrusted-{uuid4().hex}",
        amount=Decimal("100.00"),
        currency="NGN",
    )
    db_session_adapter.release_read_transaction(db_session)

    with pytest.raises(PaymentProviderEventError) as captured:
        PaymentProviderEvents.ingest(
            db_session,
            command,
            context=_context(command.external_id or "missing"),
        )

    assert captured.value.code == (
        "financial.payment_provider_events.untrusted_financial_observation"
    )
    assert (
        db_session.query(PaymentProviderEvent)
        .filter_by(provider_id=provider.id)
        .count()
        == 0
    )


def test_provider_identity_reuse_with_changed_evidence_fails_closed(db_session):
    provider = _provider(db_session)
    identity = f"notice-{uuid4().hex}"
    command = PaymentProviderEventCommand(
        provider_id=provider.id,
        event_type="provider.informational_notice",
        external_id=identity,
        amount=Decimal("10.00"),
        currency="NGN",
    )
    db_session_adapter.release_read_transaction(db_session)
    PaymentProviderEvents.ingest(db_session, command, context=_context(identity))

    with pytest.raises(PaymentProviderEventError) as captured:
        PaymentProviderEvents.ingest(
            db_session,
            replace(command, amount=Decimal("11.00")),
            context=_context(identity),
        )

    assert captured.value.code == "financial.payment_provider_events.replay_conflict"
    assert (
        db_session.query(PaymentProviderEvent)
        .filter_by(provider_id=provider.id)
        .count()
        == 1
    )


def test_owner_rolls_back_record_audit_and_event_when_event_staging_fails(
    db_session, monkeypatch
):
    provider = _provider(db_session)
    identity = f"rollback-{uuid4().hex}"
    command = PaymentProviderEventCommand(
        provider_id=provider.id,
        event_type="provider.informational_notice",
        external_id=identity,
    )
    db_session_adapter.release_read_transaction(db_session)
    audit_count_before = (
        db_session.query(AuditEvent)
        .filter_by(entity_type="payment_provider_event")
        .count()
    )
    event_count_before = (
        db_session.query(EventStore)
        .filter_by(event_type="payment_provider_event.processed")
        .count()
    )
    db_session_adapter.release_read_transaction(db_session)
    monkeypatch.setattr(
        "app.services.payment_provider_events.emit_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("event failed")),
    )

    with pytest.raises(RuntimeError, match="event failed"):
        PaymentProviderEvents.ingest(
            db_session,
            command,
            context=_context(identity),
        )

    assert (
        db_session.query(PaymentProviderEvent)
        .filter_by(provider_id=provider.id)
        .count()
        == 0
    )
    assert (
        db_session.query(AuditEvent)
        .filter_by(entity_type="payment_provider_event")
        .count()
        == audit_count_before
    )
    assert (
        db_session.query(EventStore)
        .filter_by(event_type="payment_provider_event.processed")
        .count()
        == event_count_before
    )


def test_verified_participant_rejects_an_administrative_scope(db_session):
    provider = _provider(db_session)
    command = PaymentProviderEventCommand(
        provider_id=provider.id,
        event_type="provider.informational_notice",
        external_id=f"wrong-scope-{uuid4().hex}",
    )

    with pytest.raises(PaymentProviderEventError) as captured:
        PaymentProviderEvents.stage_verified_webhook_event(
            db_session,
            command,
            context=_context(command.external_id or "missing"),
        )

    assert captured.value.code == (
        "financial.payment_provider_events.command_scope_mismatch"
    )
    assert (
        db_session.query(PaymentProviderEvent)
        .filter_by(provider_id=provider.id)
        .count()
        == 0
    )


def test_semantically_equal_verified_sources_share_one_canonical_event(db_session):
    provider = _provider(db_session)
    identity = f"verified-{uuid4().hex}"
    webhook_command = PaymentProviderEventCommand(
        provider_id=provider.id,
        event_type="charge.success",
        external_id=identity,
        idempotency_key=f"paystack-{identity}",
        amount=Decimal("100.00"),
        provider_fee=Decimal("1.50"),
        net_amount=Decimal("98.50"),
        provider_reference=f"reference-{identity}",
        currency="NGN",
        payload={"source": "webhook", "received": "first"},
    )
    webhook_context = CommandContext.system(
        actor="provider:paystack",
        scope=WEBHOOK_PARTICIPANT_SCOPE,
        reason="Verify a signature-verified provider observation",
        idempotency_key=webhook_command.idempotency_key,
    )
    first = PaymentProviderEvents.stage_verified_webhook_event(
        db_session,
        webhook_command,
        context=webhook_context,
    )
    db_session.commit()

    reconciliation_command = replace(
        webhook_command,
        event_type="gateway.reconciliation.succeeded",
        payload={"source": "gateway_reconciliation", "observed": "later"},
        observed_payment_status=first.observed_payment_status,
    )
    second = PaymentProviderEvents.stage_verified_reconciliation_event(
        db_session,
        reconciliation_command,
        context=CommandContext.system(
            actor="scheduler:payment-reconciliation",
            scope=RECONCILIATION_PARTICIPANT_SCOPE,
            reason="Verify the same transaction through the gateway API",
            idempotency_key=reconciliation_command.idempotency_key,
        ),
    )

    assert first.status is PaymentProviderEventStatus.failed
    assert second.id == first.id
    assert second.replayed is True
    assert second.source is PaymentProviderEventSource.verified_webhook
    assert (
        db_session.query(PaymentProviderEvent)
        .filter_by(provider_id=provider.id)
        .count()
        == 1
    )
