import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.mutable import MutableDict, MutableList
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class TicketStatus(enum.Enum):
    new = "new"
    open = "open"
    pending = "pending"
    waiting_on_customer = "waiting_on_customer"
    lastmile_rerun = "lastmile_rerun"
    site_under_construction = "site_under_construction"
    on_hold = "on_hold"
    resolved = "resolved"
    closed = "closed"
    canceled = "canceled"
    merged = "merged"


class TicketPriority(enum.Enum):
    lower = "lower"
    low = "low"
    medium = "medium"
    normal = "normal"
    high = "high"
    urgent = "urgent"


class TicketChannel(enum.Enum):
    web = "web"
    email = "email"
    phone = "phone"
    chat = "chat"
    api = "api"


class Ticket(Base):
    __tablename__ = "support_tickets"
    __table_args__ = (
        Index("ix_support_tickets_number", "number"),
        Index("ix_support_tickets_status", "status"),
        Index("ix_support_tickets_priority", "priority"),
        Index("ix_support_tickets_subscriber", "subscriber_id"),
        Index("ix_support_tickets_active", "is_active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # Optional relations
    subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id")
    )
    customer_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id")
    )
    lead_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    customer_person_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id")
    )
    created_by_person_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id")
    )
    assigned_to_person_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id")
    )
    technician_person_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id")
    )
    ticket_manager_person_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id")
    )
    site_coordinator_person_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id")
    )
    service_team_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    number: Mapped[str | None] = mapped_column(String(50), unique=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    region: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[TicketStatus] = mapped_column(
        Enum(TicketStatus, values_callable=lambda x: [e.value for e in x]),
        default=TicketStatus.open,
        nullable=False,
    )
    priority: Mapped[TicketPriority] = mapped_column(
        Enum(TicketPriority, values_callable=lambda x: [e.value for e in x]),
        default=TicketPriority.normal,
        nullable=False,
    )
    ticket_type: Mapped[str | None] = mapped_column(String(80))
    channel: Mapped[TicketChannel] = mapped_column(
        Enum(TicketChannel, values_callable=lambda x: [e.value for e in x]),
        default=TicketChannel.web,
        nullable=False,
    )
    tags: Mapped[list[str] | None] = mapped_column(
        MutableList.as_mutable(JSON()), default=list
    )
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )
    attachments: Mapped[list[dict] | None] = mapped_column(
        MutableList.as_mutable(JSON()), default=list
    )

    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    merged_into_ticket_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("support_tickets.id")
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    comments = relationship(
        "TicketComment", back_populates="ticket", cascade="all, delete-orphan"
    )
    assignees = relationship(
        "TicketAssignee", back_populates="ticket", cascade="all, delete-orphan"
    )
    sla_events = relationship(
        "TicketSlaEvent", back_populates="ticket", cascade="all, delete-orphan"
    )


class TicketAssignee(Base):
    __tablename__ = "support_ticket_assignees"
    __table_args__ = (
        UniqueConstraint("ticket_id", "person_id", name="uq_support_ticket_assignee"),
    )

    ticket_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("support_tickets.id"), primary_key=True
    )
    person_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    ticket = relationship("Ticket", back_populates="assignees")


class TicketComment(Base):
    __tablename__ = "support_ticket_comments"
    __table_args__ = (Index("ix_support_ticket_comments_ticket", "ticket_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("support_tickets.id"), nullable=False
    )
    author_person_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id")
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    is_internal: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    attachments: Mapped[list[dict] | None] = mapped_column(
        MutableList.as_mutable(JSON()), default=list
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    ticket = relationship("Ticket", back_populates="comments")


class TicketSlaEvent(Base):
    __tablename__ = "support_ticket_sla_events"
    __table_args__ = (Index("ix_support_ticket_sla_events_ticket", "ticket_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("support_tickets.id"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    expected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    actual_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    ticket = relationship("Ticket", back_populates="sla_events")


class TicketMerge(Base):
    __tablename__ = "support_ticket_merges"
    __table_args__ = (
        UniqueConstraint(
            "source_ticket_id", "target_ticket_id", name="uq_support_ticket_merge_pair"
        ),
    )

    source_ticket_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("support_tickets.id"), primary_key=True
    )
    target_ticket_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("support_tickets.id"), primary_key=True
    )
    reason: Mapped[str | None] = mapped_column(Text)
    merged_by_person_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class TicketLink(Base):
    __tablename__ = "support_ticket_links"
    __table_args__ = (
        UniqueConstraint(
            "from_ticket_id", "to_ticket_id", "link_type", name="uq_support_ticket_link"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    from_ticket_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("support_tickets.id"), nullable=False
    )
    to_ticket_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("support_tickets.id"), nullable=False
    )
    link_type: Mapped[str] = mapped_column(String(80), nullable=False)
    created_by_person_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
