import uuid
from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

FIELD_CHAT_DIRECTIONS = ("staff", "customer")


class FieldJobChatMessage(Base):
    """Job-scoped technician/customer chat message on a work-order mirror.

    Minimal sub-native store: sub has no CRM inbox/conversation stack (Phase 4),
    so field chat persists directly against the work-order mirror instead of a
    conversation engine. ``direction`` is ``staff`` for technician-authored
    messages and ``customer`` for future customer-portal replies.
    """

    __tablename__ = "field_job_chat_messages"
    __table_args__ = (
        Index(
            "ix_field_job_chat_messages_mirror_created",
            "work_order_mirror_id",
            "created_at",
        ),
        Index("ix_field_job_chat_messages_crm_work_order_id", "crm_work_order_id"),
        CheckConstraint(
            "direction IN ('staff', 'customer')",
            name="ck_field_job_chat_messages_direction",
        ),
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
    direction: Mapped[str] = mapped_column(String(20), default="staff", nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    author_technician_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("technician_profiles.id")
    )
    author_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    author_system_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("system_users.id")
    )
    author_name: Mapped[str | None] = mapped_column(String(160))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    work_order_mirror = relationship("WorkOrderMirror")
    author_technician = relationship("TechnicianProfile")
    author_system_user = relationship("SystemUser")
