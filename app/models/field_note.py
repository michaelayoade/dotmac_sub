import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db import Base


class FieldWorkOrderNote(Base):
    """Native field note attached to a CRM-synced work-order mirror."""

    __tablename__ = "field_work_order_notes"
    __table_args__ = (
        Index(
            "ix_field_work_order_notes_mirror_created",
            "work_order_mirror_id",
            "created_at",
        ),
        Index("ix_field_work_order_notes_crm_work_order_id", "crm_work_order_id"),
        Index("ix_field_work_order_notes_author_technician", "author_technician_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    work_order_mirror_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("work_order_mirror.id", ondelete="CASCADE"),
        nullable=False,
    )
    crm_work_order_id: Mapped[str] = mapped_column(String(64), nullable=False)
    author_technician_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("technician_profiles.id"), nullable=False
    )
    author_person_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    author_system_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("system_users.id")
    )
    author_name: Mapped[str | None] = mapped_column(String(160))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    is_internal: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    attachments: Mapped[list | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    work_order_mirror = relationship("WorkOrderMirror")
    author_technician = relationship("TechnicianProfile")
    author_system_user = relationship("SystemUser")
