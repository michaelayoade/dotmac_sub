import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class FieldWorkLog(Base):
    """Native technician worklog attached to a CRM-synced work-order mirror."""

    __tablename__ = "field_worklogs"
    __table_args__ = (
        CheckConstraint(
            "(author_vendor_user_id IS NULL AND author_technician_id IS NOT NULL AND person_id IS NOT NULL) OR (author_vendor_user_id IS NOT NULL AND author_technician_id IS NULL AND person_id IS NULL AND system_user_id IS NOT NULL)",
            name="ck_field_worklogs_actor",
        ),
        Index("ix_field_worklogs_mirror_start", "work_order_mirror_id", "start_at"),
        Index("ix_field_worklogs_author_start", "author_technician_id", "start_at"),
        Index("ix_field_worklogs_client_ref", "client_ref", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    work_order_mirror_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("work_order.id", ondelete="CASCADE"),
        nullable=False,
    )
    author_vendor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("field_vendor_users.id")
    )
    author_technician_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("technician_profiles.id"), nullable=True
    )
    person_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    system_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("system_users.id")
    )
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    client_ref: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    work_order_mirror = relationship("WorkOrder")
    author_technician = relationship("TechnicianProfile")
    system_user = relationship("SystemUser")
