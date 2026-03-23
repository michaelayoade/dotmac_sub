"""RADIUS authentication error tracking."""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
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


class RadiusAuthErrorType(enum.Enum):
    reject = "reject"
    timeout = "timeout"
    invalid_credentials = "invalid_credentials"
    disabled_account = "disabled_account"
    expired_account = "expired_account"
    nas_mismatch = "nas_mismatch"
    policy_violation = "policy_violation"
    other = "other"


class RadiusAuthError(Base):
    """Tracks RADIUS authentication failures.

    Equivalent to Splynx's error_session table (323K rows).
    One row per failed auth attempt for troubleshooting and
    error rate monitoring.
    """

    __tablename__ = "radius_auth_errors"
    __table_args__ = (
        Index("ix_radius_auth_errors_username", "username"),
        Index("ix_radius_auth_errors_occurred_at", "occurred_at"),
        Index("ix_radius_auth_errors_nas", "nas_device_id"),
        Index("ix_radius_auth_errors_subscriber", "subscriber_id"),
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
    nas_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nas_devices.id"), nullable=True
    )

    username: Mapped[str] = mapped_column(String(120), nullable=False)
    nas_ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    calling_station_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_type: Mapped[RadiusAuthErrorType] = mapped_column(
        Enum(
            RadiusAuthErrorType,
            name="radiusautherrortype",
            create_constraint=False,
        ),
        nullable=False,
        default=RadiusAuthErrorType.reject,
    )
    reply_message: Mapped[str | None] = mapped_column(String(255), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

    subscriber = relationship("Subscriber")
    subscription = relationship("Subscription")
    nas_device = relationship("NasDevice")
