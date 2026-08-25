import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

FIELD_PRESENCE_STATUSES = ("off_shift", "on_shift", "on_break", "busy")


class FieldTechPresence(Base):
    __tablename__ = "field_tech_presence"
    __table_args__ = (
        Index("ix_field_tech_presence_technician_id", "technician_id", unique=True),
        Index("ix_field_tech_presence_person_id", "person_id"),
        Index("ix_field_tech_presence_status", "status"),
        Index("ix_field_tech_presence_last_location_at", "last_location_at"),
        CheckConstraint(
            "status IN ('off_shift', 'on_shift', 'on_break', 'busy')",
            name="ck_field_tech_presence_status",
        ),
        CheckConstraint(
            "NOT location_sharing_enabled OR ("
            "collection_purpose IS NOT NULL AND "
            "collection_granted_at IS NOT NULL AND "
            "collection_expires_at IS NOT NULL AND "
            "collection_expires_at > collection_granted_at)",
            name="ck_field_tech_presence_active_collection_grant",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    technician_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("technician_profiles.id"), nullable=False
    )
    person_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="off_shift", nullable=False)
    location_sharing_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    collection_purpose: Mapped[str | None] = mapped_column(String(32))
    collection_granted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    collection_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    last_latitude: Mapped[float | None] = mapped_column(Float)
    last_longitude: Mapped[float | None] = mapped_column(Float)
    last_location_accuracy_m: Mapped[float | None] = mapped_column(Float)
    last_location_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    technician = relationship("TechnicianProfile")


class FieldTechLocationPing(Base):
    __tablename__ = "field_tech_location_pings"
    __table_args__ = (
        Index(
            "ix_field_tech_location_pings_technician_received",
            "technician_id",
            "received_at",
        ),
        Index(
            "ix_field_tech_location_pings_person_received", "person_id", "received_at"
        ),
        Index("ix_field_tech_location_pings_received_at", "received_at"),
        Index("ix_field_tech_location_pings_work_order_id", "work_order_id"),
        Index(
            "ux_field_tech_location_pings_observation_identity",
            "technician_id",
            "source",
            "client_observation_id",
            unique=True,
        ),
        CheckConstraint(
            "latitude >= -90 AND latitude <= 90",
            name="ck_field_tech_location_pings_lat_range",
        ),
        CheckConstraint(
            "longitude >= -180 AND longitude <= 180",
            name="ck_field_tech_location_pings_lng_range",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    technician_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("technician_profiles.id"), nullable=False
    )
    person_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    client_observation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    payload_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    work_order_id: Mapped[str | None] = mapped_column(String(64))
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    accuracy_m: Mapped[float | None] = mapped_column(Float)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    source: Mapped[str] = mapped_column(String(32), default="mobile", nullable=False)

    technician = relationship("TechnicianProfile")
