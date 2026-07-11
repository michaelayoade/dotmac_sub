"""Event store model for persisting events for resilience and retry.

This module provides the EventStore model which persists events before
dispatching to handlers, enabling retry of failed events.
"""

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

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

    def __init__(self, **kwargs):
        if kwargs.get("handler_attempts") is None:
            kwargs.pop("handler_attempts", None)
        for key, value in kwargs.items():
            setattr(self, key, value)

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
    account_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    service_order_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    # Tracking handlers that failed
    failed_handlers: Mapped[list[dict[str, str]] | None] = mapped_column(JSONB)
    handler_attempts: Mapped[list["EventHandlerAttempt"]] = relationship(
        "EventHandlerAttempt",
        back_populates="event_store",
        cascade="all, delete-orphan",
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class EventHandlerAttempt(Base):
    """Per-handler processing attempts for an EventStore row."""

    __tablename__ = "event_handler_attempts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    event_store_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("event_store.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    handler_name: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    error: Mapped[str | None] = mapped_column(Text)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    attempted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    event_store: Mapped[EventStore] = relationship(
        "EventStore",
        back_populates="handler_attempts",
    )
