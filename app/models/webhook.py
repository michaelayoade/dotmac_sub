import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class WebhookEventType(enum.Enum):
    """Event types for webhook subscriptions (~40 types)."""

    # Subscriber events (4)
    subscriber_created = "subscriber.created"
    subscriber_updated = "subscriber.updated"
    subscriber_suspended = "subscriber.suspended"
    subscriber_reactivated = "subscriber.reactivated"

    # Subscription events (8)
    subscription_created = "subscription.created"
    subscription_activated = "subscription.activated"
    subscription_suspended = "subscription.suspended"
    subscription_resumed = "subscription.resumed"
    subscription_canceled = "subscription.canceled"
    subscription_upgraded = "subscription.upgraded"
    subscription_downgraded = "subscription.downgraded"
    subscription_expiring = "subscription.expiring"

    # Billing - Invoice events (4)
    invoice_created = "invoice.created"
    invoice_sent = "invoice.sent"
    invoice_paid = "invoice.paid"
    invoice_overdue = "invoice.overdue"

    # Billing - Payment events (3)
    payment_received = "payment.received"
    payment_failed = "payment.failed"
    payment_refunded = "payment.refunded"

    # Usage events (4)
    usage_recorded = "usage.recorded"
    usage_warning = "usage.warning"
    usage_exhausted = "usage.exhausted"
    usage_topped_up = "usage.topped_up"

    # Operations - Provisioning events (3)
    provisioning_started = "provisioning.started"
    provisioning_completed = "provisioning.completed"
    provisioning_failed = "provisioning.failed"

    # Operations - Service Order events (3)
    service_order_created = "service_order.created"
    service_order_assigned = "service_order.assigned"
    service_order_completed = "service_order.completed"

    # Operations - Appointment events (2)
    appointment_scheduled = "appointment.scheduled"
    appointment_missed = "appointment.missed"

    # Network events (4)
    device_offline = "device.offline"
    device_online = "device.online"
    session_started = "session.started"
    session_ended = "session.ended"
    network_alert = "network.alert"

    # Custom event type for extensibility
    custom = "custom"


class WebhookDeliveryStatus(enum.Enum):
    pending = "pending"
    delivered = "delivered"
    failed = "failed"


class WebhookEndpoint(Base):
    __tablename__ = "webhook_endpoints"
    __table_args__ = (UniqueConstraint("url", name="uq_webhook_endpoints_url"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    connector_config_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("connector_configs.id")
    )
    secret: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    subscriptions = relationship("WebhookSubscription", back_populates="endpoint")
    connector_config = relationship("ConnectorConfig")


class WebhookSubscription(Base):
    __tablename__ = "webhook_subscriptions"
    __table_args__ = (
        UniqueConstraint(
            "endpoint_id", "event_type", name="uq_webhook_subscriptions_endpoint_event"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    endpoint_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("webhook_endpoints.id"), nullable=False
    )
    event_type: Mapped[WebhookEventType] = mapped_column(
        Enum(WebhookEventType), default=WebhookEventType.custom
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    endpoint = relationship("WebhookEndpoint", back_populates="subscriptions")
    deliveries = relationship("WebhookDelivery", back_populates="subscription")


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("webhook_subscriptions.id"), nullable=False
    )
    endpoint_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("webhook_endpoints.id"), nullable=False
    )
    event_type: Mapped[WebhookEventType] = mapped_column(
        Enum(WebhookEventType), default=WebhookEventType.custom
    )
    status: Mapped[WebhookDeliveryStatus] = mapped_column(
        Enum(WebhookDeliveryStatus), default=WebhookDeliveryStatus.pending
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    response_status: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JSON)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    subscription = relationship("WebhookSubscription", back_populates="deliveries")
    endpoint = relationship("WebhookEndpoint")
