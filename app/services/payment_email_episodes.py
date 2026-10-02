"""Sub correlation owner: source recipient intents cover one queued payment email.

Every first part already has a durable Notification. A second part can join
only while that exact row remains queued and before the collection deadline.
There is no episode sweep, transport, retry loop, or new dispatchable event.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from dotmac_template_studio.composition import RenderedEmailPart, compose_email
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models.billing import (
    Invoice,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentAllocation,
    PaymentSettlement,
    PaymentStatus,
)
from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationStatus,
)
from app.models.payment_email import PaymentEmailEpisode, PaymentEmailPart
from app.services.communication_intents import (
    PlannedRecipient,
    cover_planned_recipient,
    execute_planned_recipient,
    recheck_covered_notification,
    recheck_planned_recipient,
)
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.operator_tenant import operator_tenant_id
from app.services.payment_email_content import (
    PaymentEmailKind,
    PublishedPaymentEmail,
    supports_plain_text_composition,
)

COLLECTION_WINDOW = timedelta(seconds=60)


@dataclass(frozen=True)
class ProvenPaymentPair:
    payment_id: UUID
    invoice_id: UUID
    subscriber_id: UUID
    allocation_id: UUID


@dataclass(frozen=True)
class PaymentEmailSource:
    pair: ProvenPaymentPair
    source_event_id: UUID
    kind: PaymentEmailKind
    content: PublishedPaymentEmail


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def prove_payment_pair(
    db: Session,
    *,
    payment_id: UUID,
    invoice_id: UUID,
    subscriber_id: UUID,
    causing_allocation_id: UUID | None = None,
    causing_ledger_entry_id: UUID | None = None,
) -> ProvenPaymentPair | None:
    payment = db.get(Payment, payment_id)
    invoice = db.get(Invoice, invoice_id)
    if (
        payment is None
        or invoice is None
        or not payment.is_active
        or payment.status is not PaymentStatus.succeeded
        or payment.account_id != subscriber_id
        or invoice.account_id != subscriber_id
    ):
        return None
    allocations = db.scalars(
        select(PaymentAllocation).where(
            PaymentAllocation.payment_id == payment_id,
            PaymentAllocation.is_active.is_(True),
        )
    ).all()
    if (
        len(allocations) != 1
        or allocations[0].invoice_id != invoice_id
        or allocations[0].ledger_entry_id is None
        or (
            causing_allocation_id is not None
            and causing_allocation_id != allocations[0].id
        )
    ):
        return None
    ledger = db.get(LedgerEntry, allocations[0].ledger_entry_id)
    if (
        ledger is None
        or not ledger.is_active
        or ledger.payment_id != payment_id
        or ledger.invoice_id != invoice_id
        or ledger.account_id != subscriber_id
        or ledger.entry_type is not LedgerEntryType.credit
        or ledger.source is not LedgerSource.payment
        or ledger.amount != allocations[0].amount
        or (
            causing_ledger_entry_id is not None and ledger.id != causing_ledger_entry_id
        )
    ):
        return None
    settlement = db.scalar(
        select(PaymentSettlement.id).where(PaymentSettlement.payment_id == payment_id)
    )
    if settlement is None:
        return None
    return ProvenPaymentPair(payment_id, invoice_id, subscriber_id, allocations[0].id)


def _serialize_first_creator(
    db: Session, source: PaymentEmailSource, recipient: str
) -> None:
    """Serialize the absent-row case; provider claims never take this lock.

    For an existing episode the row-lock order is Notification, episode,
    parts, source decisions/coverage. SQLite is a content-only test lane.
    """
    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        return
    if dialect != "postgresql":
        raise DomainError(
            code="payment_email_episodes.unsupported_database",
            message="Payment composition requires PostgreSQL",
            retryable=False,
        )
    identity = f"{operator_tenant_id()}:{source.pair.payment_id}:{source.pair.invoice_id}:{recipient}"
    key = int.from_bytes(
        hashlib.sha256(identity.encode()).digest()[:8], "big", signed=True
    )
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def _parts(db: Session, episode: PaymentEmailEpisode) -> list[PaymentEmailPart]:
    return list(
        db.scalars(
            select(PaymentEmailPart)
            .where(
                PaymentEmailPart.tenant_id == episode.tenant_id,
                PaymentEmailPart.episode_id == episode.id,
            )
            .with_for_update()
        ).all()
    )


def _compose(notification: Notification, parts: list[PaymentEmailPart]) -> None:
    if not parts:
        notification.status = NotificationStatus.canceled
        notification.last_error = "payment_email_sources_suppressed"
        return
    primary = next(
        (part for part in parts if part.kind == PaymentEmailKind.receipt.value),
        parts[0],
    )
    result = compose_email(
        tuple(
            RenderedEmailPart(
                part_id=str(part.id),
                order=0 if part.kind == PaymentEmailKind.receipt.value else 1,
                subject=part.subject,
                body=part.body,
            )
            for part in parts
        ),
        primary_part_id=str(primary.id),
    )
    notification.subject = result.subject
    notification.body = result.body


def stage_planned_payment_email(
    db: Session,
    *,
    source: PaymentEmailSource,
    recipient: PlannedRecipient,
    now: datetime | None = None,
) -> UUID | None:
    """Execute the first source now, or cover it with a compatible pending row."""
    if (
        not recipient.accepted
        or recipient.channel is not NotificationChannel.email
        or recipient.audience_type != "subscriber"
        or recipient.subscriber_id != source.pair.subscriber_id
        or not recipient.normalized_recipient
        or recipient.attachments
        or recipient.has_attachment_metadata
        or not supports_plain_text_composition(recipient.body or "")
    ):
        return execute_planned_recipient(db, recipient.decision_id).notification_id
    address = recipient.normalized_recipient
    _serialize_first_creator(db, source, address)
    episode = db.scalar(
        select(PaymentEmailEpisode).where(
            PaymentEmailEpisode.tenant_id == operator_tenant_id(),
            PaymentEmailEpisode.payment_id == source.pair.payment_id,
            PaymentEmailEpisode.invoice_id == source.pair.invoice_id,
            PaymentEmailEpisode.recipient == address,
        )
    )
    if episode is None:
        planned_at = utc(recipient.planned_at)
        deadline = planned_at + COLLECTION_WINDOW
        execution = execute_planned_recipient(
            db, recipient.decision_id, minimum_send_at=deadline
        )
        if not execution.queued or execution.notification_id is None:
            return execution.notification_id
        episode = PaymentEmailEpisode(
            tenant_id=operator_tenant_id(),
            payment_id=source.pair.payment_id,
            invoice_id=source.pair.invoice_id,
            subscriber_id=source.pair.subscriber_id,
            recipient=address,
            notification_id=execution.notification_id,
            deadline_at=deadline,
            created_at=planned_at,
        )
        db.add(episode)
        db.flush()
    else:
        notification = db.scalar(
            select(Notification)
            .where(Notification.id == episode.notification_id)
            .with_for_update()
        )
        db.refresh(episode, with_for_update=True)
        parts = _parts(db, episode)
        # A lock wait consumes the fixed collection window. Measure after
        # acquiring the delivery/episode locks, never at handler entry.
        checked_at = utc(now or datetime.now(UTC))
        if any(part.decision_id == recipient.decision_id for part in parts):
            return episode.notification_id
        if (
            notification is None
            or notification.status is not NotificationStatus.queued
            or checked_at >= utc(episode.deadline_at)
            or any(part.kind == source.kind.value for part in parts)
        ):
            return execute_planned_recipient(db, recipient.decision_id).notification_id
        if (
            notification.subscriber_id != recipient.subscriber_id
            or notification.audience_type != recipient.audience_type
            or notification.audience_id != recipient.audience_id
            or notification.category != recipient.category
            or (notification.metadata_ or {}).get("communication_class")
            != recipient.communication_class.value
        ):
            return execute_planned_recipient(db, recipient.decision_id).notification_id
        accepted = recheck_planned_recipient(
            db, recipient.decision_id, exclude_notification_id=notification.id
        )
        if not accepted.accepted:
            return None
        send_at = (
            utc(notification.send_at)
            if notification.send_at is not None
            else checked_at
        )
        if (
            accepted.canonical_send_at is not None
            and utc(accepted.canonical_send_at) > send_at
        ):
            return execute_planned_recipient(db, recipient.decision_id).notification_id
        part = _new_part(episode, source, accepted)
        db.add(part)
        db.flush()
        _record_source_collection(db, part, episode.notification_id)
        _compose(notification, [*parts, part])
        cover_planned_recipient(db, accepted.decision_id, notification.id)
        db.flush()
        return notification.id
    part = _new_part(episode, source, recipient)
    db.add(part)
    db.flush()
    _record_source_collection(db, part, episode.notification_id)
    return episode.notification_id


def _new_part(
    episode: PaymentEmailEpisode,
    source: PaymentEmailSource,
    recipient: PlannedRecipient,
) -> PaymentEmailPart:
    return PaymentEmailPart(
        tenant_id=episode.tenant_id,
        episode_id=episode.id,
        source_event_id=source.source_event_id,
        recipient=episode.recipient,
        decision_id=recipient.decision_id,
        kind=source.kind.value,
        content_template_id=source.content.template_id,
        content_version=source.content.version,
        subject=recipient.subject,
        body=recipient.body or "",
    )


def prepare_claimed_payment_email(db: Session, notification: Notification) -> None:
    """Rebuild from still-eligible covered sources under the worker's row lock."""
    episode = db.scalar(
        select(PaymentEmailEpisode)
        .where(
            PaymentEmailEpisode.tenant_id == operator_tenant_id(),
            PaymentEmailEpisode.notification_id == notification.id,
        )
        .with_for_update()
    )
    if episode is None:
        return
    parts = _parts(db, episode)
    recheck = recheck_covered_notification(db, notification.id)
    eligible_ids = frozenset(recheck.eligible_decision_ids)
    eligible = [part for part in parts if part.decision_id in eligible_ids]
    _compose(notification, eligible)
    db.flush()


def _record_source_collection(
    db: Session, part: PaymentEmailPart, notification_id: UUID
) -> None:
    emit_event(
        db,
        EventType.payment_email_source_collected,
        {
            "schema_version": 1,
            "episode_id": str(part.episode_id),
            "source_event_id": str(part.source_event_id),
            "decision_id": str(part.decision_id),
            "notification_id": str(notification_id),
            "content_template_id": str(part.content_template_id),
            "content_version": part.content_version,
        },
        actor="payment_email_episodes",
        defer_until_commit=True,
        dispatch_after_commit=False,
        record_only=True,
    )
