import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class SplynxArchivedTicket(Base):
    """Frozen read-only archive of Splynx support tickets."""

    __tablename__ = "splynx_archived_tickets"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    splynx_ticket_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id")
    )
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="open")
    priority: Mapped[str] = mapped_column(String(20), nullable=False, default="normal")
    assigned_to: Mapped[str | None] = mapped_column(String(160))
    created_by: Mapped[str | None] = mapped_column(String(160))
    body: Mapped[str | None] = mapped_column(Text)
    splynx_metadata: Mapped[dict | None] = mapped_column(JSONB, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    subscriber = relationship("Subscriber")
    messages = relationship("SplynxArchivedTicketMessage", back_populates="ticket")


class SplynxArchivedTicketMessage(Base):
    """Frozen read-only archive of Splynx ticket messages."""

    __tablename__ = "splynx_archived_ticket_messages"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    splynx_message_id: Mapped[int] = mapped_column(
        Integer, unique=True, nullable=False
    )
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("splynx_archived_tickets.id"),
        nullable=False,
    )
    sender_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="customer"
    )
    sender_name: Mapped[str | None] = mapped_column(String(160))
    body: Mapped[str | None] = mapped_column(Text)
    is_internal: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    ticket = relationship("SplynxArchivedTicket", back_populates="messages")


class SplynxArchivedQuote(Base):
    """Frozen read-only archive of Splynx quotes."""

    __tablename__ = "splynx_archived_quotes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    splynx_quote_id: Mapped[int] = mapped_column(
        Integer, unique=True, nullable=False
    )
    subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id")
    )
    quote_number: Mapped[str | None] = mapped_column(String(60))
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="draft")
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="NGN")
    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    tax_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    memo: Mapped[str | None] = mapped_column(Text)
    splynx_metadata: Mapped[dict | None] = mapped_column(JSONB, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    subscriber = relationship("Subscriber")
    items = relationship("SplynxArchivedQuoteItem", back_populates="quote")


class SplynxArchivedQuoteItem(Base):
    """Frozen read-only archive of Splynx quote line items."""

    __tablename__ = "splynx_archived_quote_items"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    splynx_item_id: Mapped[int | None] = mapped_column(Integer)
    quote_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("splynx_archived_quotes.id"),
        nullable=False,
    )
    description: Mapped[str | None] = mapped_column(Text)
    quantity: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=1)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    quote = relationship("SplynxArchivedQuote", back_populates="items")
