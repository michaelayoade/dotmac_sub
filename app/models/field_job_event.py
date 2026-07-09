import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db import Base

FIELD_JOB_EVENTS = (
    "accept",
    "en_route",
    "arrived",
    "start",
    "pause",
    "hold",
    "resume",
    "complete",
    "unable_to_complete",
)


class FieldJobEvent(Base):
    """Native mobile execution fact for a CRM-synced work-order mirror."""

    __tablename__ = "field_job_events"
    __table_args__ = (
        Index(
            "ix_field_job_events_mirror_occurred", "work_order_mirror_id", "occurred_at"
        ),
        Index("ix_field_job_events_crm_work_order_id", "crm_work_order_id"),
        Index(
            "ix_field_job_events_author_occurred", "author_technician_id", "occurred_at"
        ),
        Index("ix_field_job_events_client_event_id", "client_event_id", unique=True),
        CheckConstraint(
            "event IN ('accept', 'en_route', 'arrived', 'start', 'pause', 'hold', "
            "'resume', 'complete', 'unable_to_complete')",
            name="ck_field_job_events_event",
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
    author_technician_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("technician_profiles.id"), nullable=False
    )
    person_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    system_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("system_users.id")
    )
    event: Mapped[str] = mapped_column(String(40), nullable=False)
    previous_status: Mapped[str | None] = mapped_column(String(40))
    new_status: Mapped[str | None] = mapped_column(String(40))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    note: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JSON)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    client_event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )

    work_order_mirror = relationship("WorkOrderMirror")
    author_technician = relationship("TechnicianProfile")
    system_user = relationship("SystemUser")
