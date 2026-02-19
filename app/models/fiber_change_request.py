import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, ForeignKey, Index, JSON, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class FiberChangeRequestStatus(enum.Enum):
    pending = "pending"
    applied = "applied"
    rejected = "rejected"


class FiberChangeRequestOperation(enum.Enum):
    create = "create"
    update = "update"
    delete = "delete"


class FiberChangeRequest(Base):
    __tablename__ = "fiber_change_requests"
    __table_args__ = (
        Index("ix_fiber_change_requests_status", "status"),
        Index("ix_fiber_change_requests_asset_type", "asset_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    asset_type: Mapped[str] = mapped_column(String(80), nullable=False)
    asset_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    operation: Mapped[FiberChangeRequestOperation] = mapped_column(
        Enum(FiberChangeRequestOperation), nullable=False
    )
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[FiberChangeRequestStatus] = mapped_column(
        Enum(FiberChangeRequestStatus), default=FiberChangeRequestStatus.pending
    )
    requested_by_person_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id")
    )
    requested_by_vendor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True)
    )
    reviewed_by_person_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id")
    )
    review_notes: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
