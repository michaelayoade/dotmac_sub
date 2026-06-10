"""Reseller-submitted requests for new service / installations.

A reseller asks the ISP to provision service — for one of their existing
customers or a brand-new lead — with the install location pinned on the map.
The ISP works the queue (review → schedule → complete/reject); the reseller
is notified on every status change.
"""

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, Enum, Float, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class ServiceRequestStatus(enum.Enum):
    new = "new"
    reviewing = "reviewing"
    scheduled = "scheduled"
    completed = "completed"
    rejected = "rejected"


class Serviceability(enum.Enum):
    unknown = "unknown"
    serviceable = "serviceable"
    not_serviceable = "not_serviceable"


class ResellerServiceRequest(Base):
    __tablename__ = "reseller_service_requests"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    reseller_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("resellers.id"), nullable=False, index=True
    )
    # Existing customer (nullable: a brand-new lead has no subscriber yet).
    subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=True
    )
    contact_name: Mapped[str | None] = mapped_column(String(160))
    contact_phone: Mapped[str | None] = mapped_column(String(40))
    contact_email: Mapped[str | None] = mapped_column(String(255))
    address: Mapped[str | None] = mapped_column(Text)
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    # Auto-flagged at submission from fiber-plant proximity; staff confirm.
    serviceability: Mapped[Serviceability] = mapped_column(
        Enum(Serviceability), default=Serviceability.unknown, nullable=False
    )
    status: Mapped[ServiceRequestStatus] = mapped_column(
        Enum(ServiceRequestStatus),
        default=ServiceRequestStatus.new,
        nullable=False,
        index=True,
    )
    notes: Mapped[str | None] = mapped_column(Text)
    admin_notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    reseller = relationship("Reseller")
    subscriber = relationship("Subscriber")
