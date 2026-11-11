"""Read-only, customer-scoped access to historical communication logs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.communication_log import (
    CommunicationChannel,
    CommunicationDirection,
    CommunicationLog,
    CommunicationStatus,
)
from app.models.subscriber import Subscriber, UserType


@dataclass(frozen=True)
class CustomerEmailDetailQuery:
    customer_id: UUID
    message_id: UUID


@dataclass(frozen=True)
class CustomerEmailDetail:
    id: UUID
    customer_id: UUID
    subject: str | None
    sender: str | None
    recipient: str | None
    body: str | None
    direction: CommunicationDirection
    status: CommunicationStatus
    sent_at: datetime | None
    created_at: datetime


def get_customer_email_detail(
    db: Session, *, query: CustomerEmailDetailQuery
) -> CustomerEmailDetail | None:
    """Return one email only when it belongs to the named customer."""
    customer = db.get(Subscriber, query.customer_id)
    if customer is None or customer.user_type != UserType.customer:
        return None

    message = (
        db.query(CommunicationLog)
        .filter(
            CommunicationLog.id == query.message_id,
            CommunicationLog.subscriber_id == query.customer_id,
            CommunicationLog.channel == CommunicationChannel.email,
        )
        .one_or_none()
    )
    if message is None:
        return None

    return CustomerEmailDetail(
        id=message.id,
        customer_id=query.customer_id,
        subject=message.subject,
        sender=message.sender,
        recipient=message.recipient,
        body=message.body,
        direction=message.direction,
        status=message.status,
        sent_at=message.sent_at,
        created_at=message.created_at,
    )
