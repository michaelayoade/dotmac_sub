"""Device operation primitives enforcing apply→verify→commit pattern.

This module provides a unified approach to device operations where:
1. All changes are first applied to the device
2. Changes are verified via readback/SNMP
3. Only after verification passes is the DB committed

This ensures the OLT is the source of truth and prevents DB/device drift.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.network_operation import (
    NetworkOperation,
    NetworkOperationStatus,
    NetworkOperationTargetType,
    NetworkOperationType,
)

logger = logging.getLogger(__name__)


@dataclass
class DeviceOperationStep:
    """A single step in a device operation with apply, verify, and optional rollback."""

    name: str
    apply_fn: Callable[[], tuple[bool, str]]
    verify_fn: Callable[[], tuple[bool, str]]
    rollback_fn: Callable[[], None] | None = None
    timeout_seconds: float = 30.0


@dataclass
class DeviceOperationResult:
    """Result of executing a device operation."""

    success: bool
    message: str
    steps_completed: list[str] = field(default_factory=list)
    steps_failed: list[str] = field(default_factory=list)
    device_verified: bool = False
    db_committed: bool = False
    partial_success: bool = False
    operation_id: str | None = None


class DeviceOperationContext:
    """Context manager enforcing apply→verify→commit pattern.

    Usage:
        ctx = DeviceOperationContext(db, "ont_move", str(ont_id))
        ctx.add_step(DeviceOperationStep(
            name="deauthorize_old",
            apply_fn=lambda: deauthorize_ont(...),
            verify_fn=lambda: verify_ont_removed(...),
        ))
        result = ctx.execute()

    The operation:
    1. Creates a NetworkOperation record in pending status
    2. For each step, calls apply_fn then verify_fn
    3. On failure with all_or_nothing=True, calls rollback_fn for completed steps
    4. Only commits DB changes if all verifications pass
    5. Updates NetworkOperation status on completion
    """

    def __init__(
        self,
        db: Session,
        operation_type: str,
        target_id: str,
        *,
        all_or_nothing: bool = True,
        target_type: NetworkOperationTargetType = NetworkOperationTargetType.ont,
        initiated_by: str | None = None,
        input_payload: dict | None = None,
    ):
        self.db = db
        self.operation_type = operation_type
        self.target_id = target_id
        self.all_or_nothing = all_or_nothing
        self.target_type = target_type
        self.initiated_by = initiated_by
        self.input_payload = input_payload
        self.steps: list[DeviceOperationStep] = []
        self._completed_steps: list[DeviceOperationStep] = []
        self._network_operation: NetworkOperation | None = None

    def add_step(self, step: DeviceOperationStep) -> None:
        """Add a step to be executed."""
        self.steps.append(step)

    def _create_operation_record(self) -> NetworkOperation:
        """Create the tracking NetworkOperation record."""
        from app.services.common import coerce_uuid

        # Map string operation type to enum if possible
        try:
            op_type = NetworkOperationType(self.operation_type)
        except ValueError:
            # Fall back to a generic type for new operation types
            op_type = NetworkOperationType.ont_provision

        operation = NetworkOperation(
            operation_type=op_type,
            target_type=self.target_type,
            target_id=coerce_uuid(self.target_id),
            status=NetworkOperationStatus.running,
            input_payload=self.input_payload,
            initiated_by=self.initiated_by,
            started_at=datetime.now(UTC),
        )
        self.db.add(operation)
        self.db.flush()
        return operation

    def _rollback_completed_steps(self) -> list[str]:
        """Call rollback_fn for all completed steps in reverse order."""
        rollback_errors: list[str] = []
        for step in reversed(self._completed_steps):
            if step.rollback_fn:
                try:
                    step.rollback_fn()
                    logger.info("Rolled back step: %s", step.name)
                except Exception as exc:
                    error_msg = f"Rollback failed for {step.name}: {exc}"
                    logger.error(error_msg)
                    rollback_errors.append(error_msg)
        return rollback_errors

    def execute(self) -> DeviceOperationResult:
        """Execute all steps with apply→verify→commit semantics.

        Returns:
            DeviceOperationResult with success status and details.
        """
        if not self.steps:
            return DeviceOperationResult(
                success=True,
                message="No steps to execute",
                device_verified=True,
            )

        steps_completed: list[str] = []
        steps_failed: list[str] = []

        try:
            self._network_operation = self._create_operation_record()
        except Exception as exc:
            logger.warning("Failed to create operation record: %s", exc)
            # Continue without tracking record

        try:
            for step in self.steps:
                logger.info("Executing step: %s", step.name)

                # Apply phase
                try:
                    apply_ok, apply_msg = step.apply_fn()
                except Exception as exc:
                    apply_ok, apply_msg = False, f"Apply error: {exc}"
                    logger.error("Step %s apply failed: %s", step.name, exc)

                if not apply_ok:
                    steps_failed.append(step.name)
                    logger.warning("Step %s apply failed: %s", step.name, apply_msg)

                    if self.all_or_nothing:
                        self._rollback_completed_steps()
                        self._finalize_operation(
                            NetworkOperationStatus.failed,
                            f"Apply failed at step {step.name}: {apply_msg}",
                        )
                        return DeviceOperationResult(
                            success=False,
                            message=f"Apply failed at step {step.name}: {apply_msg}",
                            steps_completed=steps_completed,
                            steps_failed=steps_failed,
                            device_verified=False,
                            db_committed=False,
                            operation_id=self._get_operation_id(),
                        )
                    continue

                # Verify phase
                try:
                    verify_ok, verify_msg = step.verify_fn()
                except Exception as exc:
                    verify_ok, verify_msg = False, f"Verify error: {exc}"
                    logger.error("Step %s verify failed: %s", step.name, exc)

                if not verify_ok:
                    steps_failed.append(step.name)
                    logger.warning("Step %s verify failed: %s", step.name, verify_msg)

                    if self.all_or_nothing:
                        # Rollback this step too since apply succeeded
                        if step.rollback_fn:
                            try:
                                step.rollback_fn()
                            except Exception as rb_exc:
                                logger.error(
                                    "Rollback of current step %s failed: %s",
                                    step.name,
                                    rb_exc,
                                )
                        self._rollback_completed_steps()
                        self._finalize_operation(
                            NetworkOperationStatus.failed,
                            f"Verify failed at step {step.name}: {verify_msg}",
                        )
                        return DeviceOperationResult(
                            success=False,
                            message=f"Verify failed at step {step.name}: {verify_msg}",
                            steps_completed=steps_completed,
                            steps_failed=steps_failed,
                            device_verified=False,
                            db_committed=False,
                            operation_id=self._get_operation_id(),
                        )
                    continue

                # Step completed successfully
                steps_completed.append(step.name)
                self._completed_steps.append(step)
                logger.info("Step %s completed successfully", step.name)

            # All steps completed
            all_success = len(steps_failed) == 0
            partial = len(steps_completed) > 0 and len(steps_failed) > 0

            if all_success or (partial and not self.all_or_nothing):
                self._finalize_operation(
                    NetworkOperationStatus.succeeded,
                    f"Completed {len(steps_completed)} steps",
                )
                return DeviceOperationResult(
                    success=all_success,
                    message=f"Completed {len(steps_completed)} steps"
                    + (f", {len(steps_failed)} failed" if steps_failed else ""),
                    steps_completed=steps_completed,
                    steps_failed=steps_failed,
                    device_verified=True,
                    db_committed=False,  # Caller handles DB commit
                    partial_success=partial,
                    operation_id=self._get_operation_id(),
                )

            # All steps failed in non-all-or-nothing mode
            self._finalize_operation(
                NetworkOperationStatus.failed,
                "All steps failed",
            )
            return DeviceOperationResult(
                success=False,
                message="All steps failed",
                steps_completed=steps_completed,
                steps_failed=steps_failed,
                device_verified=False,
                db_committed=False,
                operation_id=self._get_operation_id(),
            )

        except Exception as exc:
            logger.error("Device operation failed unexpectedly: %s", exc, exc_info=True)
            self._rollback_completed_steps()
            self._finalize_operation(
                NetworkOperationStatus.failed,
                f"Unexpected error: {exc}",
            )
            return DeviceOperationResult(
                success=False,
                message=f"Unexpected error: {exc}",
                steps_completed=steps_completed,
                steps_failed=steps_failed,
                device_verified=False,
                db_committed=False,
                operation_id=self._get_operation_id(),
            )

    def _get_operation_id(self) -> str | None:
        """Return the operation ID if we have one."""
        if self._network_operation:
            return str(self._network_operation.id)
        return None

    def _finalize_operation(self, status: NetworkOperationStatus, message: str) -> None:
        """Update the NetworkOperation record with final status."""
        if not self._network_operation:
            return
        try:
            self._network_operation.status = status
            self._network_operation.completed_at = datetime.now(UTC)
            if status == NetworkOperationStatus.failed:
                self._network_operation.error = message[:500] if message else None
            else:
                self._network_operation.output_payload = {"message": message}
            self.db.flush()
        except Exception as exc:
            logger.warning("Failed to finalize operation record: %s", exc)
