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
from app.models.network_monitoring import AlertSeverity, AlertStatus


class NotificationChannel(enum.Enum):
    email = "email"
    sms = "sms"
    push = "push"
    whatsapp = "whatsapp"
    webhook = "webhook"


class NotificationStatus(enum.Enum):
    queued = "queued"
    sending = "sending"
    delivered = "delivered"
    failed = "failed"
    canceled = "canceled"


class DeliveryStatus(enum.Enum):
    accepted = "accepted"
    delivered = "delivered"
    failed = "failed"
    bounced = "bounced"
    rejected = "rejected"


class NotificationTemplate(Base):
    __tablename__ = "notification_templates"
    __table_args__ = (
        UniqueConstraint(
            "code", "channel", name="uq_notification_templates_code_channel"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    code: Mapped[str] = mapped_column(String(120), nullable=False)
    channel: Mapped[NotificationChannel] = mapped_column(Enum(NotificationChannel))
    subject: Mapped[str | None] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    conditions: Mapped[dict] = mapped_column(
        MutableDict.as_mutable(JSON()), default=dict, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    notifications = relationship("Notification", back_populates="template")


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("notification_templates.id")
    )
    connector_config_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("connector_configs.id")
    )
    subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscribers.id", ondelete="SET NULL"),
        index=True,
    )
    channel: Mapped[NotificationChannel] = mapped_column(
        Enum(NotificationChannel), nullable=False
    )
    event_type: Mapped[str | None] = mapped_column(String(120), index=True)
    category: Mapped[str | None] = mapped_column(String(40), index=True)
    recipient: Mapped[str] = mapped_column(String(255), nullable=False)
    subject: Mapped[str | None] = mapped_column(String(200))
    body: Mapped[str | None] = mapped_column(Text)
    status: Mapped[NotificationStatus] = mapped_column(
        Enum(NotificationStatus), default=NotificationStatus.queued
    )
    send_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    template = relationship("NotificationTemplate", back_populates="notifications")
    deliveries = relationship("NotificationDelivery", back_populates="notification")
    subscriber = relationship("Subscriber")


class NotificationDelivery(Base):
    __tablename__ = "notification_deliveries"
    __table_args__ = (
        Index(
            "uq_notification_deliveries_provider_message",
            "provider",
            "provider_message_id",
            unique=True,
            postgresql_where=text(
                "is_active AND provider IS NOT NULL AND provider_message_id IS NOT NULL"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    notification_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("notifications.id"), nullable=False
    )
    provider: Mapped[str | None] = mapped_column(String(120))
    provider_message_id: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[DeliveryStatus] = mapped_column(Enum(DeliveryStatus))
    response_code: Mapped[str | None] = mapped_column(String(60))
    response_body: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    notification = relationship("Notification", back_populates="deliveries")


class AlertNotificationPolicy(Base):
    __tablename__ = "alert_notification_policies"
    __table_args__ = (
        UniqueConstraint("name", name="uq_alert_notification_policies_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    channel: Mapped[NotificationChannel] = mapped_column(
        Enum(NotificationChannel), nullable=False
    )
    recipient: Mapped[str] = mapped_column(String(255), nullable=False)
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("notification_templates.id")
    )
    connector_config_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("connector_configs.id")
    )
    rule_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("alert_rules.id")
    )
    device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("network_devices.id")
    )
    interface_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("device_interfaces.id")
    )
    severity_min: Mapped[AlertSeverity] = mapped_column(
        Enum(AlertSeverity), default=AlertSeverity.warning
    )
    status: Mapped[AlertStatus] = mapped_column(
        Enum(AlertStatus), default=AlertStatus.open
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    template = relationship("NotificationTemplate")
    steps = relationship("AlertNotificationPolicyStep", back_populates="policy")
    notifications = relationship("AlertNotificationLog", back_populates="policy")


class AlertNotificationLog(Base):
    __tablename__ = "alert_notification_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    alert_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("alerts.id"), nullable=False
    )
    policy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("alert_notification_policies.id"), nullable=False
    )
    notification_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("notifications.id")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    policy = relationship("AlertNotificationPolicy", back_populates="notifications")
    notification = relationship("Notification")


class OnCallRotation(Base):
    __tablename__ = "on_call_rotations"
    __table_args__ = (UniqueConstraint("name", name="uq_on_call_rotations_name"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    timezone: Mapped[str] = mapped_column(String(60), default="Africa/Lagos")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    members = relationship("OnCallRotationMember", back_populates="rotation")
    policy_steps = relationship(
        "AlertNotificationPolicyStep", back_populates="rotation"
    )


class OnCallRotationMember(Base):
    __tablename__ = "on_call_rotation_members"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    rotation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("on_call_rotations.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    contact: Mapped[str] = mapped_column(String(255), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    rotation = relationship("OnCallRotation", back_populates="members")


class AlertNotificationPolicyStep(Base):
    __tablename__ = "alert_notification_policy_steps"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    policy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("alert_notification_policies.id"), nullable=False
    )
    step_index: Mapped[int] = mapped_column(Integer, default=0)
    delay_minutes: Mapped[int] = mapped_column(Integer, default=0)
    channel: Mapped[NotificationChannel] = mapped_column(
        Enum(NotificationChannel), nullable=False
    )
    recipient: Mapped[str | None] = mapped_column(String(255))
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("notification_templates.id")
    )
    connector_config_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("connector_configs.id")
    )
    rotation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("on_call_rotations.id")
    )
    severity_min: Mapped[AlertSeverity] = mapped_column(
        Enum(AlertSeverity), default=AlertSeverity.warning
    )
    status: Mapped[AlertStatus] = mapped_column(
        Enum(AlertStatus), default=AlertStatus.open
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    policy = relationship("AlertNotificationPolicy", back_populates="steps")
    template = relationship("NotificationTemplate")
    rotation = relationship("OnCallRotation", back_populates="policy_steps")


class SuppressionScope(enum.Enum):
    """What a suppression blocks.

    The distinction is the whole point. An unsubscribe is a refusal of
    *marketing*; it is not permission to stop sending someone their invoice,
    their outage notice, or their credentials. Conflating the two is how a
    consent ledger turns into a billing incident.

    ``marketing`` — blocks marketing only. This is what "unsubscribe" means.
    ``all``       — blocks everything, transactional included. Reserved for
                    addresses we must not send to at all: hard bounces, spam
                    complaints, and legal erasure requests. Never set by a
                    customer clicking unsubscribe.
    """

    marketing = "marketing"
    all = "all"


class SuppressionReason(enum.Enum):
    unsubscribe = "unsubscribe"
    bounce = "bounce"
    complaint = "complaint"
    manual = "manual"
    erasure = "erasure"


class CommunicationSuppression(Base):
    """Platform-wide 'do not contact' ledger — the single source of truth for
    whether we may send to an address on a channel.

    Campaigns, transactional notifications, imports and every other sender ask
    the same question of the same table (``communication_eligibility.may_send``).
    Before this, marketing eligibility was decided inside the campaign segment
    filter, which meant a customer could unsubscribe and still be reachable by
    any other path -- and that the answer differed depending on who was asking.
    """

    __tablename__ = "communication_suppressions"
    __table_args__ = (
        UniqueConstraint(
            "channel", "address", name="uq_communication_suppressions_channel_address"
        ),
        Index("ix_communication_suppressions_address", "address"),
        Index("ix_communication_suppressions_subscriber_id", "subscriber_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    channel: Mapped[NotificationChannel] = mapped_column(
        Enum(NotificationChannel, native_enum=False), nullable=False, index=True
    )
    #: Normalised recipient (lower-cased email, digits-only phone). The raw form
    #: is kept in ``raw_address`` so a suppression is auditable back to what the
    #: customer actually clicked.
    address: Mapped[str] = mapped_column(String(320), nullable=False)
    raw_address: Mapped[str | None] = mapped_column(String(320))
    #: Best-effort link. An address is not always resolvable to a subscriber
    #: (imports, forwarded mail), and the ledger is keyed on the ADDRESS -- the
    #: thing the transport actually sends to -- not on the person.
    subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), index=True
    )
    scope: Mapped[SuppressionScope] = mapped_column(
        Enum(SuppressionScope, native_enum=False),
        nullable=False,
        default=SuppressionScope.marketing,
    )
    reason: Mapped[SuppressionReason] = mapped_column(
        Enum(SuppressionReason, native_enum=False),
        nullable=False,
        default=SuppressionReason.unsubscribe,
    )
    #: Free-text provenance: the bounce code, the campaign that carried the
    #: unsubscribe link, the operator who set it by hand.
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    created_by: Mapped[str | None] = mapped_column(String(120))
