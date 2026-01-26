import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class BandwidthSample(Base):
    __tablename__ = "bandwidth_samples"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscriptions.id"), nullable=False
    )
    device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("network_devices.id")
    )
    interface_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("device_interfaces.id")
    )
    rx_bps: Mapped[int] = mapped_column(Integer, default=0)
    tx_bps: Mapped[int] = mapped_column(Integer, default=0)
    sample_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    subscription = relationship("Subscription", back_populates="bandwidth_samples")
    device = relationship("NetworkDevice")
    interface = relationship("DeviceInterface")


class QueueMapping(Base):
    """
    Maps MikroTik queue names to subscriptions for bandwidth tracking.

    When the poller reads queue stats from a NAS device, it uses this mapping
    to associate the queue name with the correct subscription.
    """
    __tablename__ = "queue_mappings"
    __table_args__ = (
        UniqueConstraint("nas_device_id", "queue_name", name="uq_queue_mappings_device_queue"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    nas_device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nas_devices.id"), nullable=False
    )
    queue_name: Mapped[str] = mapped_column(String(255), nullable=False)
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscriptions.id"), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc)
    )

    nas_device = relationship("NasDevice")
    subscription = relationship("Subscription")
