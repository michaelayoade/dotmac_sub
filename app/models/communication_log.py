"""Communication log — historical email, SMS, and in-app messages."""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class CommunicationDirection(enum.Enum):
    inbound = "inbound"
    outbound = "outbound"


class CommunicationChannel(enum.Enum):
    email = "email"
    sms = "sms"
    in_app = "in_app"
    whatsapp = "whatsapp"


class CommunicationStatus(enum.Enum):
    pending = "pending"
    sent = "sent"
    delivered = "delivered"
    failed = "failed"
    bounced = "bounced"


class CommunicationLog(Base):
    """Immutable log of communications sent to/from subscribers.

    Primary migration target for Splynx mail_pool (798K) and
    sms_pool (186K) tables.  Also used for ongoing outbound
    notification history.
    """

    __tablename__ = "communication_logs"
    __table_args__ = (
        Index("ix_communication_logs_subscriber", "subscriber_id"),
        Index("ix_communication_logs_channel", "channel"),
        Index("ix_communication_logs_sent_at", "sent_at"),
        Index(
            "uq_communication_logs_channel_external_id",
            "channel",
            "external_id",
            unique=True,
            postgresql_where=text("external_id IS NOT NULL"),
        ),
        Index(
            "uq_communication_logs_channel_splynx_message_id",
            "channel",
            "splynx_message_id",
            unique=True,
            postgresql_where=text("splynx_message_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=True
    )
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscriptions.id"), nullable=True
    )

    channel: Mapped[CommunicationChannel] = mapped_column(
        Enum(
            CommunicationChannel,
            name="communicationchannel",
            create_constraint=False,
        ),
        nullable=False,
    )
    direction: Mapped[CommunicationDirection] = mapped_column(
        Enum(
            CommunicationDirection,
            name="communicationdirection",
            create_constraint=False,
        ),
        nullable=False,
        default=CommunicationDirection.outbound,
    )
    recipient: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sender: Mapped[str | None] = mapped_column(String(255), nullable=True)
    subject: Mapped[str | None] = mapped_column(String(500), nullable=True)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[CommunicationStatus] = mapped_column(
        Enum(
            CommunicationStatus,
            name="communicationstatus",
            create_constraint=False,
        ),
        nullable=False,
        default=CommunicationStatus.sent,
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # External references for deduplication and traceability
    external_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    splynx_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Extra data (headers, attachment info, provider response)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

    subscriber = relationship("Subscriber")
    subscription = relationship("Subscription")
