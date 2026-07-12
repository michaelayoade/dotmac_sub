from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class CampaignType(enum.StrEnum):
    one_time = "one_time"
    nurture = "nurture"


class CampaignChannel(enum.StrEnum):
    email = "email"
    whatsapp = "whatsapp"


class CampaignStatus(enum.StrEnum):
    draft = "draft"
    scheduled = "scheduled"
    sending = "sending"
    completed = "completed"
    canceled = "canceled"
    failed = "failed"


class CampaignRecipientStatus(enum.StrEnum):
    pending = "pending"
    queued = "queued"
    sent = "sent"
    delivered = "delivered"
    opened = "opened"
    clicked = "clicked"
    failed = "failed"
    skipped = "skipped"
    replied = "replied"


class CampaignSender(Base):
    __tablename__ = "campaign_senders"
    __table_args__ = (
        UniqueConstraint("sender_key", name="uq_campaign_senders_sender_key"),
        Index("ix_campaign_senders_active", "is_active", "name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    sender_key: Mapped[str] = mapped_column(String(120), nullable=False)
    from_name: Mapped[str | None] = mapped_column(String(160))
    from_email: Mapped[str | None] = mapped_column(String(255))
    reply_to: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class Campaign(Base):
    __tablename__ = "campaigns"
    __table_args__ = (
        Index("ix_campaigns_status_scheduled", "status", "scheduled_at"),
        Index("ix_campaigns_channel_status", "channel", "status"),
        Index("ix_campaigns_created_by", "created_by_system_user_id"),
        Index(
            "uq_campaigns_crm_campaign_id",
            "crm_campaign_id",
            unique=True,
            postgresql_where=text("crm_campaign_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    crm_campaign_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    campaign_type: Mapped[str] = mapped_column(
        String(40), default=CampaignType.one_time.value, nullable=False
    )
    channel: Mapped[str] = mapped_column(
        String(40), default=CampaignChannel.email.value, nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(40), default=CampaignStatus.draft.value, nullable=False
    )
    subject: Mapped[str | None] = mapped_column(String(200))
    body_html: Mapped[str | None] = mapped_column(Text)
    body_text: Mapped[str | None] = mapped_column(Text)
    from_name: Mapped[str | None] = mapped_column(String(160))
    from_email: Mapped[str | None] = mapped_column(String(255))
    reply_to: Mapped[str | None] = mapped_column(String(255))
    whatsapp_template_name: Mapped[str | None] = mapped_column(String(200))
    whatsapp_template_language: Mapped[str | None] = mapped_column(String(10))
    whatsapp_template_components: Mapped[dict | None] = mapped_column(JSON)
    segment_filter: Mapped[dict | None] = mapped_column(JSON)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sending_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    total_recipients: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sent_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    delivered_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    opened_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    clicked_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_by_system_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("system_users.id")
    )
    campaign_sender_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaign_senders.id")
    )
    service_team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_teams.id")
    )
    connector_config_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("connector_configs.id")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    sender = relationship("CampaignSender")
    service_team = relationship("ServiceTeam")
    connector_config = relationship("ConnectorConfig")
    steps = relationship(
        "CampaignStep", back_populates="campaign", order_by="CampaignStep.step_index"
    )
    recipients = relationship("CampaignRecipient", back_populates="campaign")


class CampaignStep(Base):
    __tablename__ = "campaign_steps"
    __table_args__ = (
        UniqueConstraint("campaign_id", "step_index", name="uq_campaign_steps_index"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id"), nullable=False
    )
    step_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    name: Mapped[str | None] = mapped_column(String(200))
    subject: Mapped[str | None] = mapped_column(String(200))
    body_html: Mapped[str | None] = mapped_column(Text)
    body_text: Mapped[str | None] = mapped_column(Text)
    delay_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    campaign = relationship("Campaign", back_populates="steps")
    recipients = relationship("CampaignRecipient", back_populates="step")


class CampaignRecipient(Base):
    __tablename__ = "campaign_recipients"
    __table_args__ = (
        UniqueConstraint(
            "campaign_id", "subscriber_id", "step_id", name="uq_campaign_sub_step"
        ),
        Index(
            "uq_campaign_sub_null_step",
            "campaign_id",
            "subscriber_id",
            unique=True,
            postgresql_where=text("step_id IS NULL"),
        ),
        Index("ix_campaign_recipients_status", "campaign_id", "status"),
        Index("ix_campaign_recipients_subscriber", "subscriber_id"),
        Index("ix_campaign_recipients_conversation", "conversation_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id"), nullable=False
    )
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=False
    )
    step_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaign_steps.id")
    )
    address: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(
        String(40), default=CampaignRecipientStatus.pending.value, nullable=False
    )
    notification_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("notifications.id")
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("inbox_conversations.id")
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("inbox_messages.id")
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_reason: Mapped[str | None] = mapped_column(Text)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    clicked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    open_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    click_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    campaign = relationship("Campaign", back_populates="recipients")
    subscriber = relationship("Subscriber")
    step = relationship("CampaignStep", back_populates="recipients")
    notification = relationship("Notification")
    conversation = relationship("InboxConversation")
    message = relationship("InboxMessage")
