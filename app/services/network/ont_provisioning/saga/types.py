"""Saga pattern types for ONT provisioning.

This module defines the core data structures for the saga pattern:
- SagaStep: A single step with action and optional compensation
- SagaDefinition: A complete saga with ordered steps
- SagaContext: Execution context passed to step functions
- SagaResult: Result of saga execution with compensation history
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.models.network import OLTDevice, OntUnit
    from app.services.network.ont_provisioning.result import StepResult


class SagaExecutionStatus(str, Enum):
    """Status of a saga execution."""

    pending = "pending"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    compensating = "compensating"
    compensation_failed = "compensation_failed"


@dataclass
class SagaContext:
    """Execution context for saga steps.

    Contains all shared state needed by step functions:
    - Database session
    - Target ONT and OLT references
    - Step-to-step data sharing
    - Execution metadata

    Attributes:
        db: Database session for queries and persistence.
        ont_id: UUID of the target ONT unit.
        saga_execution_id: UUID tracking this execution.
        ont: Loaded ONT model (populated during execution).
        olt: Loaded OLT model (populated during execution).
        step_data: Dictionary for steps to share data.
        dry_run: If True, steps should not make real changes.
        initiated_by: User or system that started the saga.
        correlation_key: Optional key linking saga records and provisioning events.
        timeout_seconds: Optional total deadline for the saga execution.
    """

    db: Session
    ont_id: str
    saga_execution_id: str
    ont: OntUnit | None = None
    olt: OLTDevice | None = None
    step_data: dict[str, Any] = field(default_factory=dict)
    dry_run: bool = False
    initiated_by: str | None = None
    correlation_key: str | None = None
    timeout_seconds: float | None = None


@dataclass
class StepExecutionRecord:
    """Record of a single step execution.

    Captures timing, status, and any returned data from a step.

    Attributes:
        step_name: Name of the executed step.
        success: Whether the step succeeded.
        message: Human-readable result message.
        duration_ms: Execution time in milliseconds.
        critical: Whether failure should trigger rollback.
        skipped: Whether the step was skipped.
        data: Additional data returned by the step.
        executed_at: Timestamp of execution.
    """

    step_name: str
    success: bool
    message: str
    duration_ms: int = 0
    critical: bool = True
    skipped: bool = False
    data: dict[str, Any] | None = None
    executed_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def from_step_result(cls, result: StepResult) -> StepExecutionRecord:
        """Create from a StepResult."""
        return cls(
            step_name=result.step_name,
            success=result.success,
            message=result.message,
            duration_ms=result.duration_ms,
            critical=result.critical,
            skipped=result.skipped,
            data=result.data,
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "step_name": self.step_name,
            "success": self.success,
            "message": self.message,
            "duration_ms": self.duration_ms,
            "critical": self.critical,
            "skipped": self.skipped,
            "data": self.data,
            "executed_at": self.executed_at.isoformat(),
        }


# Type aliases for step functions
StepAction = Callable[[SagaContext], "StepResult"]
CompensateAction = Callable[[SagaContext, "StepResult"], "StepResult"]


@dataclass
class SagaStep:
    """A single step in a saga with optional compensation.

    Each step consists of:
    - An action function that performs the work
    - An optional compensate function to undo the work on rollback
    - A critical flag indicating if failure should trigger rollback

    Attributes:
        name: Unique identifier for the step.
        action: Function to execute the step.
        compensate: Optional function to undo the step on rollback.
        critical: If True, failure triggers compensation of prior steps.
        rollback_on_failure: If True and step fails but is non-critical,
            compensate this single step without triggering full rollback.
            Useful for partial cleanup when continuing after failure.
        resumable: If True, a prior durable success can skip re-execution.
        description: Human-readable description of what the step does.
    """

    name: str
    action: StepAction
    compensate: CompensateAction | None = None
    critical: bool = True
    rollback_on_failure: bool = False
    resumable: bool = False
    description: str = ""


@dataclass
class SagaDefinition:
    """Definition of a complete saga workflow.

    A saga is an ordered sequence of steps that are executed in order.
    If a critical step fails, all previously completed steps with
    compensation functions are rolled back in reverse order.

    Attributes:
        name: Unique identifier for the saga type.
        description: Human-readable description.
        steps: Ordered list of steps to execute.
        version: Version string for tracking saga definition changes.
        on_success: Optional callback on successful completion.
        on_failure: Optional callback on failure (after compensation).
        timeout_seconds: Total saga execution deadline in seconds.
    """

    name: str
    description: str
    steps: list[SagaStep]
    version: str = "1.0"
    on_success: Callable[[SagaContext, SagaResult], None] | None = None
    on_failure: Callable[[SagaContext, SagaResult], None] | None = None
    timeout_seconds: float | None = 1800.0


@dataclass
class CompensationRecord:
    """Record of a compensation action execution.

    Attributes:
        step_name: Name of the step being compensated.
        success: Whether compensation succeeded.
        message: Result message.
        duration_ms: Compensation execution time.
        error: Error message if compensation failed.
    """

    step_name: str
    success: bool
    message: str
    duration_ms: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "step_name": self.step_name,
            "success": self.success,
            "message": self.message,
            "duration_ms": self.duration_ms,
            "error": self.error,
        }


@dataclass
class SagaResult:
    """Result of a complete saga execution.

    Contains the outcome of all steps, compensations, and overall status.

    Attributes:
        saga_name: Name of the executed saga.
        saga_execution_id: UUID of this execution.
        success: Whether the saga completed successfully.
        message: Summary message.
        steps_executed: Records of all executed steps.
        steps_compensated: Records of compensation actions.
        compensation_failures: List of (step_name, error) for failed compensations.
        duration_ms: Total execution time.
        status: Final execution status.
        failed_step: Name of the step that caused failure (if any).
        started_at: Execution start time.
        completed_at: Execution end time.
    """

    saga_name: str
    saga_execution_id: str
    success: bool
    message: str
    steps_executed: list[StepExecutionRecord] = field(default_factory=list)
    steps_compensated: list[CompensationRecord] = field(default_factory=list)
    compensation_failures: list[tuple[str, str]] = field(default_factory=list)
    duration_ms: int = 0
    status: SagaExecutionStatus = SagaExecutionStatus.pending
    failed_step: str | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None

    @property
    def all_compensations_succeeded(self) -> bool:
        """True if all compensations executed successfully."""
        return len(self.compensation_failures) == 0

    @property
    def steps_needing_manual_cleanup(self) -> list[str]:
        """List of step names that failed compensation."""
        return [step_name for step_name, _ in self.compensation_failures]

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "saga_name": self.saga_name,
            "saga_execution_id": self.saga_execution_id,
            "success": self.success,
            "message": self.message,
            "status": self.status.value,
            "failed_step": self.failed_step,
            "duration_ms": self.duration_ms,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "steps_executed": [s.to_dict() for s in self.steps_executed],
            "steps_compensated": [c.to_dict() for c in self.steps_compensated],
            "compensation_failures": [
                {"step_name": name, "error": err}
                for name, err in self.compensation_failures
            ],
        }


def generate_saga_execution_id() -> str:
    """Generate a unique saga execution ID."""
    return str(uuid.uuid4())
