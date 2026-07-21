"""Test builders for the typed payment-provider event owner."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.schemas.billing import PaymentProviderEventIngest
from app.services import billing as billing_service
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext
from app.services.payment_provider_events import (
    ADMINISTRATIVE_INGEST_SCOPE,
    WEBHOOK_PARTICIPANT_SCOPE,
    PaymentProviderEventCommand,
    PaymentProviderEventResult,
)


def provider_event_command(
    payload: PaymentProviderEventIngest,
) -> PaymentProviderEventCommand:
    data = payload.model_dump()
    observed_status = data.pop("status_hint")
    return PaymentProviderEventCommand(
        **data,
        observed_payment_status=observed_status,
    )


def stage_verified_provider_event(
    db: Session,
    payload: PaymentProviderEventIngest,
) -> PaymentProviderEventResult:
    command = provider_event_command(payload)
    result = billing_service.payment_provider_events.stage_verified_webhook_event(
        db,
        command,
        context=CommandContext.system(
            actor="pytest:payment-provider-event",
            scope=WEBHOOK_PARTICIPANT_SCOPE,
            reason="Exercise signature-verified provider-event participant",
            idempotency_key=command.idempotency_key or command.external_id,
        ),
    )
    db.commit()
    return result


def ingest_administrative_event(
    db: Session,
    payload: PaymentProviderEventIngest,
) -> PaymentProviderEventResult:
    command = provider_event_command(payload)
    db_session_adapter.release_read_transaction(db)
    return billing_service.payment_provider_events.ingest(
        db,
        command,
        context=CommandContext.system(
            actor="pytest:payment-provider-event-admin",
            scope=ADMINISTRATIVE_INGEST_SCOPE,
            reason="Exercise administrative provider-event command",
            idempotency_key=command.idempotency_key or command.external_id,
        ),
    )
