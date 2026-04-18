"""Compensation failure tracking for rollback operations.

Persists failed compensation (rollback) entries for manual resolution.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class CompensationStatus(str, enum.Enum):
    """Status of a compensation failure record."""

    pending = "pending"
    resolved = "resolved"
    abandoned = "abandoned"


class CompensationFailure(Base):
    """Tracks failed compensation (rollback) entries for manual resolution.

    When a provisioning rollback fails to execute one or more undo commands,
    the failure is persisted here for operator review and manual cleanup.
    """

    __tablename__ = "compensation_failures"
    __table_args__ = (
        Index("ix_compensation_failures_ont_unit", "ont_unit_id"),
        Index("ix_compensation_failures_olt_device", "olt_device_id"),
        Index("ix_compensation_failures_status", "status"),
        Index("ix_compensation_failures_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    ont_unit_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ont_units.id"), nullable=True
    )
    olt_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("olt_devices.id"), nullable=True
    )

    # Operation context
    operation_type: Mapped[str] = mapped_column(
        String(64), nullable=False
    )  # "provisioning", "deprovision", "reconciliation"
    step_name: Mapped[str] = mapped_column(String(128), nullable=False)
    undo_commands: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )  # Commands that failed to execute
    description: Mapped[str] = mapped_column(Text, nullable=False)
    resource_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )  # e.g., service-port index
    interface_path: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )  # e.g., "0/2" for GPON interface

    # Failure details
    error_message: Mapped[str] = mapped_column(Text, nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_attempted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

    # Status tracking
    status: Mapped[CompensationStatus] = mapped_column(
        Enum(
            CompensationStatus,
            name="compensationstatus",
            create_constraint=False,
        ),
        nullable=False,
        default=CompensationStatus.pending,
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    resolution_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

    # Relationships
    ont_unit = relationship("OntUnit")
    olt_device = relationship("OLTDevice")
