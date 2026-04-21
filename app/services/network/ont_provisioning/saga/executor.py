"""Saga executor for ONT provisioning with compensation-based rollback.

This module provides the SagaExecutor class that orchestrates saga execution:
- Executes steps in order
- Tracks compensation actions for completed steps
- On critical failure, rolls back in reverse order
- Persists execution history for observability
"""

from __future__ import annotations

import logging
import signal
import threading
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

_SERVICE_PORT_ROLLBACK_STEPS = {
    "create_service_ports",
    "provision_with_reconciliation",
}


class SagaTimeoutError(TimeoutError):
    """Raised when a saga exceeds its configured deadline."""


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
        self._resumable_steps: dict[str, Any] = {}
        self._start_time: datetime | None = None
        self._deadline_monotonic: float | None = None

    @property
    def timeout_seconds(self) -> float | None:
        if self.context.timeout_seconds is not None:
            return self.context.timeout_seconds
        return self.saga.timeout_seconds

    def execute(self) -> SagaResult:
        """Execute the saga with compensation on failure.

        Returns:
            SagaResult with execution outcome and compensation history.
        """
        self._start_time = datetime.now(UTC)
        if self.timeout_seconds is not None:
            self._deadline_monotonic = time.monotonic() + max(
                0.0, float(self.timeout_seconds)
            )
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

            self._resumable_steps = self._load_resumable_steps()

            # Execute each step in order
            for index, step in enumerate(self.saga.steps):
                timeout_result = self._timeout_result_if_expired(step.name)
                if timeout_result is not None:
                    self._step_records.append(
                        StepExecutionRecord.from_step_result(timeout_result)
                    )
                    self._persist_step_result(index, step, self._step_records[-1])
                    return self._rollback_and_fail(result, step, timeout_result)

                resumed_result = self._build_resumed_step_result(step)
                if resumed_result is not None:
                    self._record_step_event(step.name, resumed_result)
                    record = StepExecutionRecord.from_step_result(resumed_result)
                    self._step_records.append(record)
                    self._persist_step_result(index, step, record)
                    continue

                self._mark_step_running(index, step)
                step_result = self._execute_step(step)
                self._record_step_event(step.name, step_result)
                record = StepExecutionRecord.from_step_result(step_result)
                self._step_records.append(record)
                self._persist_step_result(index, step, record)

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

    def _load_resumable_steps(self) -> dict[str, Any]:
        if not self.context.correlation_key:
            return {}
        try:
            from app.services.network.ont_provisioning.saga.persistence import (
                saga_step_executions,
            )

            return saga_step_executions.get_resumable_steps(
                self.context.db,
                correlation_key=self.context.correlation_key,
                saga_name=self.saga.name,
            )
        except Exception:
            logger.warning(
                "Failed to load resumable saga checkpoints",
                exc_info=True,
                extra={
                    "event": "saga_resume_load_failed",
                    "saga_name": self.saga.name,
                    "saga_execution_id": self.context.saga_execution_id,
                },
            )
            return {}

    def _build_resumed_step_result(self, step: SagaStep) -> StepResult | None:
        if not step.resumable:
            return None
        checkpoint = self._resumable_steps.get(step.name)
        if checkpoint is None:
            return None
        checkpoint_data = dict(checkpoint.result_data or {})
        step_data = checkpoint_data.get("data")
        if isinstance(step_data, dict):
            self.context.step_data.setdefault("resumed_steps", {})[step.name] = step_data
        return StepResult(
            step_name=step.name,
            success=True,
            skipped=True,
            critical=step.critical,
            duration_ms=0,
            message=f"Resumed from checkpoint created by {checkpoint.saga_execution_id}",
            data=step_data if isinstance(step_data, dict) else None,
        )

    def _mark_step_running(self, step_order: int, step: SagaStep) -> None:
        try:
            from app.services.network.ont_provisioning.saga.persistence import (
                saga_step_executions,
            )

            saga_step_executions.mark_running(
                self.context.db,
                execution_id=self.context.saga_execution_id,
                saga_name=self.saga.name,
                correlation_key=self.context.correlation_key,
                step_name=step.name,
                step_order=step_order,
            )
        except Exception:
            logger.warning(
                "Failed to persist running saga step: %s",
                step.name,
                exc_info=True,
            )

    def _persist_step_result(
        self,
        step_order: int,
        step: SagaStep,
        record: StepExecutionRecord,
    ) -> None:
        try:
            from app.services.network.ont_provisioning.saga.persistence import (
                saga_step_executions,
            )

            saga_step_executions.mark_completed(
                self.context.db,
                execution_id=self.context.saga_execution_id,
                saga_name=self.saga.name,
                correlation_key=self.context.correlation_key,
                step_name=step.name,
                step_order=step_order,
                record=record,
            )
        except Exception:
            logger.warning(
                "Failed to persist saga step result: %s",
                step.name,
                exc_info=True,
            )

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
            result = self._run_step_with_timeout(step)
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

        except SagaTimeoutError as exc:
            elapsed_ms = int((time.monotonic() - start_time) * 1000)
            logger.error(
                "Saga step timed out: %s - %s",
                step.name,
                exc,
                extra={
                    "event": "saga_step_timeout",
                    "saga_name": self.saga.name,
                    "step": step.name,
                },
            )
            return StepResult(
                step_name=step.name,
                success=False,
                message=str(exc),
                duration_ms=elapsed_ms,
                critical=True,
            )
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

    def _record_step_event(self, step_name: str, result: StepResult) -> None:
        """Best-effort append-only audit event for saga step completion."""
        if self.context.ont is None:
            return
        try:
            from app.services.network.provisioning_events import (
                record_ont_provisioning_event,
            )

            record_ont_provisioning_event(
                self.context.db,
                self.context.ont,
                step_name,
                result,
                event_data={
                    "saga_name": self.saga.name,
                    "saga_execution_id": self.context.saga_execution_id,
                },
                correlation_key=self.context.correlation_key,
            )
            self.context.db.flush()
        except Exception:
            logger.warning(
                "Failed to record saga step provisioning event: %s",
                step_name,
                exc_info=True,
            )

    def _remaining_timeout_seconds(self) -> float | None:
        if self._deadline_monotonic is None:
            return None
        return self._deadline_monotonic - time.monotonic()

    def _timeout_result_if_expired(self, step_name: str) -> StepResult | None:
        remaining = self._remaining_timeout_seconds()
        if remaining is None or remaining > 0:
            return None
        return StepResult(
            step_name=step_name,
            success=False,
            message=(
                f"Saga '{self.saga.name}' timed out after "
                f"{self.timeout_seconds:g} seconds"
            ),
            critical=True,
        )

    def _run_step_with_timeout(self, step: SagaStep) -> StepResult:
        remaining = self._remaining_timeout_seconds()
        if remaining is None:
            return step.action(self.context)
        if remaining <= 0:
            raise SagaTimeoutError(
                f"Saga '{self.saga.name}' timed out before step '{step.name}'"
            )
        if threading.current_thread() is not threading.main_thread():
            return step.action(self.context)

        def _handle_timeout(_signum: int, _frame: object) -> None:
            raise SagaTimeoutError(
                f"Saga '{self.saga.name}' timed out while executing step '{step.name}'"
            )

        previous_handler = signal.getsignal(signal.SIGALRM)
        previous_timer = signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, _handle_timeout)
        signal.setitimer(signal.ITIMER_REAL, max(0.001, remaining))
        try:
            return step.action(self.context)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)
            if previous_timer[0] > 0:
                signal.setitimer(signal.ITIMER_REAL, previous_timer[0], previous_timer[1])

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
        persisted_failure_specs: list[tuple[str, StepResult, str]] = []

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
                    self._mark_step_compensated(step, record)
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
                    self._mark_step_compensated(step, record)
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
                    persisted_failure_specs.append(
                        (step.name, original_result, comp_result.message)
                    )

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
                persisted_failure_specs.append((step.name, original_result, str(exc)))
                self._mark_step_compensated(step, record)

        # Alert operators if there are compensation failures
        if compensation_failures:
            self._persist_compensation_failures(persisted_failure_specs)
            self._alert_compensation_failures(failed_step.name, compensation_failures)

        return self._build_failure_result(
            result,
            step_result.message,
            failed_step=failed_step.name,
            compensation_records=compensation_records,
            compensation_failures=compensation_failures,
        )

    def _persist_compensation_failures(
        self,
        failures: list[tuple[str, StepResult, str]],
    ) -> None:
        """Persist retryable compensation failures for watchdog pickup."""
        from app.models.compensation_failure import (
            CompensationFailure,
            CompensationStatus,
        )

        if self.context.ont is None or self.context.olt is None:
            logger.warning(
                "Skipping compensation failure persistence due to missing saga context models",
                extra={
                    "event": "saga_compensation_persistence_skipped",
                    "saga_name": self.saga.name,
                    "saga_execution_id": self.context.saga_execution_id,
                },
            )
            return

        persisted = 0
        for step_name, original_result, error_message in failures:
            if step_name in _SERVICE_PORT_ROLLBACK_STEPS:
                recorded_indices: list[int] = []
                if isinstance(original_result.data, dict):
                    raw_indices = original_result.data.get("created_service_port_indices")
                    if isinstance(raw_indices, list):
                        recorded_indices = [
                            int(index)
                            for index in raw_indices
                            if isinstance(index, int)
                            or (isinstance(index, str) and index.isdigit())
                        ]
                if not recorded_indices:
                    logger.warning(
                        "Skipping saga compensation persistence for %s; no targeted service-port indices recorded",
                        step_name,
                        extra={
                            "event": "saga_compensation_persistence_skipped_no_indices",
                            "saga_name": self.saga.name,
                            "saga_execution_id": self.context.saga_execution_id,
                            "step_name": step_name,
                        },
                    )
                    continue
                failure = CompensationFailure(
                    ont_unit_id=self.context.ont.id,
                    olt_device_id=self.context.olt.id,
                    operation_type=f"saga:{self.saga.name}",
                    step_name="rollback_service_ports",
                    undo_commands=[
                        f"service_port_index:{index}"
                        for index in sorted(set(recorded_indices))
                    ],
                    description=(
                        "Retry targeted service-port rollback after saga compensation "
                        f"failure for step '{step_name}'"
                    ),
                    resource_id=step_name,
                    error_message=error_message,
                    status=CompensationStatus.pending,
                )
                self.context.db.add(failure)
                persisted += 1

        if persisted == 0:
            logger.info(
                "No retryable compensation failure persistence mappings for saga %s",
                self.saga.name,
                extra={
                    "event": "saga_compensation_persistence_noop",
                    "saga_execution_id": self.context.saga_execution_id,
                    "failure_count": len(failures),
                },
            )
            return

        try:
            self.context.db.flush()
            logger.info(
                "Persisted %d saga compensation failure record(s)",
                persisted,
                extra={
                    "event": "saga_compensation_persisted",
                    "saga_name": self.saga.name,
                    "saga_execution_id": self.context.saga_execution_id,
                    "persisted": persisted,
                },
            )
        except Exception:
            logger.exception(
                "Failed to persist saga compensation failures",
                extra={
                    "event": "saga_compensation_persistence_error",
                    "saga_name": self.saga.name,
                    "saga_execution_id": self.context.saga_execution_id,
                },
            )

    def _mark_step_compensated(
        self,
        step: SagaStep,
        record: CompensationRecord,
    ) -> None:
        try:
            from app.services.network.ont_provisioning.saga.persistence import (
                saga_step_executions,
            )

            step_order = next(
                (index for index, candidate in enumerate(self.saga.steps) if candidate.name == step.name),
                0,
            )
            saga_step_executions.mark_compensated(
                self.context.db,
                execution_id=self.context.saga_execution_id,
                saga_name=self.saga.name,
                correlation_key=self.context.correlation_key,
                step_name=step.name,
                step_order=step_order,
                record=record,
            )
        except Exception:
            logger.warning(
                "Failed to persist compensated saga step: %s",
                step.name,
                exc_info=True,
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
    timeout_seconds: float | None = None,
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
        timeout_seconds: Optional total deadline for this saga execution.

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
        timeout_seconds=timeout_seconds,
    )

    executor = SagaExecutor(saga, context)
    return executor.execute()
