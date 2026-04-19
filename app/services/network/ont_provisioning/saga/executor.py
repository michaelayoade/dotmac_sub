"""Saga executor for ONT provisioning with compensation-based rollback.

This module provides the SagaExecutor class that orchestrates saga execution:
- Executes steps in order
- Tracks compensation actions for completed steps
- On critical failure, rolls back in reverse order
- Persists execution history for observability
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.services.network.ont_provisioning.result import StepResult
from app.services.network.ont_provisioning.saga.types import (
    CompensationRecord,
    SagaContext,
    SagaDefinition,
    SagaExecutionStatus,
    SagaResult,
    SagaStep,
    StepExecutionRecord,
    generate_saga_execution_id,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class SagaExecutor:
    """Executes a saga with automatic compensation on failure.

    The executor runs each step in order, tracking which steps have completed
    and their compensation actions. On critical failure, it reverses through
    completed steps and executes their compensations.

    Usage:
        context = SagaContext(db=db, ont_id=ont_id, saga_execution_id=uuid4())
        executor = SagaExecutor(saga_definition, context)
        result = executor.execute()

        if not result.success:
            logger.error("Saga failed: %s", result.message)
            for step_name, error in result.compensation_failures:
                logger.error("Manual cleanup needed: %s - %s", step_name, error)
    """

    def __init__(self, saga: SagaDefinition, context: SagaContext):
        """Initialize the executor.

        Args:
            saga: The saga definition to execute.
            context: Execution context with db session, ont_id, etc.
        """
        self.saga = saga
        self.context = context
        self._completed_steps: list[tuple[SagaStep, StepResult]] = []
        self._step_records: list[StepExecutionRecord] = []
        self._start_time: datetime | None = None

    def execute(self) -> SagaResult:
        """Execute the saga with compensation on failure.

        Returns:
            SagaResult with execution outcome and compensation history.
        """
        self._start_time = datetime.now(UTC)
        result = SagaResult(
            saga_name=self.saga.name,
            saga_execution_id=self.context.saga_execution_id,
            success=False,
            message="Execution started",
            status=SagaExecutionStatus.running,
            started_at=self._start_time,
        )

        logger.info(
            "Starting saga execution: %s (execution_id=%s, ont_id=%s)",
            self.saga.name,
            self.context.saga_execution_id,
            self.context.ont_id,
            extra={
                "event": "saga_execution_start",
                "saga_name": self.saga.name,
                "saga_execution_id": self.context.saga_execution_id,
                "ont_id": self.context.ont_id,
                "step_count": len(self.saga.steps),
            },
        )

        try:
            # Load ONT and OLT into context if not already loaded
            if not self._load_context_models():
                return self._build_failure_result(
                    result,
                    "Failed to load ONT or OLT",
                    failed_step="context_load",
                )

            # Execute each step in order
            for step in self.saga.steps:
                step_result = self._execute_step(step)
                record = StepExecutionRecord.from_step_result(step_result)
                self._step_records.append(record)

                if not step_result.success and not step_result.skipped:
                    if step.critical:
                        logger.warning(
                            "Critical step failed, initiating rollback: %s",
                            step.name,
                            extra={
                                "event": "saga_critical_failure",
                                "saga_name": self.saga.name,
                                "saga_execution_id": self.context.saga_execution_id,
                                "failed_step": step.name,
                                "step_message": step_result.message,
                            },
                        )
                        return self._rollback_and_fail(result, step, step_result)
                    else:
                        # Non-critical failure - log warning and continue
                        logger.warning(
                            "Non-critical step failed, continuing: %s - %s",
                            step.name,
                            step_result.message,
                            extra={
                                "event": "saga_noncritical_failure",
                                "saga_name": self.saga.name,
                                "step": step.name,
                            },
                        )

                # Track completed step for potential compensation
                if step_result.success and step.compensate:
                    self._completed_steps.append((step, step_result))

            # All steps completed successfully
            return self._build_success_result(result)

        except Exception as exc:
            logger.error(
                "Saga execution error: %s",
                exc,
                exc_info=True,
                extra={
                    "event": "saga_execution_error",
                    "saga_name": self.saga.name,
                    "saga_execution_id": self.context.saga_execution_id,
                },
            )
            # Create a synthetic failed step result for the exception
            error_result = StepResult(
                step_name="saga_execution",
                success=False,
                message=f"Unexpected error: {exc}",
                critical=True,
            )
            error_step = SagaStep(
                name="saga_execution",
                action=lambda ctx: error_result,
                critical=True,
            )
            return self._rollback_and_fail(result, error_step, error_result)

    def _load_context_models(self) -> bool:
        """Load ONT and OLT models into context.

        Returns:
            True if models loaded successfully, False otherwise.
        """
        from app.models.network import OLTDevice, OntUnit

        if self.context.ont is None:
            ont = self.context.db.get(OntUnit, self.context.ont_id)
            if ont is None:
                logger.error(
                    "ONT not found: %s",
                    self.context.ont_id,
                    extra={"event": "saga_ont_not_found"},
                )
                return False
            self.context.ont = ont

        if self.context.olt is None and self.context.ont is not None:
            olt = self.context.db.get(OLTDevice, self.context.ont.olt_device_id)
            if olt is None:
                logger.error(
                    "OLT not found for ONT: %s",
                    self.context.ont_id,
                    extra={"event": "saga_olt_not_found"},
                )
                return False
            self.context.olt = olt

        return True

    def _execute_step(self, step: SagaStep) -> StepResult:
        """Execute a single step with timing and error handling.

        Args:
            step: The step to execute.

        Returns:
            StepResult from the step action.
        """
        start_time = time.monotonic()

        logger.debug(
            "Executing saga step: %s",
            step.name,
            extra={
                "event": "saga_step_start",
                "saga_name": self.saga.name,
                "step": step.name,
            },
        )

        try:
            result = step.action(self.context)
            # Ensure duration is set
            elapsed_ms = int((time.monotonic() - start_time) * 1000)
            if result.duration_ms == 0:
                result.duration_ms = elapsed_ms

            logger.info(
                "Saga step completed: %s - %s (%dms)",
                step.name,
                "success" if result.success else "failed",
                result.duration_ms,
                extra={
                    "event": "saga_step_complete",
                    "saga_name": self.saga.name,
                    "step": step.name,
                    "success": result.success,
                    "duration_ms": result.duration_ms,
                },
            )

            return result

        except Exception as exc:
            elapsed_ms = int((time.monotonic() - start_time) * 1000)
            logger.error(
                "Saga step error: %s - %s",
                step.name,
                exc,
                exc_info=True,
                extra={
                    "event": "saga_step_error",
                    "saga_name": self.saga.name,
                    "step": step.name,
                },
            )
            return StepResult(
                step_name=step.name,
                success=False,
                message=f"Step error: {exc}",
                duration_ms=elapsed_ms,
                critical=step.critical,
            )

    def _rollback_and_fail(
        self,
        result: SagaResult,
        failed_step: SagaStep,
        step_result: StepResult,
    ) -> SagaResult:
        """Execute compensation in reverse order and return failure result.

        Args:
            result: The SagaResult to populate.
            failed_step: The step that failed.
            step_result: The result from the failed step.

        Returns:
            SagaResult with failure status and compensation records.
        """
        result.status = SagaExecutionStatus.compensating
        result.failed_step = failed_step.name
        compensation_records: list[CompensationRecord] = []
        compensation_failures: list[tuple[str, str]] = []

        logger.info(
            "Starting compensation for %d completed steps",
            len(self._completed_steps),
            extra={
                "event": "saga_compensation_start",
                "saga_name": self.saga.name,
                "saga_execution_id": self.context.saga_execution_id,
                "steps_to_compensate": len(self._completed_steps),
            },
        )

        # Execute compensation in reverse order
        for step, original_result in reversed(self._completed_steps):
            if step.compensate is None:
                continue

            start_time = time.monotonic()
            try:
                comp_result = step.compensate(self.context, original_result)
                elapsed_ms = int((time.monotonic() - start_time) * 1000)

                record = CompensationRecord(
                    step_name=step.name,
                    success=comp_result.success,
                    message=comp_result.message,
                    duration_ms=elapsed_ms,
                )
                compensation_records.append(record)

                if comp_result.success:
                    logger.info(
                        "Compensation succeeded: %s",
                        step.name,
                        extra={
                            "event": "saga_compensation_success",
                            "saga_name": self.saga.name,
                            "step": step.name,
                        },
                    )
                else:
                    logger.error(
                        "Compensation failed: %s - %s",
                        step.name,
                        comp_result.message,
                        extra={
                            "event": "saga_compensation_failed",
                            "saga_name": self.saga.name,
                            "step": step.name,
                        },
                    )
                    record.error = comp_result.message
                    compensation_failures.append((step.name, comp_result.message))

            except Exception as exc:
                elapsed_ms = int((time.monotonic() - start_time) * 1000)
                error_msg = f"Compensation error: {exc}"
                logger.error(
                    "Compensation exception: %s - %s",
                    step.name,
                    exc,
                    exc_info=True,
                    extra={
                        "event": "saga_compensation_error",
                        "saga_name": self.saga.name,
                        "step": step.name,
                    },
                )
                record = CompensationRecord(
                    step_name=step.name,
                    success=False,
                    message=error_msg,
                    duration_ms=elapsed_ms,
                    error=str(exc),
                )
                compensation_records.append(record)
                compensation_failures.append((step.name, str(exc)))

        # Alert operators if there are compensation failures
        if compensation_failures:
            self._alert_compensation_failures(failed_step.name, compensation_failures)

        return self._build_failure_result(
            result,
            step_result.message,
            failed_step=failed_step.name,
            compensation_records=compensation_records,
            compensation_failures=compensation_failures,
        )

    def _alert_compensation_failures(
        self,
        failed_step: str,
        failures: list[tuple[str, str]],
    ) -> None:
        """Alert operators about compensation failures requiring manual cleanup.

        Args:
            failed_step: Name of the step that triggered rollback.
            failures: List of (step_name, error) tuples.
        """
        try:
            from app.services.notification_adapter import notify

            step_names = [name for name, _ in failures]
            notify.alert_operators(
                title="Saga Compensation Failed",
                message=(
                    f"ONT provisioning saga '{self.saga.name}' failed at step "
                    f"'{failed_step}' and {len(failures)} compensation(s) also failed. "
                    f"Manual cleanup required for: {', '.join(step_names)}"
                ),
                severity="critical",
                metadata={
                    "saga_name": self.saga.name,
                    "saga_execution_id": self.context.saga_execution_id,
                    "ont_id": self.context.ont_id,
                    "failed_step": failed_step,
                    "compensation_failures": step_names,
                },
            )
        except Exception as exc:
            logger.error(
                "Failed to send compensation failure alert: %s",
                exc,
                extra={"event": "saga_alert_failed"},
            )

    def _build_success_result(self, result: SagaResult) -> SagaResult:
        """Build the final result for successful execution.

        Args:
            result: The SagaResult to populate.

        Returns:
            Populated SagaResult.
        """
        result.success = True
        result.status = SagaExecutionStatus.succeeded
        result.message = f"Saga '{self.saga.name}' completed successfully"
        result.steps_executed = self._step_records.copy()
        result.completed_at = datetime.now(UTC)
        result.duration_ms = self._calculate_duration()

        # Call success callback if defined
        if self.saga.on_success:
            try:
                self.saga.on_success(self.context, result)
            except Exception as exc:
                logger.error(
                    "Saga success callback failed: %s",
                    exc,
                    extra={"event": "saga_callback_error"},
                )

        logger.info(
            "Saga completed successfully: %s (%dms)",
            self.saga.name,
            result.duration_ms,
            extra={
                "event": "saga_execution_success",
                "saga_name": self.saga.name,
                "saga_execution_id": self.context.saga_execution_id,
                "duration_ms": result.duration_ms,
                "steps_executed": len(result.steps_executed),
            },
        )

        return result

    def _build_failure_result(
        self,
        result: SagaResult,
        message: str,
        *,
        failed_step: str | None = None,
        compensation_records: list[CompensationRecord] | None = None,
        compensation_failures: list[tuple[str, str]] | None = None,
    ) -> SagaResult:
        """Build the final result for failed execution.

        Args:
            result: The SagaResult to populate.
            message: Failure message.
            failed_step: Name of the failed step.
            compensation_records: Records of compensation attempts.
            compensation_failures: List of failed compensations.

        Returns:
            Populated SagaResult.
        """
        result.success = False
        result.message = message
        result.failed_step = failed_step
        result.steps_executed = self._step_records.copy()
        result.steps_compensated = compensation_records or []
        result.compensation_failures = compensation_failures or []
        result.completed_at = datetime.now(UTC)
        result.duration_ms = self._calculate_duration()

        if result.compensation_failures:
            result.status = SagaExecutionStatus.compensation_failed
        else:
            result.status = SagaExecutionStatus.failed

        # Call failure callback if defined
        if self.saga.on_failure:
            try:
                self.saga.on_failure(self.context, result)
            except Exception as exc:
                logger.error(
                    "Saga failure callback failed: %s",
                    exc,
                    extra={"event": "saga_callback_error"},
                )

        logger.warning(
            "Saga failed: %s - %s (%dms)",
            self.saga.name,
            message,
            result.duration_ms,
            extra={
                "event": "saga_execution_failed",
                "saga_name": self.saga.name,
                "saga_execution_id": self.context.saga_execution_id,
                "failed_step": failed_step,
                "duration_ms": result.duration_ms,
                "compensation_failures": len(result.compensation_failures),
            },
        )

        return result

    def _calculate_duration(self) -> int:
        """Calculate total execution duration in milliseconds."""
        if self._start_time is None:
            return 0
        delta = datetime.now(UTC) - self._start_time
        return int(delta.total_seconds() * 1000)


def execute_saga(
    db: Session,
    saga: SagaDefinition,
    ont_id: str,
    *,
    step_data: dict[str, Any] | None = None,
    dry_run: bool = False,
    initiated_by: str | None = None,
) -> SagaResult:
    """Execute a saga for the given ONT.

    This is the main entry point for saga execution. It creates the context,
    executor, and runs the saga.

    Args:
        db: Database session.
        saga: The saga definition to execute.
        ont_id: UUID of the target ONT.
        step_data: Optional initial data for steps.
        dry_run: If True, steps should not make real changes.
        initiated_by: User or system identifier.

    Returns:
        SagaResult with execution outcome.
    """
    context = SagaContext(
        db=db,
        ont_id=ont_id,
        saga_execution_id=generate_saga_execution_id(),
        step_data=step_data or {},
        dry_run=dry_run,
        initiated_by=initiated_by,
    )

    executor = SagaExecutor(saga, context)
    return executor.execute()
