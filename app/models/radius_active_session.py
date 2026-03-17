"""Live RADIUS session tracking — who is online right now."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class RadiusActiveSession(Base):
    """Tracks currently active RADIUS sessions.

    Rows are inserted on Acct-Start, updated on Acct-Interim-Update,
    and deleted on Acct-Stop.  This provides a real-time view of
    connected subscribers for operational dashboards and CoA targeting.
    """

    __tablename__ = "radius_active_sessions"
    __table_args__ = (
        UniqueConstraint(
            "acct_session_id",
            "nas_device_id",
            name="uq_radius_active_session",
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
    access_credential_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("access_credentials.id"), nullable=True
    )
    nas_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nas_devices.id"), nullable=True
    )

    # RADIUS attributes
    username: Mapped[str] = mapped_column(String(120), nullable=False)
    acct_session_id: Mapped[str] = mapped_column(String(120), nullable=False)
    nas_ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    framed_ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    framed_ipv6_prefix: Mapped[str | None] = mapped_column(String(128), nullable=True)
    calling_station_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    nas_port_id: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # Session counters
    session_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    session_time: Mapped[int] = mapped_column(Integer, default=0)
    bytes_in: Mapped[int] = mapped_column(BigInteger, default=0)
    bytes_out: Mapped[int] = mapped_column(BigInteger, default=0)
    packets_in: Mapped[int] = mapped_column(BigInteger, default=0)
    packets_out: Mapped[int] = mapped_column(BigInteger, default=0)

    last_update: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

    subscriber = relationship("Subscriber")
    subscription = relationship("Subscription")
    access_credential = relationship("AccessCredential")
    nas_device = relationship("NasDevice")
