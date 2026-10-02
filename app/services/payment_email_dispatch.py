"""Thin payment event adapter to published content and canonical intent planning."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.orm import Session

from app.services.communication_intents import (
    CommunicationIntent,
    execute_planned_recipient,
    plan_intent,
)
from app.services.events.types import Event, EventType
from app.services.payment_email_content import PaymentEmailKind, PublishedPaymentEmail
from app.services.payment_email_cutover import composition_enabled
from app.services.payment_email_episodes import (
    PaymentEmailSource,
    prove_payment_pair,
    stage_planned_payment_email,
)


class ReceiptCorrelation(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    payment_id: UUID


class InvoicePaidCorrelation(ReceiptCorrelation):
    allocation_id: UUID
    ledger_entry_id: UUID
    source: Literal["payment_settlement"]
    previous_status: Literal["issued", "overdue", "partially_paid"]


@dataclass(frozen=True)
class PaymentEmailDispatchResult:
    intent_id: UUID
    notification_ids: tuple[UUID, ...]


def dispatch_payment_email(
    db: Session,
    *,
    event: Event,
    intent: CommunicationIntent,
    content: PublishedPaymentEmail,
) -> PaymentEmailDispatchResult:
    # The explicit collection minimum requests an ETA wakeup from the existing
    # queue owner; normal timing still honors quiet hours.
    plan = plan_intent(db, intent)
    kind = (
        PaymentEmailKind.receipt
        if event.event_type is EventType.payment_received
        else PaymentEmailKind.invoice_paid
    )
    pair = None
    try:
        correlation = (
            ReceiptCorrelation
            if kind is PaymentEmailKind.receipt
            else InvoicePaidCorrelation
        ).model_validate(event.payload)
        if (
            composition_enabled(db)
            and event.invoice_id is not None
            and intent.subscriber_id is not None
        ):
            pair = prove_payment_pair(
                db,
                payment_id=correlation.payment_id,
                invoice_id=event.invoice_id,
                subscriber_id=intent.subscriber_id,
                causing_allocation_id=correlation.allocation_id
                if isinstance(correlation, InvoicePaidCorrelation)
                else None,
                causing_ledger_entry_id=correlation.ledger_entry_id
                if isinstance(correlation, InvoicePaidCorrelation)
                else None,
            )
    except ValidationError:
        # A legacy or unrelated invoice-paid event remains an individual email.
        pass
    deliveries: set[UUID] = set()
    for recipient in plan.recipients:
        notification_id = (
            stage_planned_payment_email(
                db,
                source=PaymentEmailSource(pair, event.event_id, kind, content),
                recipient=recipient,
            )
            if pair is not None
            else execute_planned_recipient(db, recipient.decision_id).notification_id
        )
        if notification_id is not None:
            deliveries.add(notification_id)
    return PaymentEmailDispatchResult(
        plan.intent_id, tuple(sorted(deliveries, key=str))
    )
