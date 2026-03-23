"""Persisted OLT autofind candidates for global unconfigured ONT UI."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

if TYPE_CHECKING:
    from app.models.network import OLTDevice, OntUnit


class OltAutofindCandidate(Base):
    """Cached unconfigured ONT discovered from an OLT autofind scan."""

    __tablename__ = "olt_autofind_candidates"
    __table_args__ = (
        UniqueConstraint(
            "olt_id",
            "fsp",
            "serial_number",
            name="uq_olt_autofind_candidates_olt_fsp_serial",
        ),
        Index("ix_olt_autofind_candidates_active", "is_active"),
        Index("ix_olt_autofind_candidates_olt_active", "olt_id", "is_active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    olt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("olt_devices.id", ondelete="CASCADE"),
        nullable=False,
    )
    ont_unit_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ont_units.id", ondelete="SET NULL"),
        nullable=True,
    )
    fsp: Mapped[str] = mapped_column(String(32), nullable=False)
    serial_number: Mapped[str] = mapped_column(String(120), nullable=False)
    serial_hex: Mapped[str | None] = mapped_column(String(32))
    vendor_id: Mapped[str | None] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(120))
    software_version: Mapped[str | None] = mapped_column(String(160))
    mac: Mapped[str | None] = mapped_column(String(32))
    equipment_sn: Mapped[str | None] = mapped_column(String(120))
    autofind_time: Mapped[str | None] = mapped_column(String(120))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    resolution_reason: Mapped[str | None] = mapped_column(String(64))
    notes: Mapped[str | None] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    olt: Mapped[OLTDevice] = relationship("OLTDevice")
    ont_unit: Mapped[OntUnit | None] = relationship("OntUnit")
