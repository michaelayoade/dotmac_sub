"""Portal messaging — subscriber-facing messages and onboarding state."""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class PortalMessageType(enum.Enum):
    welcome = "welcome"
    announcement = "announcement"
    billing = "billing"
    service = "service"
    support = "support"
    system = "system"


class PortalMessageStatus(enum.Enum):
    unread = "unread"
    read = "read"
    archived = "archived"


class PortalMessage(Base):
    """In-app messages visible in the customer portal.

    Covers imported portal_messages, welcome_message, and
    instant_message tables.
    """

    __tablename__ = "portal_messages"
    __table_args__ = (
        Index("ix_portal_messages_subscriber", "subscriber_id"),
        Index("ix_portal_messages_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscribers.id", ondelete="CASCADE"),
        nullable=False,
    )
    message_type: Mapped[PortalMessageType] = mapped_column(
        Enum(
            PortalMessageType,
            name="portalmessagetype",
            create_constraint=False,
        ),
        nullable=False,
        default=PortalMessageType.system,
    )
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[PortalMessageStatus] = mapped_column(
        Enum(
            PortalMessageStatus,
            name="portalmessagestatus",
            create_constraint=False,
        ),
        nullable=False,
        default=PortalMessageStatus.unread,
    )
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    read_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

    subscriber = relationship("Subscriber")
