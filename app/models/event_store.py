"""Event store model for persisting events for resilience and retry.

This module provides the EventStore model which persists events before
dispatching to handlers, enabling retry of failed events.
"""

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Enum, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class EventStatus(enum.Enum):
    """Status of an event in the event store."""
    pending = "pending"
    processing = "processing"
    completed = "completed"
    failed = "failed"


class EventStore(Base):
    """Persistent storage for events.

    Events are stored before dispatching to handlers. This enables:
    - Retry of events that failed during handler processing
    - Audit trail of all events
    - Recovery from crashes during event processing
    """

    __tablename__ = "event_store"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[EventStatus] = mapped_column(
        Enum(EventStatus), default=EventStatus.pending, index=True
    )
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Context fields for filtering and debugging
    actor: Mapped[str | None] = mapped_column(String(255))
    subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), index=True
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), index=True
    )
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    ticket_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    service_order_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    # Tracking handlers that failed
    failed_handlers: Mapped[dict | None] = mapped_column(JSONB)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
