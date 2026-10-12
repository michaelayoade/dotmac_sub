"""The customer's half of the job chat, from the selfcare portal.

Scoped to the subscriber's own work order on every call, exactly as
``customer_work_order_selfcare`` scopes the technician's location. A customer
message is an ordinary inbound ``InboxMessage`` on the job conversation, so the
technician sees it through the same conversation as everything else and it
counts as "awaiting reply" for the completion fallback.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.team_inbox import (
    InboxChannelType,
    InboxConversation,
    InboxMessage,
    InboxMessageDirection,
)
from app.models.work_order import WorkOrder
from app.services import (
    team_inbox_commands,
    team_inbox_field_job,
    team_inbox_realtime,
)
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.team_inbox_commands import (
    RecordFieldJobCustomerMessageCommand as SendCustomerChatMessageCommand,
)

MAX_CHAT_MESSAGES = 200
MAX_BODY_CHARS = 2000


class CustomerChatAvailabilityReason(StrEnum):
    not_found = "not_found"
    not_departed = "not_departed"
    open = "open"
    closed = "closed"


@dataclass(frozen=True, slots=True)
class CustomerChatAvailabilityQuery:
    subscriber_id: UUID
    work_order_public_id: str


@dataclass(frozen=True, slots=True)
class CustomerChatAvailability:
    available: bool
    can_send: bool
    reason: CustomerChatAvailabilityReason


@dataclass(frozen=True, slots=True)
class CustomerChatThreadQuery:
    subscriber_id: UUID
    work_order_public_id: str
    limit: int = 50


@dataclass(frozen=True, slots=True)
class CustomerChatMessage:
    id: UUID
    body: str
    from_customer: bool
    author_name: str | None
    created_at: datetime
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class CustomerChatThread:
    available: bool
    reason: CustomerChatAvailabilityReason | None = None
    can_send: bool = False
    work_order_id: str | None = None
    conversation_id: UUID | None = None
    technician_name: str | None = None
    messages: tuple[CustomerChatMessage, ...] = ()


def chat_availability(
    db: Session, *, query: CustomerChatAvailabilityQuery
) -> CustomerChatAvailability:
    """Expose existing owner-controlled history without guessing from job status."""
    row = _owned_work_order(db, query.subscriber_id, query.work_order_public_id)
    if row is None:
        return CustomerChatAvailability(
            False, False, CustomerChatAvailabilityReason.not_found
        )
    conversation = team_inbox_field_job.conversation_for(db, row)
    if conversation is not None and conversation.subscriber_id != row.subscriber_id:
        return CustomerChatAvailability(
            False, False, CustomerChatAvailabilityReason.not_found
        )
    if conversation is None:
        return CustomerChatAvailability(
            False, False, CustomerChatAvailabilityReason.not_departed
        )
    can_send = team_inbox_field_job.is_open(conversation)
    return CustomerChatAvailability(
        True,
        can_send,
        CustomerChatAvailabilityReason.open
        if can_send
        else CustomerChatAvailabilityReason.closed,
    )


class CustomerChatErrorReason(StrEnum):
    empty_body = "empty_body"
    not_found = "not_found"
    not_departed = "not_departed"
    closed = "closed"
    invalid_request = "invalid_request"
    idempotency_conflict = "idempotency_conflict"


class FieldJobChatError(DomainError, ValueError):
    """Raised when the customer cannot chat on this job."""

    def __init__(self, reason: CustomerChatErrorReason, message: str) -> None:
        self.reason = reason
        super().__init__(
            code=f"customer.field_job_chat.{reason.value}",
            message=message,
            retryable=False,
        )


def _owned_work_order(
    db: Session, subscriber_id: UUID, work_order_public_id: str
) -> WorkOrder | None:
    return db.scalar(
        select(WorkOrder).where(
            WorkOrder.subscriber_id == subscriber_id,
            WorkOrder.public_id == work_order_public_id,
            WorkOrder.work_order_kind == "customer",
            WorkOrder.is_active.is_(True),
        )
    )


def _message(message: InboxMessage) -> CustomerChatMessage:
    from_customer = message.direction == InboxMessageDirection.inbound.value
    author_name = (message.metadata_ or {}).get("author_name")
    return CustomerChatMessage(
        id=message.id,
        body=message.body or "",
        from_customer=from_customer,
        author_name=author_name
        if not from_customer and isinstance(author_name, str)
        else None,
        created_at=_utc(message.created_at),
    )


def _utc(value: datetime) -> datetime:
    # SQLite test transport drops timezone metadata; persisted instants are UTC.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _thread_payload(
    db: Session, conversation: InboxConversation, *, limit: int
) -> tuple[CustomerChatMessage, ...]:
    rows = (
        db.query(InboxMessage)
        .filter(InboxMessage.conversation_id == conversation.id)
        .filter(
            InboxMessage.direction.in_(
                (
                    InboxMessageDirection.inbound.value,
                    InboxMessageDirection.outbound.value,
                )
            )
        )
        .order_by(InboxMessage.created_at.desc())
        .limit(limit)
        .all()
    )
    rows.reverse()
    return tuple(_message(message) for message in rows)


def get_thread(db: Session, *, query: CustomerChatThreadQuery) -> CustomerChatThread:
    row = _owned_work_order(db, query.subscriber_id, query.work_order_public_id)
    if row is None:
        return CustomerChatThread(False, CustomerChatAvailabilityReason.not_found)

    conversation = team_inbox_field_job.conversation_for(db, row)
    if conversation is None:
        # The chat exists from the moment the technician sets off, not from
        # the moment the job is booked.
        return CustomerChatThread(
            False,
            CustomerChatAvailabilityReason.not_departed,
            work_order_id=row.public_id,
        )

    if conversation.subscriber_id != row.subscriber_id:
        return CustomerChatThread(False, CustomerChatAvailabilityReason.not_found)
    safe_limit = max(1, min(query.limit or 50, MAX_CHAT_MESSAGES))
    return CustomerChatThread(
        available=True,
        can_send=team_inbox_field_job.is_open(conversation),
        work_order_id=row.public_id,
        conversation_id=conversation.id,
        technician_name=row.technician_name,
        messages=_thread_payload(db, conversation, limit=safe_limit),
    )


def send_message(
    db: Session, *, command: SendCustomerChatMessageCommand
) -> CustomerChatMessage:
    text = command.body.strip()
    if not text:
        raise FieldJobChatError(
            CustomerChatErrorReason.empty_body, "A message is required"
        )
    if len(text) > MAX_BODY_CHARS:
        text = text[:MAX_BODY_CHARS]

    row = _owned_work_order(db, command.subscriber_id, command.work_order_public_id)
    if row is None:
        raise FieldJobChatError(CustomerChatErrorReason.not_found, "Visit not found")

    conversation = team_inbox_field_job.conversation_for(db, row)
    if conversation is None:
        raise FieldJobChatError(
            CustomerChatErrorReason.not_departed,
            "You can message your technician once they set off",
        )
    if not team_inbox_field_job.is_open(conversation):
        raise FieldJobChatError(
            CustomerChatErrorReason.closed, "This visit's chat has closed"
        )

    # Preflight improves adapter feedback; the canonical owner repeats scope and
    # open-state checks under root/conversation locks after releasing this read.
    db_session_adapter.release_read_transaction(db)
    try:
        written = team_inbox_commands.record_field_job_customer_message(
            db,
            command=replace(command, body=text),
        )
    except team_inbox_commands.FieldJobCustomerMessageError as exc:
        raise FieldJobChatError(
            CustomerChatErrorReason(exc.reason.value), exc.message
        ) from exc

    if not written.replayed:
        team_inbox_realtime.publish_conversation_event(
            db,
            str(written.conversation_id),
            event_type=team_inbox_realtime.EventType.MESSAGE_NEW,
            payload=team_inbox_realtime.message_event_payload(
                conversation_id=str(written.conversation_id),
                message_id=str(written.message_id),
                body=written.body,
                direction=InboxMessageDirection.inbound.value,
                channel_type=InboxChannelType.field_job.value,
                created_at=written.created_at,
                author_name=written.author_name,
                extra={"sender_type": "customer", "from_customer": True},
            ),
        )
    return CustomerChatMessage(
        id=written.message_id,
        body=written.body,
        from_customer=True,
        author_name=None,
        created_at=_utc(written.created_at),
        replayed=written.replayed,
    )
