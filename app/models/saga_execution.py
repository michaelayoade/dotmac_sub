"""Saga execution tracking model.

Persists saga execution history for observability and debugging.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class SagaExecutionStatus(str, enum.Enum):
    """Status of a saga execution."""

    pending = "pending"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    compensating = "compensating"
    compensation_failed = "compensation_failed"


class ProvisioningStepExecutionStatus(str, enum.Enum):
    """Status of an individual saga step execution."""

    pending = "pending"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    skipped = "skipped"
    compensated = "compensated"


class SagaExecution(Base):
    """Tracks saga execution history for ONT provisioning.

    Each record represents a single execution attempt of a saga,
    capturing input, output, timing, and step-by-step progress.

    Attributes:
        id: Unique execution identifier.
        saga_name: Name of the saga definition.
        saga_version: Version of the saga definition.
        ont_unit_id: Target ONT unit (optional, may not exist yet).
        olt_device_id: Target OLT device (optional).
        status: Current execution status.
        input_data: Input parameters for the saga.
        output_data: Final result data.
        steps_executed: List of step names that were executed.
        steps_compensated: List of step names that were compensated.
        compensation_failures: List of steps where compensation failed.
        failed_step: Name of the step that caused failure.
        error_message: Error message if failed.
        started_at: Execution start timestamp.
        completed_at: Execution end timestamp.
        duration_ms: Total execution time in milliseconds.
        initiated_by: User or system that initiated the saga.
        correlation_key: Optional correlation key for deduplication.
    """

    __tablename__ = "saga_executions"
    __table_args__ = (
        Index("ix_saga_executions_ont_unit", "ont_unit_id"),
        Index("ix_saga_executions_olt_device", "olt_device_id"),
        Index("ix_saga_executions_status", "status"),
        Index("ix_saga_executions_saga_name", "saga_name"),
        Index("ix_saga_executions_started_at", "started_at"),
        Index("ix_saga_executions_correlation_key", "correlation_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # Saga identification
    saga_name: Mapped[str] = mapped_column(String(128), nullable=False)
    saga_version: Mapped[str] = mapped_column(
        String(32), nullable=False, default="1.0"
    )

    # Target references (optional - saga may create these)
    ont_unit_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ont_units.id"), nullable=True
    )
    olt_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("olt_devices.id"), nullable=True
    )

    # Execution status
    status: Mapped[SagaExecutionStatus] = mapped_column(
        Enum(
            SagaExecutionStatus,
            name="sagaexecutionstatus",
            create_constraint=False,
        ),
        nullable=False,
        default=SagaExecutionStatus.pending,
    )

    # Input/Output data
    input_data: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict
    )
    output_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Step tracking
    steps_executed: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    steps_compensated: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    compensation_failures: Mapped[list[dict]] = mapped_column(
        JSON, nullable=False, default=list
    )

    # Failure details
    failed_step: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Timing
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Audit
    initiated_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    correlation_key: Mapped[str | None] = mapped_column(String(256), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

    # Relationships
    ont_unit = relationship("OntUnit", foreign_keys=[ont_unit_id])
    olt_device = relationship("OLTDevice", foreign_keys=[olt_device_id])
    step_executions = relationship(
        "ProvisioningStepExecution",
        back_populates="saga_execution",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return (
            f"<SagaExecution {self.saga_name} "
            f"status={self.status.value} "
            f"id={self.id}>"
        )


class ProvisioningStepExecution(Base):
    """Durable checkpoint for a single saga step attempt."""

    __tablename__ = "provisioning_step_executions"
    __table_args__ = (
        UniqueConstraint(
            "saga_execution_id",
            "step_name",
            name="uq_provisioning_step_execution_per_attempt",
        ),
        Index(
            "ix_provisioning_step_executions_correlation_step",
            "correlation_key",
            "saga_name",
            "step_name",
        ),
        Index(
            "ix_provisioning_step_executions_status",
            "status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    saga_execution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("saga_executions.id", ondelete="CASCADE"),
        nullable=False,
    )
    saga_name: Mapped[str] = mapped_column(String(128), nullable=False)
    correlation_key: Mapped[str | None] = mapped_column(String(256), nullable=True)
    step_name: Mapped[str] = mapped_column(String(128), nullable=False)
    step_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[ProvisioningStepExecutionStatus] = mapped_column(
        Enum(
            ProvisioningStepExecutionStatus,
            name="provisioningstepexecutionstatus",
            create_constraint=False,
        ),
        nullable=False,
        default=ProvisioningStepExecutionStatus.pending,
    )
    result_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    saga_execution = relationship("SagaExecution", back_populates="step_executions")

    def __repr__(self) -> str:
        return (
            f"<ProvisioningStepExecution {self.step_name} "
            f"status={self.status.value} "
            f"execution_id={self.saga_execution_id}>"
        )
